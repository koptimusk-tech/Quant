"""Model Lab: comparison measurements, synthetic benchmark, API, caching, cancellation and budgets."""

import csv
import io

import numpy as np
import pytest
from fastapi.testclient import TestClient

from qal import db
from qal.jobs.runner import cancel_job, run_job, submit_job
from qal.models import Experiment, Job
from qal.research import model_lab
from qal.research.model_lab import ModelSpec, episodes, overlap, synthetic_benchmark

FAST_IF = {"detector": "isolation_forest", "params": {"n_estimators": 30}}


@pytest.fixture()
def client(env):
    from qal.api.main import create_app

    with TestClient(create_app()) as c:
        yield c


def _ingest(client, symbols=("SYN-A", "SYN-D")):
    r = client.post("/api/v1/data/ingest", json={"items": [
        {"adapter": "synthetic", "symbol": s, "asset_class": "other"} for s in symbols]})
    assert r.status_code == 200, r.text
    return {x["symbol"]: x["dataset_id"] for x in r.json()["ingested"]}


def test_overlap_and_episodes():
    assert episodes(np.array([10, 11, 12, 30, 31, 100])) == 3
    assert episodes(np.array([])) == 0
    o = overlap(np.array([10, 20, 30]), np.array([11, 50]))
    assert o["share_a_in_b"] == pytest.approx(1 / 3, abs=1e-4) and o["share_b_in_a"] == 0.5
    assert overlap(np.array([1, 2]), np.array([1, 2]))["jaccard"] == 1.0
    assert overlap(np.array([]), np.array([]))["jaccard"] is None


def test_synthetic_benchmark_measures_recall_and_false_alarms():
    b = synthetic_benchmark([ModelSpec(**FAST_IF), ModelSpec("return_shock")], n_bars=1500)
    assert b["synthetic"] is True and "not evidence about real markets" in b["note"]
    rows = {r["model"]: r for r in b["rows"]}
    shocks = [e for e in rows["return_shock"]["events"] if e["kind"] == "return_shock"]
    assert all(e["detected"] for e in shocks)
    iso = rows["isolation_forest"]
    assert any(e["kind"] == "return_shock" and e["scored"] and e["detected"] for e in iso["events"])
    assert not any(e["scored"] for e in iso["events"] if e["bar"] < 600)  # warm-up events are not scored
    assert iso["control_false_alarms"]["rate"] < 0.05


def test_comparison_via_api_is_stored_cached_and_exported(client):
    ids = _ingest(client)
    body = {"dataset_ids": [ids["SYN-A"]], "models": [FAST_IF, {"detector": "return_shock"}],
            "seeds": [0, 1], "window_variants": [0.67], "horizons": [1, 5], "run_async": False,
            "label": "test"}
    r = client.post("/api/v1/models/experiments", json=body)
    assert r.status_code == 202, r.text
    exp_id = r.json()["experiment"]["id"]
    d = client.get(f"/api/v1/models/experiments/{exp_id}").json()
    assert d["status"] == "succeeded" and d["results"]["is_synthetic"] is True
    ds = d["results"]["datasets"][0]
    rows = {x["model"]: x for x in ds["rows"]}
    iso, shock = rows["isolation_forest"], rows["return_shock"]
    assert iso["status"] == "ok" and iso["n_flagged"] >= iso["n_episodes"] >= 1
    assert iso["stability"]["seeds"]["runs"][0]["seed"] == 1
    assert "deterministic" in shock["stability"]["seeds"]["note"]
    assert iso["stability"]["window_variants"][0]["variant"] == "train_window=1005"
    assert set(iso["per_regime"]) >= {"high_vol", "low_vol"} and iso["per_year"]
    assert iso["predictive"]["verdict"] in ("insufficient_sample", "no_evidence", "in_sample_only", "oos_supported")
    assert ds["overlap"]["models"] == ["isolation_forest", "return_shock"]
    assert len(ds["chart"]["ts"]) == len(ds["chart"]["close"]) > 100
    assert "aggregate" not in str(sorted(iso)).lower()  # no opaque combined score
    # identical request: the stored experiment is reused
    again = client.post("/api/v1/models/experiments", json=body).json()["experiment"]["id"]
    assert again == exp_id
    rows_csv = list(csv.DictReader(io.StringIO(client.get(f"/api/v1/models/experiments/{exp_id}/export.csv").text)))
    assert [x["model"] for x in rows_csv] == ["isolation_forest", "return_shock"]
    assert rows_csv[0]["seed_jaccard_mean"] != ""
    assert client.get("/api/v1/models/experiments").json()[0]["id"] == exp_id


def test_catalog_endpoints(client):
    dets = {d["name"]: d for d in client.get("/api/v1/models/detectors").json()}
    assert "pair_divergence" not in dets and dets["isolation_forest"]["stochastic"] is True
    assert dets["isolation_forest"]["multivariate"] and not dets["return_shock"]["multivariate"]
    f = client.get("/api/v1/models/features").json()
    assert set(f["default"]) <= {x["name"] for x in f["features"]}


@pytest.mark.parametrize("body", [
    {"models": [{"detector": "nope"}]},
    {"models": [{"detector": "pair_divergence"}]},
    {"models": [{"detector": "isolation_forest", "params": {"contamination": 0.4}}]},
    {"models": [{"detector": "return_shock"}, {"detector": "return_shock"}]},
    {"seeds": []},
    {"max_seconds": 1},
    {"dataset_ids": []},
])
def test_configuration_errors_return_400(client, body):
    ids = _ingest(client, ("SYN-D",))
    r = client.post("/api/v1/models/experiments", json={"dataset_ids": [ids["SYN-D"]], **body})
    assert r.status_code == 400, r.text


def test_running_job_is_cancelled_cooperatively_and_keeps_its_record(client):
    ids = _ingest(client, ("SYN-A", "SYN-D"))
    with db.session_scope() as s:
        job = submit_job(s, "model_experiment", {"dataset_ids": [ids["SYN-A"], ids["SYN-D"]],
                                                 "models": [FAST_IF], "predictive": False})
        job_id = job.id
        job.status = "running"  # as if a worker had claimed it
    with db.session_scope() as s:
        assert cancel_job(s, s.get(Job, job_id)) and s.get(Job, job_id).status == "cancelling"
    run_job(job_id)
    with db.session_scope() as s:
        job = s.get(Job, job_id)
        assert job.status == "cancelled"
        exp = s.get(Experiment, job.result["experiment_id"])
        assert exp.status == "cancelled"  # kept in the registry, not deleted
        assert all(d["status"] == "skipped" and d["reason"] == "cancelled" for d in exp.results["datasets"])


def test_time_budget_stops_between_units(client, monkeypatch):
    ids = _ingest(client, ("SYN-A", "SYN-D"))
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(model_lab.time, "perf_counter", lambda: float(next(clock)))
    with db.session_scope() as s:
        exp = model_lab.run_comparison(
            s, model_lab.as_config({"dataset_ids": [ids["SYN-A"], ids["SYN-D"]], "models": [FAST_IF],
                                    "predictive": False, "max_seconds": 150}),
            store=_store())
        assert exp.status == "succeeded" and exp.results["budget_exhausted"] is True
        assert any(d["status"] == "skipped" and d["reason"] == "budget" for d in exp.results["datasets"])


def _store():
    from qal.config import get_settings

    return model_lab.ParquetStore(get_settings().data_dir)
