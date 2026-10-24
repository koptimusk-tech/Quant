"""Score explanations, investigation priority and the predictive event study."""

import math
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from qal.anomaly import priority as pr
from qal.anomaly.event_study import evaluate
from qal.anomaly.explain import economic, significance
from qal.data.schema import InstrumentSpec, normalize_bars

# ---- explanations ------------------------------------------------------------------------------


@pytest.mark.parametrize("p,level", [(0.005, "strong"), (0.03, "significant"), (0.07, "weak"), (0.2, "not_significant"),
                                     (1.0, "not_significant"), (None, "unknown"), (float("nan"), "unknown")])
def test_significance_levels(p, level):
    assert significance(p, None)["level"] == level


def test_adjusted_p_of_one_is_not_presented_as_evidence():
    s = significance(1.0, 0.0004, n_tests=108_790)
    assert s["label"] == "Not significant"
    assert "absence of evidence" in s["text"] and "108,790" in s["text"]


def test_usdchf_volatility_example_is_broken_down_and_flagged():
    # The table showed ECON. SCORE 0.86 for a volatility_shift: move 55 bps of daily vol vs 3 bps FX cost.
    ctx = {"assumed_round_trip_cost_bps": 3.0, "legs": 1, "move_bps": 55.2, "move_to_cost_ratio": 18.4,
           "median_dollar_volume_20": 5e6}
    e = economic("volatility_regime", 18.4 / 21.4, ctx)
    assert round(e["score"], 2) == 0.86
    assert e["interpretable"] is False and "volatility" in e["caveat"]
    comps = {c["name"]: c["value"] for c in e["components"]}
    assert comps["move_bps"] == 55.2 and comps["assumed_round_trip_cost_bps"] == 3.0
    assert comps["liquidity_factor"] == 1.0 and math.isclose(comps["saturated_ratio"], 18.4 / 21.4, rel_tol=1e-3)
    assert "not a return estimate" in e["text"] and e["missing"]


def test_economic_liquidity_penalty_and_missing_score():
    low = economic("return_shock", 0.4, {"assumed_round_trip_cost_bps": 10, "legs": 1, "move_bps": 400,
                                         "move_to_cost_ratio": 40, "median_dollar_volume_20": 1e5,
                                         "liquidity_flag": "Median dollar volume 100,000 below 1,000,000"})
    assert {c["name"]: c["value"] for c in low["components"]}["liquidity_factor"] == 0.5
    novol = economic("return_shock", 0.9, {"assumed_round_trip_cost_bps": 3, "legs": 1, "move_bps": 100,
                                           "move_to_cost_ratio": 33.3, "liquidity_flag": "No volume data to assess liquidity"})
    assert {c["name"]: c["value"] for c in novol["components"]}["liquidity_factor"] == 1.0
    idx = economic("return_shock", None, {"reason": "Asset class 'index' is not directly tradable"})
    assert idx["interpretable"] is False and "index" in idx["text"]
    assert economic("return_shock", None, None)["score"] is None  # null context does not crash


# ---- investigation priority -------------------------------------------------------------------

BASE = dict(quality="pass", synthetic=False, duplicate_of=None, age_bars=0.0)


def test_evidence_component_edges():
    assert pr.evidence_component(1.0) == 0.0
    assert pr.evidence_component(None) == 0.0 and pr.evidence_component(float("nan")) == 0.0
    assert pr.evidence_component(0.0) == 1.0 and pr.evidence_component(1e-6) == 1.0
    assert math.isclose(pr.evidence_component(0.05), -math.log10(0.05) / 3)


def test_high_econ_score_without_evidence_does_not_rank_first():
    usdchf = pr.score(detector="volatility_regime", adjusted_p=1.0, stat_score=0.51, econ=0.86,
                      verdict="not_evaluated", **{**BASE, "age_bars": 2400})
    modest_but_significant = pr.score(detector="return_shock", adjusted_p=0.001, stat_score=0.6, econ=0.3,
                                      verdict="not_evaluated", **BASE)
    assert usdchf["tier"] == "low" and usdchf["value"] < modest_but_significant["value"]
    assert "no statistical evidence" in usdchf["explanation"]
    assert "not an execution signal" in usdchf["label"]


def test_cap_without_significance_or_oos_support():
    best_case = pr.score(detector="return_shock", adjusted_p=0.2, stat_score=0.99, econ=0.99,
                         verdict="in_sample_only", **BASE)
    assert best_case["value"] <= 0.49 and best_case["tier"] != "high"
    supported = pr.score(detector="return_shock", adjusted_p=0.0005, stat_score=0.9, econ=0.8,
                         verdict="oos_supported", **BASE)
    assert supported["tier"] == "high" and not supported["capped"]
    assert "out-of-sample predictive evidence" not in supported["label"]


def test_multipliers_and_null_inputs():
    full = pr.score(detector="return_shock", adjusted_p=0.001, stat_score=0.8, econ=0.5, verdict="not_evaluated", **BASE)
    syn = pr.score(detector="return_shock", adjusted_p=0.001, stat_score=0.8, econ=0.5, verdict="not_evaluated",
                   **{**BASE, "synthetic": True})
    dup = pr.score(detector="return_shock", adjusted_p=0.001, stat_score=0.8, econ=0.5, verdict="not_evaluated",
                   **{**BASE, "duplicate_of": "abc"})
    old = pr.score(detector="return_shock", adjusted_p=0.001, stat_score=0.8, econ=0.5, verdict="not_evaluated",
                   **{**BASE, "age_bars": 500})
    assert math.isclose(syn["value"], full["value"] * 0.5, rel_tol=1e-3)
    assert math.isclose(dup["value"], full["value"] * 0.6, rel_tol=1e-3)
    assert math.isclose(old["value"], full["value"] * 0.75, rel_tol=1e-3)
    empty = pr.score(detector="unknown", adjusted_p=None, stat_score=None, econ=None, verdict="weird",
                     quality=None, synthetic=False, duplicate_of=None, age_bars=None)
    assert math.isfinite(empty["value"]) and empty["components"][0]["value"] == 0.0
    assert sum(c["weight"] for c in full["components"]) == pytest.approx(1.0)


# ---- event study ------------------------------------------------------------------------------


def _frame(returns: np.ndarray, delay=timedelta(0)) -> pd.DataFrame:
    idx = pd.date_range("2000-01-03", periods=len(returns), freq="D", tz="UTC")
    close = 100 * np.exp(np.cumsum(returns))
    open_ = np.r_[100.0, close[:-1]]
    raw = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.001,
                        "low": np.minimum(open_, close) * 0.999, "close": close, "volume": 1e6}, index=idx)
    df, _ = normalize_bars(raw, provider="test", spec=InstrumentSpec("T", "fx"), frequency="1d",
                           availability_delay=delay, is_synthetic=True, source_ref="test")
    return df


def _with_shocks(n=6000, every=60, drift_after=0.0, seed=0, size=0.08):
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.005, n)
    for t in range(300, n - 10, every):
        s = size if rng.random() < 0.5 else -size
        r[t] += s
        r[t + 1:t + 6] += np.sign(s) * drift_after
    return r


def test_known_continuation_edge_is_supported_out_of_sample():
    res = evaluate(_frame(_with_shocks(drift_after=0.01)), "return_shock", {}, [1, 5, 10], "fx")
    assert res["verdict"] == "oos_supported", res["verdict_text"]
    assert res["selected"]["rule"] == "continuation"
    assert res["out_of_sample"]["n"] >= 10 and res["out_of_sample"]["ci95_net_bps"][0] > 0
    row = next(r for r in res["table"] if r["horizon"] == res["selected"]["horizon"] and r["rule"] == "continuation")
    assert row["all"]["mean_net_bps"] < row["all"]["mean_gross_bps"]  # costs are charged
    assert len(res["walk_forward"]) == 5


def test_event_bar_move_itself_is_not_captured():
    # The shock bar moves 8 % but nothing follows it: entering at the next open must find no edge.
    res = evaluate(_frame(_with_shocks(drift_after=0.0)), "return_shock", {}, [1, 5, 10], "fx")
    assert res["verdict"] in ("no_evidence", "insufficient_sample")
    assert res["entry_rule"].startswith("Open of the first bar starting at or after available_ts")


def test_availability_delay_pushes_entry_later():
    # With a 1-day publication delay the 5-day drift starts before the earliest possible entry.
    r = _with_shocks(drift_after=0.01)
    now = evaluate(_frame(r), "return_shock", {}, [1], "fx")
    late = evaluate(_frame(r, delay=timedelta(days=1)), "return_shock", {}, [1], "fx")
    g = lambda res: next(x for x in res["table"] if x["rule"] == "continuation")["all"]["mean_gross_bps"]  # noqa: E731
    assert g(now) > 0 and g(late) > 0  # both still inside the drift window, but entries differ
    assert now["table"][0]["all"]["n"] == late["table"][0]["all"]["n"]
    assert g(now) != g(late)


def test_insufficient_sample_and_pair_detector():
    short = evaluate(_frame(_with_shocks(n=900)), "return_shock", {}, [1, 5], "fx")
    assert short["verdict"] == "insufficient_sample"
    assert "anecdotal" in short["verdict_text"]
    tiny = evaluate(_frame(np.random.default_rng(1).normal(0, 0.01, 100)), "return_shock", {}, [1], "fx")
    assert tiny["verdict"] == "insufficient_sample" and tiny["table"] == []
    with pytest.raises(ValueError):
        evaluate(_frame(_with_shocks()), "pair_divergence", {}, [1], "fx")


# ---- API -----------------------------------------------------------------------------------------


@pytest.fixture()
def client(env):
    from qal.api.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_api_priority_stage_explanations_and_event_study(client):
    r = client.post("/api/v1/data/ingest", json={"items": [
        {"adapter": "synthetic", "symbol": s, "asset_class": "other"} for s in ("SYN-A", "SYN-B")]})
    ds = {x["symbol"]: x["dataset_id"] for x in r.json()["ingested"]}
    client.post("/api/v1/scans", json={"dataset_ids": list(ds.values())})

    page = client.get("/api/v1/anomalies", params={"sort": "investigation_priority", "limit": 200}).json()
    vals = [a["investigation_priority"]["value"] for a in page["items"]]
    assert vals == sorted(vals, reverse=True) and len(vals) == page["total"]
    a = page["items"][0]
    assert set(a["explanations"]) == {"rarity", "significance", "economic", "predictive"}
    assert a["stage"]["stage"] == "detection" and a["investigation_priority"]["label"].startswith("Research priority")
    for x in page["items"]:  # synthetic data is halved and nothing is predictive yet
        assert {m["name"]: m["value"] for m in x["investigation_priority"]["multipliers"]}["data_quality"] <= 0.5

    es = client.post("/api/v1/event-studies", json={"dataset_id": ds["SYN-A"], "detector": "return_shock"})
    assert es.status_code == 200, es.text
    assert es.json()["results"]["verdict"] in ("insufficient_sample", "no_evidence", "in_sample_only", "oos_supported")
    again = client.post("/api/v1/event-studies", json={"dataset_id": ds["SYN-A"], "detector": "return_shock"})
    assert again.json()["id"] == es.json()["id"]  # same data and config -> stored study
    assert client.get(f"/api/v1/event-studies/{es.json()['id']}").json()["status"] == "succeeded"
    assert client.post("/api/v1/event-studies", json={"dataset_id": ds["SYN-A"], "detector": "nope"}).status_code == 400
    assert client.post("/api/v1/event-studies", json={"dataset_id": "missing", "detector": "return_shock"}).status_code == 400

    shock = client.get("/api/v1/anomalies", params={"symbol": "SYN-A", "detector": "return_shock", "limit": 1}).json()["items"][0]
    detail = client.get(f"/api/v1/anomalies/{shock['id']}").json()
    assert detail["event_study_id"] == es.json()["id"] and detail["event_study"]["verdict"]
    assert detail["anomaly"]["investigation_priority"]["event_study"]["experiment_id"] == es.json()["id"]
    assert "tests of the scan" in detail["anomaly"]["explanations"]["significance"]["text"]
    assert client.get("/api/v1/anomalies/priority-config").json()["weights"]["evidence"] == 0.35
    assert client.get("/api/v1/anomalies", params={"sort": "bogus"}).status_code == 400


def test_stage_follows_linked_hypotheses(client):
    r = client.post("/api/v1/data/ingest", json={"items": [{"adapter": "synthetic", "symbol": "SYN-A",
                                                             "asset_class": "other"}]})
    ds = r.json()["ingested"][0]["dataset_id"]
    client.post("/api/v1/scans", json={"dataset_ids": [ds]})
    aid = client.get("/api/v1/anomalies", params={"limit": 1}).json()["items"][0]["id"]
    contract = {
        "title": "SYN-A shock follow-up", "source_anomaly_ids": [aid], "dataset_ids": [ds],
        "premise": "Extreme one-bar moves are partly liquidity-driven and tend to correct afterwards.",
        "mechanism": "Market orders hitting thin liquidity overshoot fair value and are later absorbed.",
        "features": ["return_shock statistic"],
        "strategy": {"type": "event", "params": {"detector": "return_shock"}, "param_grid": {"hold_bars": [3, 5]}},
        "entry_rule": "Fade the shock at the next open", "exit_rule": "Exit after hold_bars bars",
        "horizon_bars": 5, "risks": ["trend continuation"], "invalidation": ["OOS Sharpe below threshold"],
        "validation": {"train_bars": 400, "test_bars": 100, "embargo_bars": 5, "holdout_start": "2024-01-01T00:00:00Z"},
    }
    h = client.post("/api/v1/hypotheses", json={"contract": contract})
    assert h.status_code == 201, h.text
    a = client.get(f"/api/v1/anomalies/{aid}").json()
    assert a["anomaly"]["stage"]["stage"] == "investigation"
    assert a["related_hypotheses"][0]["id"] == h.json()["id"]
    assert a["anomaly"]["validation_status"] == "HYPOTHESIS_FORMULATED"  # STATUS keeps its meaning
    client.post(f"/api/v1/hypotheses/{h.json()['id']}/validate", json={"run_async": False})
    st = client.get(f"/api/v1/anomalies/{aid}").json()["anomaly"]["stage"]["stage"]
    assert st in ("oos_passed", "oos_failed")
