"""Phase 2 acceptance: falsifiable contracts, frozen criteria, gates that agents cannot override,
leakage detection, negative results recorded, holdout isolation."""

import copy

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from qal.research import strategies as strat_mod
from qal.research.contract import HypothesisContract
from qal.research.states import TransitionError, check_transition


@pytest.fixture()
def client(env):
    from qal.api.main import create_app

    with TestClient(create_app()) as c:
        yield c


def _ingest(client, symbols=("SYN-A", "SYN-B", "SYN-D")):
    r = client.post("/api/v1/data/ingest", json={"items": [
        {"adapter": "synthetic", "symbol": s, "asset_class": "other"} for s in symbols]})
    return {x["symbol"]: x["dataset_id"] for x in r.json()["ingested"]}


def contract(ds, **over):
    c = {
        "title": "Pair reversion SYN-B vs SYN-A",
        "premise": "Two assets driven by a common factor should not drift apart persistently after costs.",
        "mechanism": "Relative-value traders close temporary deviations of the spread around its rolling fit.",
        "dataset_ids": [ds.get("SYN-B"), ds.get("SYN-A")],
        "features": ["rolling OLS spread z-score"],
        "strategy": {"type": "pair_mean_reversion", "param_grid": {"entry_z": [1.5, 2.0, 2.5]}},
        "entry_rule": "Enter when |z| >= entry_z, short the rich leg",
        "exit_rule": "Exit when |z| <= exit_z or after max_hold bars",
        "horizon_bars": 10,
        "risks": ["relationship break"],
        "invalidation": ["OOS Sharpe below threshold", "edge vanishes at 2x costs"],
        "validation": {"train_bars": 400, "test_bars": 100, "embargo_bars": 20,
                       "holdout_start": "2024-01-01T00:00:00Z", "placebo_runs": 50, "bootstrap_runs": 200},
    }
    c.update(over)
    return c


def test_vague_and_inconsistent_contracts_rejected():
    base = contract({"SYN-A": "a", "SYN-B": "b"})
    with pytest.raises(ValidationError, match="vague"):
        HypothesisContract(**{**base, "premise": "El precio parece que va a subir porque lleva varios días bajando."})
    with pytest.raises(ValidationError, match="embargo"):
        HypothesisContract(**{**base, "horizon_bars": 50})
    with pytest.raises(ValidationError, match="exactly 2"):
        HypothesisContract(**{**base, "dataset_ids": ["a"]})
    with pytest.raises(ValidationError, match="unknown parameters"):
        HypothesisContract(**{**base, "strategy": {"type": "pair_mean_reversion", "params": {"magic": 1}}})


def test_states_reserve_promotions_for_policy_engine():
    with pytest.raises(TransitionError, match="policy engine"):
        check_transition("BACKTEST_PENDING", "VALIDATION_PASSED", "agent")
    with pytest.raises(TransitionError, match="Gate F"):
        check_transition("LIVE_ELIGIBLE", "LIVE_ENABLED", "policy")
    with pytest.raises(TransitionError, match="Phase 6"):
        check_transition("VALIDATION_PASSED", "PAPER_TRADING", "policy")
    check_transition("VALIDATION_FAILED", "REJECTED", "user")


def test_full_research_flow_via_api(client):
    ds = _ingest(client)
    anomalies = client.post("/api/v1/scans", json={"dataset_ids": list(ds.values())}).json()
    aid = client.get("/api/v1/anomalies", params={"symbol": "SYN-B", "limit": 1}).json()["items"][0]["id"]
    r = client.post("/api/v1/hypotheses", json={"contract": contract(ds, source_anomaly_ids=[aid])})
    assert r.status_code == 201, r.text
    hid = r.json()["id"]
    assert client.get(f"/api/v1/anomalies/{aid}").json()["anomaly"]["validation_status"] == "HYPOTHESIS_FORMULATED"

    # Frozen contract and no manual promotion.
    assert client.patch(f"/api/v1/hypotheses/{hid}", json={}).status_code == 409
    bad = client.post(f"/api/v1/hypotheses/{hid}/transition", json={"target": "VALIDATION_PASSED", "reason": "trust me"})
    assert bad.status_code == 409

    v = client.post(f"/api/v1/hypotheses/{hid}/validate").json()
    exp = v["experiment"]
    assert exp["status"] == "succeeded"
    res = exp["results"]
    assert v["status"] == res["decision"] in ("VALIDATION_PASSED", "VALIDATION_FAILED")
    assert {g["gate"] for g in res["gates"]} == {"A", "B", "C", "D"}
    assert all(c["passed"] for c in res["leakage_checks"])
    assert res["oos"]["metrics"]["total_return_net"] < res["oos"]["metrics"]["total_return_gross"]  # costs bite
    assert [c["multiplier"] for c in res["robustness"]["cost_sensitivity"]] == [0.0, 0.5, 1.0, 2.0, 3.0]
    assert res["overfitting"]["n_trials"] == 3

    detail = client.get(f"/api/v1/hypotheses/{hid}").json()
    assert len(detail["gates"]) == 4 and detail["experiments"][0]["kind"] == "validation"

    # Re-running counts more trials (selection bias accounting), even with the same contract.
    v2 = client.post(f"/api/v1/hypotheses/{hid}/validate").json()["experiment"]["results"]
    assert v2["overfitting"]["n_trials"] == 6

    # New version shares the family: trials accumulate across versions.
    c2 = contract(ds, title="Pair reversion v2 wider grid")
    c2["strategy"] = {"type": "pair_mean_reversion", "param_grid": {"entry_z": [1.5, 2.0], "window": [60, 120]}}
    h2 = client.post("/api/v1/hypotheses", json={"contract": c2, "parent_id": hid}).json()
    assert h2["family_id"] == hid and h2["version"] == 2
    v3 = client.post(f"/api/v1/hypotheses/{h2['id']}/validate").json()["experiment"]["results"]
    assert v3["overfitting"]["n_trials"] == 10

    # Holdout: explicit confirmation, every access counted, second access contaminated.
    assert client.post(f"/api/v1/hypotheses/{hid}/final-test").status_code == 400
    f1 = client.post(f"/api/v1/hypotheses/{hid}/final-test", params={"confirm": True}).json()["experiment"]["results"]
    assert f1["holdout_bars"] > 0 and f1["contaminated"] is False
    f2 = client.post(f"/api/v1/hypotheses/{hid}/final-test", params={"confirm": True}).json()["experiment"]["results"]
    assert f2["contaminated"] is True and f2["access_number"] == 2


def test_noise_strategy_is_blocked_by_gates(client):
    ds = _ingest(client, ("SYN-D",))
    c = contract(ds, title="Momentum on a random walk", dataset_ids=[ds["SYN-D"]],
                 strategy={"type": "ts_momentum", "param_grid": {"lookback": [5, 10, 20, 40, 80]}})
    hid = client.post("/api/v1/hypotheses", json={"contract": c}).json()["id"]
    res = client.post(f"/api/v1/hypotheses/{hid}/validate").json()["experiment"]["results"]
    assert res["decision"] == "VALIDATION_FAILED"
    assert any(not g["passed"] for g in res["gates"])
    assert client.get(f"/api/v1/hypotheses/{hid}").json()["hypothesis"]["status"] == "VALIDATION_FAILED"


class PeekingStrategy(strat_mod.Strategy):
    """Deliberately uses tomorrow's return: must be caught by the leakage checks."""

    name = "peeking"
    description = "test only"
    defaults = {"k": 1}

    def weights(self, frames):
        f = frames[0]
        # Sign of the *next* bar's open-to-close move: exactly the move the position will be exposed to.
        return pd.DataFrame({0: np.sign(f["close"].shift(-1) / f["open"].shift(-1) - 1).fillna(0).to_numpy()})


def test_lookahead_strategy_fails_leakage_gate(client, monkeypatch):
    monkeypatch.setitem(strat_mod.STRATEGIES, "peeking", PeekingStrategy)
    ds = _ingest(client, ("SYN-D",))
    c = contract(ds, title="Cheating strategy", dataset_ids=[ds["SYN-D"]], strategy={"type": "peeking"})
    hid = client.post("/api/v1/hypotheses", json={"contract": c}).json()["id"]
    res = client.post(f"/api/v1/hypotheses/{hid}/validate").json()["experiment"]["results"]
    causal = next(x for x in res["leakage_checks"] if x["check"] == "strategy_causality")
    assert causal["passed"] is False
    gate_b = next(g for g in res["gates"] if g["gate"] == "B")
    assert gate_b["passed"] is False and res["decision"] == "VALIDATION_FAILED"
    # It looked spectacular in-sample: exactly why gates, not returns, decide.
    assert res["oos"]["metrics"]["sharpe_gross"] > 3


def test_failed_validation_run_is_recorded(client):
    ds = _ingest(client, ("SYN-D",))
    c = contract(ds, title="Too little data", dataset_ids=[ds["SYN-D"]],
                 strategy={"type": "ts_momentum"},
                 validation={"train_bars": 5000, "test_bars": 100, "embargo_bars": 20,
                             "holdout_start": "2024-01-01T00:00:00Z"})
    hid = client.post("/api/v1/hypotheses", json={"contract": c}).json()["id"]
    exp = client.post(f"/api/v1/hypotheses/{hid}/validate").json()["experiment"]
    assert exp["status"] == "failed" and "Not enough research bars" in exp["error"]
    assert client.get(f"/api/v1/hypotheses/{hid}").json()["hypothesis"]["status"] == "HYPOTHESIS_FORMULATED"


def test_validation_is_reproducible(client):
    ds = _ingest(client, ("SYN-A", "SYN-B"))
    hid = client.post("/api/v1/hypotheses", json={"contract": contract(ds)}).json()["id"]
    a = client.post(f"/api/v1/hypotheses/{hid}/validate").json()["experiment"]["results"]
    b = client.post(f"/api/v1/hypotheses/{hid}/validate").json()["experiment"]["results"]
    for k in ("oos", "windows"):
        assert a[k] == b[k]
    assert a["robustness"]["placebo_pvalue"] == b["robustness"]["placebo_pvalue"]
    assert a["robustness"]["bootstrap"] == b["robustness"]["bootstrap"]


def test_async_validation_job(client):
    from qal.jobs.runner import process_once

    ds = _ingest(client, ("SYN-A", "SYN-B"))
    hid = client.post("/api/v1/hypotheses", json={"contract": contract(ds)}).json()["id"]
    job = client.post(f"/api/v1/hypotheses/{hid}/validate", json={"run_async": True}).json()["job"]
    assert process_once() == 1
    j = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert j["status"] == "succeeded" and j["result"]["decision"] in ("VALIDATION_PASSED", "VALIDATION_FAILED")


def test_meta_endpoints(client):
    meta = client.get("/api/v1/research/meta").json()
    assert "LIVE_ENABLED" in meta["states"] and "crypto" in meta["default_costs"]
    assert {s["name"] for s in client.get("/api/v1/strategies").json()} >= {"event", "pair_mean_reversion"}
