"""Experiment Lab: contract, planner states, validation pipeline, selection, versioning and isolation."""

from __future__ import annotations

import ast
import copy
import pathlib

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from qal import db
from qal.config import get_settings
from qal.data.ingest import IngestRequest, ingest
from qal.data.store import ParquetStore
from qal.lab import generator, selection, service
from qal.lab.pipeline import Pipeline
from qal.lab.spec import ExperimentSpec
from qal.models import Dataset, Experiment, Hypothesis, Job
from qal.research.costs import CostModel, default_cost_model


@pytest.fixture()
def lab(session):
    ids = {x: ingest(session, IngestRequest(adapter="synthetic", symbol=x, asset_class="other"))["dataset_id"]
           for x in ("SYN-A", "SYN-B", "SYN-C", "SYN-D")}
    session.commit()
    end = pd.Timestamp(str(session.get(Dataset, ids["SYN-D"]).end_ts)[:10])
    return {"ids": ids, "holdout": str((end - pd.Timedelta(days=300)).date()),
            "store": ParquetStore(get_settings().data_dir)}


def momentum(lab, **over) -> dict:
    c = {"title": "Momentum SYN-D",
         "premise": "Trends persist because information diffuses slowly across investors over weeks.",
         "mechanism": "Slow diffusion of information and herding keep prices moving in the same direction for weeks.",
         "dataset_ids": [lab["ids"]["SYN-D"]], "features": ["trailing return"],
         "strategy": {"type": "ts_momentum", "param_grid": {"lookback": [20, 60, 120]}},
         "entry_rule": "Long when the trailing return is positive", "exit_rule": "Flat or short when it turns negative",
         "horizon_bars": 5, "risks": ["reversals"], "invalidation": ["OOS net return not above baselines"],
         "validation": {"train_bars": 300, "test_bars": 100, "embargo_bars": 5, "holdout_start": lab["holdout"],
                        "placebo_runs": 30, "bootstrap_runs": 100}}
    contract_over = over.pop("contract", {})
    c.update(contract_over)
    return {"family": "momentum", "contract": c, **over}


def spec_of(lab, **over) -> ExperimentSpec:
    return ExperimentSpec.model_validate(momentum(lab, **over))


def run_new(s, lab, **over) -> Experiment:
    exp, _ = service.create(s, spec_of(lab, **over))
    return service.run(s, exp, store=lab["store"])


def pipeline_for(s, lab, spec: ExperimentSpec, frames=None) -> Pipeline:
    dss = [s.get(Dataset, d) for d in spec.contract.dataset_ids]
    frames = frames or [lab["store"].read(d.path) for d in dss]
    return Pipeline(spec, frames, [d.instrument.symbol for d in dss], "other",
                    [default_cost_model("other") for _ in dss], {d.instrument.symbol: d.quality_status for d in dss})


# -- contract / invalid configuration -----------------------------------------------------------------------
def test_invalid_configurations_are_refused(lab):
    with pytest.raises(ValidationError, match="family momentum uses"):
        spec_of(lab, contract={"strategy": {"type": "mean_reversion"}})
    with pytest.raises(ValidationError, match="must condition on a regime"):
        ExperimentSpec.model_validate({**momentum(lab), "family": "regime"})
    with pytest.raises(ValidationError, match="unknown baselines"):
        spec_of(lab, baselines=["no_trade", "lucky_guess"])
    with pytest.raises(ValidationError, match="needs `ablation`"):
        spec_of(lab, baselines=["no_trade", "ablation"])
    with pytest.raises(ValidationError, match="above the limit"):
        spec_of(lab, limits={"max_variants": 2})
    with pytest.raises(ValidationError, match="cost_multipliers must include 1.0"):
        spec_of(lab, robustness={"cost_multipliers": [2.0]})
    pairs = momentum(lab, contract={"dataset_ids": [lab["ids"]["SYN-B"], lab["ids"]["SYN-A"]],
                                    "strategy": {"type": "pair_mean_reversion"}, "horizon_bars": 5})
    with pytest.raises(ValidationError, match="not valid for pairs"):
        ExperimentSpec.model_validate({**pairs, "family": "pairs", "baselines": ["no_trade", "buy_and_hold"]})
    assert "buy_and_hold" not in ExperimentSpec.model_validate({**pairs, "family": "pairs"}).resolved_baselines()


def test_unknown_dataset_and_mixed_synthetic_real_are_refused(session, lab):
    with pytest.raises(service.LabError, match="unknown dataset"):
        service.create(session, spec_of(lab, contract={"dataset_ids": ["nope"]}))
    ds = session.get(Dataset, lab["ids"]["SYN-A"])
    ds.is_synthetic = False
    pairs = momentum(lab, contract={"dataset_ids": [lab["ids"]["SYN-B"], lab["ids"]["SYN-A"]],
                                    "strategy": {"type": "pair_mean_reversion"}})
    with pytest.raises(service.LabError, match="mix synthetic and real"):
        service.create(session, ExperimentSpec.model_validate({**pairs, "family": "pairs"}))


def test_critical_integrity_failure_marks_experiment_invalid(session, lab):
    exp = run_new(session, lab, contract={"validation": {"train_bars": 5000, "test_bars": 100, "embargo_bars": 5,
                                                         "holdout_start": lab["holdout"]}})
    assert exp.status == "INVALID" and "enough_research_bars" in exp.error
    assert "decision_at_completion" not in exp.results["pipeline"]
    assert exp.id not in [r["experiment_id"] for r in service.ranking(session)["rows"]]


# -- reproducibility, idempotency, retries ---------------------------------------------------------------
def test_pipeline_is_reproducible(session, lab):
    spec = spec_of(lab)
    a = pipeline_for(session, lab, spec).run(n_prior_trials=0, holdout_accesses_before=0)
    b = pipeline_for(session, lab, spec).run(n_prior_trials=0, holdout_accesses_before=0)
    assert a == b


def test_same_spec_same_data_is_idempotent_and_failed_runs_can_be_retried(session, lab):
    exp = run_new(session, lab)
    again, created = service.create(session, spec_of(lab))
    assert not created and again.id == exp.id and again.status == "COMPLETED"
    assert service.run(session, again, store=lab["store"]) is again and again.results["attempts"] == 1
    # a FAILED experiment may be submitted again: a new row that points at the failed one
    failed, _ = service.create(session, spec_of(lab, contract={"title": "Momentum SYN-D failing"}))
    failed.data_refs = [{**failed.data_refs[0], "content_hash": "tampered"}]
    service.run(session, failed, store=lab["store"])
    assert failed.status == "FAILED" and "changed since" in failed.error
    retry, created = service.create(session, spec_of(lab, contract={"title": "Momentum SYN-D failing"}))
    assert created and retry.id != failed.id and retry.config["previous_attempt"] == failed.id


def test_job_submission_is_idempotent_and_the_job_runs_the_experiment(session, lab):
    from qal.jobs.runner import run_job

    exp, _ = service.create(session, spec_of(lab))
    j1, j2 = service.submit(session, exp), service.submit(session, exp)
    assert j1.id == j2.id
    session.commit()
    run_job(j1.id)
    session.expire_all()
    assert session.get(Experiment, exp.id).status == "COMPLETED"
    assert session.get(Job, j1.id).status == "succeeded"


# -- cancelled, failed, interrupted ------------------------------------------------------------------------
def test_cancel_queued_and_running_and_refuse_finished(session, lab):
    q, _ = service.create(session, spec_of(lab))
    service.submit(session, q)
    service.request_cancel(session, q)
    assert q.status == "CANCELLED" and session.scalar(select(Job.status)) == "cancelled"
    stopped, _ = service.create(session, spec_of(lab, contract={"title": "Momentum SYN-D stopped"}))
    service.run(session, stopped, store=lab["store"], should_stop=lambda: True)
    assert stopped.status == "CANCELLED" and stopped.error == "cancelled"
    with pytest.raises(service.LabError, match="only QUEUED or RUNNING"):
        service.request_cancel(session, stopped)
    # a cancelled experiment can be submitted again
    again, created = service.create(session, spec_of(lab, contract={"title": "Momentum SYN-D stopped"}))
    assert created and again.config["previous_attempt"] == stopped.id


def test_resource_limit_ends_failed_and_is_recorded(session, lab):
    exp = run_new(session, lab, limits={"max_bars": 100})
    assert exp.status == "FAILED" and "resource limit" in exp.error and exp.finished_at is not None


def test_interrupted_run_is_requeued_and_recovers(session, lab):
    from qal.jobs.runner import run_job

    exp, _ = service.create(session, spec_of(lab))
    exp.status, exp.results = "RUNNING", {"attempts": 1, "stage": "C_temporal"}  # worker died mid-run
    queued = service.enqueue_orphans(session)
    assert queued == [exp.id] and exp.status == "QUEUED"
    job = service.active_job(session, exp)
    session.commit()
    run_job(job.id)
    session.expire_all()
    exp = session.get(Experiment, exp.id)
    assert exp.status == "COMPLETED" and exp.results["attempts"] == 2
    assert service.enqueue_orphans(session) == []


# -- temporal validation: leakage, separation, purge/embargo ----------------------------------------------
def test_final_period_data_does_not_influence_research_results(session, lab):
    spec = spec_of(lab)
    base = pipeline_for(session, lab, spec)
    a = base.run(n_prior_trials=0, holdout_accesses_before=0)
    frames = [f.copy() for f in base.frames]
    late = frames[0]["ts"] >= pd.Timestamp(lab["holdout"], tz="UTC")
    for col in ("open", "high", "low", "close", "adj_close"):
        if col in frames[0]:
            frames[0].loc[late, col] = frames[0].loc[late, col] * 3.0
    b = pipeline_for(session, lab, spec, frames).run(n_prior_trials=0, holdout_accesses_before=0)
    for k in ("B_baselines", "D_robustness", "E_statistics", "oos_series"):
        assert a[k] == b[k], k


def test_leakage_checks_run_and_windows_respect_the_embargo(session, lab):
    exp = run_new(session, lab)
    p = exp.results["pipeline"]
    leak = [c for c in p["A_integrity"]["checks"] if c["check"].startswith("leakage:")]
    assert leak and all(c["passed"] and c["critical"] for c in leak)
    hold = pd.Timestamp(lab["holdout"], tz="UTC")
    assert all(pd.Timestamp(t) < hold for t in p["oos_series"]["ts"])
    emb = p["C_temporal"]["embargo_bars"]
    assert emb >= exp.config["spec"]["contract"]["horizon_bars"]
    from qal.research.validation import windows

    for w in windows(p["C_temporal"]["research_bars"], 300, 100, emb):
        assert w["test_start"] >= w["train_end"] + emb


def test_holdout_reuse_is_counted_and_flagged(session, lab):
    first = run_new(session, lab, evaluate_holdout=True)
    second = run_new(session, lab, evaluate_holdout=True, contract={"title": "Momentum SYN-D again"})
    h1, h2 = (e.results["pipeline"]["C_temporal"]["holdout"] for e in (first, second))
    assert h1["independent"] and h1["access_number"] == 1
    assert not h2["independent"] and h2["access_number"] == 2
    warn = second.results["pipeline"]["decision_at_completion"]["warnings"]
    assert any("Final test reused" in w for w in warn)


# -- baselines, robustness, statistics ----------------------------------------------------------------------
def test_baselines_are_the_declared_ones_and_compared_on_oos_bars(session, lab):
    exp = run_new(session, lab)
    p = exp.results["pipeline"]
    assert [b["baseline"] for b in p["B_baselines"]] == ["no_trade", "buy_and_hold", "permuted_signal",
                                                         "simple_reference"]
    nt = p["B_baselines"][0]
    assert nt["diff_mean_net"] == pytest.approx(p["E_statistics"]["oos"]["mean_net"], rel=1e-6, abs=1e-12)


def test_cost_and_delay_robustness_move_in_the_expected_direction(session, lab):
    rob = run_new(session, lab).results["pipeline"]["D_robustness"]
    by_mult = {c["multiplier"]: c["mean_net"] for c in rob["costs"]}
    ms = sorted(by_mult)
    assert all(by_mult[a] >= by_mult[b] for a, b in zip(ms, ms[1:], strict=False))
    assert {d["delay_bars"] for d in rob["delays"]} == {1, 2}


def test_multiple_testing_counts_earlier_lab_trials(session, lab):
    a = run_new(session, lab)
    b = run_new(session, lab, contract={"title": "Momentum SYN-D variant",
                                        "strategy": {"type": "ts_momentum", "param_grid": {"lookback": [40, 80]}}})
    assert b.results["pipeline"]["n_prior_trials"] == a.n_tests
    assert b.results["pipeline"]["E_statistics"]["n_trials"] == a.n_tests + b.n_tests
    rows = service.ranking(session)["rows"]
    assert all(r["adjusted_p"] >= r["raw_p"] - 1e-12 for r in rows)


def test_insufficient_sample_is_inconclusive_with_a_warning(session, lab):
    exp = run_new(session, lab)
    res = copy.deepcopy(exp.results["pipeline"])
    res["E_statistics"]["oos"].update(bars=50, n_trades=3, mean_net=0.01)
    d = selection.decide(res, exp.config["spec"]["contract"], adjusted_p=0.001)
    assert d["decision"] == "INCONCLUSIVE" and "insufficient sample" in d["reasons"][0]
    assert any(w.startswith("Small sample") for w in d["warnings"])


# -- selection: critical controls cannot be compensated --------------------------------------------------
def test_critical_controls_are_not_compensated_by_the_score(session, lab):
    exp = run_new(session, lab)
    contract = exp.config["spec"]["contract"]
    res = copy.deepcopy(exp.results["pipeline"])
    res["E_statistics"]["oos"].update(bars=2000, n_trades=500, mean_net=-1e-4)
    d = selection.decide(res, contract, adjusted_p=1e-6)
    assert d["decision"] == "REJECTED" and d["score"] > 0
    res2 = copy.deepcopy(exp.results["pipeline"])
    res2["invalid"] = ["leakage:feature_availability"]
    assert selection.decide(res2, contract, adjusted_p=1e-6)["decision"] == "REJECTED"
    # weights are explicit and sum to one; the decision order beats the score in the ranking
    assert sum(selection.WEIGHTS.values()) == pytest.approx(1.0)
    rows = [{"decision": "REJECTED", "score": 0.99, "adjusted_p": 0.0},
            {"decision": "INCONCLUSIVE", "score": 0.10, "adjusted_p": 0.9}]
    assert sorted(rows, key=selection.rank_key)[0]["decision"] == "INCONCLUSIVE"


def test_decisions_never_promote_or_touch_other_modules(session, lab):
    run_new(session, lab)
    service.ranking(session)
    service.record_selection(session, actor="tester", note="review")
    assert session.scalars(select(Hypothesis)).all() == []
    kinds = {e.kind for e in session.scalars(select(Experiment)).all()}
    assert kinds == {"lab_experiment", "lab_selection"}


# -- parameter / cost changes and versioning ---------------------------------------------------------------
def test_parameter_and_cost_changes_create_new_versions_without_overwriting(session, lab):
    a = run_new(session, lab)
    before = copy.deepcopy(a.results)
    ds = lab["ids"]["SYN-D"]
    cm = CostModel(commission_bps=5, half_spread_bps=5, slippage_bps=5).model_dump()
    b = run_new(session, lab, contract={"costs": {ds: cm}})
    assert b.id != a.id and a.results == before
    assert b.config["spec_hash"] != a.config["spec_hash"]
    lin = service.lineage(session, b)
    assert [x["id"] for x in lin] == [a.id, b.id]
    assert any(k.startswith("contract.costs") for k in lin[1]["changed_from_previous"])
    cost_check = next(c for c in b.results["pipeline"]["A_integrity"]["checks"] if c["check"] == "explicit_costs")
    assert cost_check["passed"]


def test_versions_and_data_snapshots_are_stored(session, lab):
    exp = run_new(session, lab)
    assert exp.algorithm_version.startswith("lab-pipeline@") and exp.algorithm_version.endswith("lab-exp@1")
    assert exp.code_version and exp.config_hash and exp.seed == exp.config["spec"]["contract"]["validation"]["seed"]
    ref = exp.data_refs[0]
    assert ref["content_hash"] == session.get(Dataset, ref["dataset_id"]).content_hash
    assert exp.results["pipeline"]["decision_at_completion"]["version"] == selection.SELECTION_VERSION


# -- generator --------------------------------------------------------------------------------------------
def test_generator_covers_families_and_skips_near_duplicates(session, lab):
    g = generator.generate(session, limit=50)
    fams = {o["family"] for o in g["proposed"]}
    assert {"momentum", "mean_reversion", "regime", "factor", "pairs"} <= fams
    assert g["n_variants"] == sum(o["variants"] for o in g["proposed"])
    for o in g["proposed"]:
        c = o["spec"]["contract"]
        assert c["mechanism"] and c["invalidation"] and c["risks"] and o["baselines"]
    first = next(o for o in g["proposed"] if o["family"] == "mean_reversion")
    service.create(session, ExperimentSpec.model_validate(first["spec"]), generator_key=first["generator_key"])
    g2 = generator.generate(session, families=["mean_reversion"], limit=50)
    assert first["generator_key"] not in {o["generator_key"] for o in g2["proposed"]}
    assert any("same family" in s["reason"] for s in g2["skipped"])


# -- portfolio contribution ------------------------------------------------------------------------------
def test_portfolio_contribution_compares_combination_with_individuals(session, lab):
    a = run_new(session, lab)
    b = run_new(session, lab, contract={"title": "Momentum SYN-C", "dataset_ids": [lab["ids"]["SYN-C"]]})
    out = service.portfolio_contribution(session, [a.id, b.id])
    assert {"correlation", "combined", "individual", "baseline_cash", "loss_diversification", "costs_and_turnover",
            "factor_exposure"} <= set(out)
    assert "stress" in out["correlation"] and "regime" in out["correlation"]
    assert out["costs_and_turnover"][a.id]["total_cost"] is not None
    with pytest.raises(service.LabError):
        service.portfolio_contribution(session, [a.id])


# -- API ----------------------------------------------------------------------------------------------------
def test_api_end_to_end(env, lab):
    from qal.api.main import create_app

    with db.session_scope():
        pass
    c = TestClient(create_app())
    meta = c.get("/api/v1/lab/meta").json()
    assert meta["live_trading_enabled"] is False and "PAPER_ELIGIBLE" in meta["decisions"]
    assert c.post("/api/v1/lab/experiments", json={"spec": {"family": "momentum"}}).status_code == 400
    r = c.post("/api/v1/lab/experiments", json={"spec": momentum(lab), "run_async": False})
    assert r.status_code == 202 and r.json()["created"]
    eid = r.json()["experiment"]["id"]
    assert r.json()["experiment"]["status"] == "COMPLETED"
    dup = c.post("/api/v1/lab/experiments", json={"spec": momentum(lab), "run_async": True}).json()
    assert not dup["created"] and dup["experiment"]["id"] == eid and dup["job"] is None
    q = c.post("/api/v1/lab/experiments", json={"spec": momentum(lab, contract={"title": "Queued one"})}).json()
    assert q["experiment"]["status"] == "QUEUED" and q["job"]["kind"] == "lab_experiment"
    assert c.post(f"/api/v1/lab/experiments/{q['experiment']['id']}/cancel").json()["status"] == "CANCELLED"
    assert c.post(f"/api/v1/lab/experiments/{eid}/cancel").status_code == 409
    assert {e["id"] for e in c.get("/api/v1/lab/experiments").json()} >= {eid, q["experiment"]["id"]}
    detail = c.get(f"/api/v1/lab/experiments/{eid}").json()
    assert detail["lineage"] and detail["results"]["pipeline"]["B_baselines"]
    assert c.get(f"/api/v1/lab/experiments/{eid}/export.json").status_code == 200
    rk = c.get("/api/v1/lab/ranking").json()
    assert rk["rows"][0]["experiment_id"] == eid and rk["rows"][0]["components"]
    csv_text = c.get("/api/v1/lab/ranking/export.csv").text
    assert csv_text.splitlines()[0].startswith("rank,experiment_id") and eid in csv_text
    assert c.post("/api/v1/lab/selections", json={"note": "first look"}).status_code == 201
    assert len(c.get("/api/v1/lab/selections").json()) == 1
    assert c.post("/api/v1/lab/portfolio", json={"experiment_ids": [eid, "x"]}).status_code == 400
    assert c.post("/api/v1/lab/generate", json={"families": ["astrology"]}).status_code == 400
    assert c.post("/api/v1/lab/generate", json={"limit": 3}).json()["n_specs"] <= 3


# -- isolation: no orders ----------------------------------------------------------------------------------
def test_lab_cannot_place_orders_or_reach_brokers():
    forbidden_mods = {"requests", "httpx", "urllib", "socket", "websocket", "websockets", "ccxt", "MetaTrader5",
                      "aiohttp", "ib_insync", "alpaca", "oandapyV20", "yfinance"}
    root = pathlib.Path(service.__file__).parent
    files = [*root.glob("*.py"), root.parent / "api" / "routers" / "lab.py"]
    for f in files:
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for n in names:
                assert n.split(".")[0] not in forbidden_mods, f"{f.name} imports {n}"
                assert not n.startswith("qal.paper"), f"{f.name} imports the paper engine ({n})"
            if isinstance(node, ast.Name):
                assert node.id not in {"PaperOrder", "PaperFill", "PaperSession"}, f"{f.name} uses {node.id}"
