"""Robustness Lab: market compatibility, period separation, leakage, scenarios, parameters, costs, capacity, stress,
risk controls, combinations, the state rules, persistence and isolation."""

from __future__ import annotations

import ast
import json
import pathlib

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from qal import db
from qal.jobs.runner import run_job, submit_job
from qal.models import Dataset, Experiment, Job
from qal.robustness import engine, report, service
from tests.test_lab import lab, momentum, run_new  # noqa: F401  (lab is a fixture)

FAST = {"max_targets": 2, "capital_levels": [1e5, 1e6, 1e7]}


def cfg(exp: Experiment, **over) -> service.RobustnessConfig:
    return service.RobustnessConfig(experiment_id=exp.id, **{**FAST, **over})


@pytest.fixture()
def cand(session, lab):  # noqa: F811
    exp = run_new(session, lab)
    assert exp.status == "COMPLETED"
    session.commit()
    return exp


def ctx_of(session, lab, exp, frames=None):  # noqa: F811
    return engine.build_context(session, exp, lab["store"], frames=frames)


# -- full run: every dimension, the matrix and nothing else touched ------------------------------------------
def test_run_reports_every_dimension_with_its_evidence_and_touches_nothing_else(session, lab, cand):  # noqa: F811
    before = json.dumps(cand.results, sort_keys=True, default=str)  # as stored (tuples become lists)
    jobs_before = session.scalars(select(Job)).all()
    e = service.run(session, cfg(cand), store=lab["store"])
    r = e.results
    assert e.status == "succeeded" and r["state"] in report.STATES and r["state_reasons"]
    assert set(r["dimensions"]) == set(report.DIMENSIONS)
    assert all(d["status"] in ("PASS", "FAIL", "INSUFFICIENT_DATA") and d["reason"] for d in r["dimensions"].values())
    rows = r["cross_asset"]["rows"]
    assert rows[0]["category"] == "design" and len(rows) == 3
    for row in rows:
        assert {"symbol", "market", "timeframe", "period", "provider", "quality_status", "costs", "cost_source",
                "status"} <= set(row)
        if row["status"] == "evaluated":
            assert {"validation", "mean_net", "sharpe_gross", "sharpe_net", "bars", "n_trades", "sample"} <= set(row)
    assert all("estimate" in x["cost_source"] for x in rows[1:])  # defaults are labelled estimates
    assert r["parameters"]["n_configs"] == len(r["parameters"]["rows"]) > 1
    assert set(r["execution"]["scenarios"]) == {"base", "adverse", "severe"}
    assert r["capacity"]["status"] in ("APPROXIMATE", "UNKNOWN")
    assert any("Synthetic data" in x for x in r["limitations"])
    assert r["lab"]["exploratory"] is True  # not OOS_SUPPORTED on synthetic data
    assert "not a forecast" in r["note"] and "promote nothing" in r["note"]
    session.expire_all()
    assert json.dumps(session.get(Experiment, cand.id).results, sort_keys=True, default=str) == before  # unchanged
    assert len(session.scalars(select(Job)).all()) == len(jobs_before)


def test_run_is_persisted_idempotent_and_changes_create_new_runs(session, lab, cand):  # noqa: F811
    a = service.run(session, cfg(cand), store=lab["store"])
    session.commit()
    b = service.run(session, cfg(cand), store=lab["store"])
    assert a.id == b.id
    c = service.run(session, cfg(cand, capital_levels=[1e5, 1e6]), store=lab["store"])
    assert c.id != a.id
    session.commit()
    session.expire_all()
    stored = session.get(Experiment, a.id)
    assert stored.kind == service.KIND and stored.results["dimensions"] and stored.config_hash
    assert {x.id for x in session.scalars(select(Experiment).where(Experiment.kind == service.KIND))} == {a.id, c.id}
    row = next(x for x in service.candidates(session) if x["experiment_id"] == cand.id)
    assert row["robustness_run_id"] == c.id  # the latest run, the earlier one is kept


def test_only_completed_candidates_and_changed_data_is_not_evaluated(session, lab, cand):  # noqa: F811
    from qal.lab import service as lab_service
    from tests.test_lab import spec_of

    q, _ = lab_service.create(session, spec_of(lab, contract={"title": "Queued"}))
    with pytest.raises(service.RobustnessError, match="only COMPLETED"):
        service.run(session, cfg(q), store=lab["store"])
    with pytest.raises(service.RobustnessError, match="unknown"):
        service.run(session, service.RobustnessConfig(experiment_id="nope"), store=lab["store"])
    d = session.get(Dataset, lab["ids"]["SYN-D"])
    d.content_hash = "changed"
    e = service.run(session, cfg(cand), store=lab["store"])
    assert e.results["state"] == "NOT_EVALUATED" and "changed" in e.results["state_reasons"][0]


# -- market compatibility and correlated assets --------------------------------------------------------------
def test_market_compatibility_filters_frequency_quality_and_synthetic(session, lab, cand):  # noqa: F811
    ctx = ctx_of(session, lab, cand)
    assert {d.instrument.symbol for d in engine.compatible_targets(session, ctx, None, 10)} == {"SYN-A", "SYN-B", "SYN-C"}
    pair = run_new(session, lab, family="pairs", contract={
        "title": "Pair SYN-B vs SYN-A", "dataset_ids": [lab["ids"]["SYN-B"], lab["ids"]["SYN-A"]],
        "strategy": {"type": "pair_mean_reversion", "param_grid": {"window": [20, 60]}}})
    assert pair.status == "COMPLETED"
    pctx = ctx_of(session, lab, pair)
    assert engine.compatible_targets(session, pctx, None, 10) == []
    assert engine.cross_asset(session, pctx, lab["store"], ids=None, limit=5, check=lambda: None)["not_applicable"]
    session.get(Dataset, lab["ids"]["SYN-A"]).frequency = "1h"
    session.get(Dataset, lab["ids"]["SYN-B"]).quality_status = "fail"
    session.get(Dataset, lab["ids"]["SYN-C"]).is_synthetic = False
    assert engine.compatible_targets(session, ctx, None, 10) == []


def test_correlated_assets_do_not_count_as_independent_evidence():
    assert engine.effective_n(np.eye(3)) == pytest.approx(3.0)
    assert engine.effective_n(np.ones((4, 4))) == pytest.approx(1.0)
    assert engine.effective_n(np.full((4, 4), 0.95) + np.eye(4) * 0.05) < 1.2
    res = fake_res()
    res["cross_asset"] = {"rows": [{"category": "design", "status": "evaluated"}] +
                          [{"category": "same_market", "status": "evaluated", "mean_net": 1e-4}] * 4,
                          "n_effective": 1.1}
    d = report.dimensions(res, {"success": {}}, None, 1e6)
    assert d["cross_asset"]["status"] == "INSUFFICIENT_DATA"
    res["cross_asset"]["n_effective"] = 3.0
    assert report.dimensions(res, {"success": {}}, None, 1e6)["cross_asset"]["status"] == "PASS"


# -- period separation and leakage --------------------------------------------------------------------------
def test_final_period_data_does_not_affect_any_robustness_result(session, lab, cand):  # noqa: F811
    base = ctx_of(session, lab, cand)
    frames = [f.copy() for f in base.frames]
    late = frames[0]["ts"] >= pd.Timestamp(lab["holdout"], tz="UTC")
    assert late.any()
    for col in ("open", "high", "low", "close", "adj_close"):
        if col in frames[0]:
            frames[0].loc[late, col] = frames[0].loc[late, col] * 3.0
    frames[0].loc[late, "volume"] = 0.0
    alt = ctx_of(session, lab, cand, frames=frames)
    noop = lambda: None
    assert engine.temporal(base) == engine.temporal(alt)
    assert engine.parameters(base, noop) == engine.parameters(alt, noop)
    assert engine.execution(base) == engine.execution(alt)
    assert engine.capacity(base, [1e6], 1.0, 1e6) == engine.capacity(alt, [1e6], 1.0, 1e6)
    a, b = engine.stress(base, noop), engine.stress(alt, noop)
    assert a["synthetic"] == b["synthetic"] and a["historical"] == b["historical"]
    hold = pd.Timestamp(lab["holdout"], tz="UTC")
    assert all(pd.Timestamp(t) < hold for t in base.wf["oos"]["ts"])


def test_regime_labels_are_causal(lab, session):  # noqa: F811
    f = lab["store"].read(session.get(Dataset, lab["ids"]["SYN-D"]).path)
    full = engine.regime_labels(f)
    cut = engine.regime_labels(f.iloc[:800])
    assert full.iloc[:800].equals(cut)  # later bars never change an earlier label


# -- scenarios, parameters, costs --------------------------------------------------------------------------
def test_stress_and_cost_scenarios_are_reproducible_and_ordered(session, lab, cand):  # noqa: F811
    ctx = ctx_of(session, lab, cand)
    s1, s2 = engine.stress(ctx, lambda: None), engine.stress(ctx, lambda: None)
    assert s1 == s2
    names = {x["scenario"] for x in s1["synthetic"]}
    assert {"base", "volatility_x2", "price_gaps", "liquidity_drop", "stale_data", "regime_break",
            "correlation_spike", "cost_spike_x5"} <= names
    assert "stress tests, not predictions" in s1["note"]
    ex = engine.execution(ctx)
    sc = ex["scenarios"]
    assert ex == engine.execution(ctx)
    assert sc["base"]["cost_mean"] <= sc["adverse"]["cost_mean"] * 1.0001
    assert sc["base"]["mean_net"] >= sc["severe"]["mean_net"] - 1e-12
    assert sc["adverse"]["delay_bars"] == 1 and sc["severe"]["participation_factor"] == 0.25
    assert ex["not_modelled"] and ex["cost_sources"]


def test_parameter_perturbation_is_local_bounded_and_counted(session, lab, cand):  # noqa: F811
    nb = dict(engine.neighbours({"lookback": 20, "z": 1.5, "long_only": True, "name": "x"}))
    assert nb == {"lookback x0.75": {"lookback": 15, "z": 1.5, "long_only": True, "name": "x"},
                  "lookback x1.25": {"lookback": 25, "z": 1.5, "long_only": True, "name": "x"},
                  "z x0.75": {"lookback": 20, "z": 1.125, "long_only": True, "name": "x"},
                  "z x1.25": {"lookback": 20, "z": 1.875, "long_only": True, "name": "x"}}
    p = engine.parameters(ctx_of(session, lab, cand), lambda: None)
    assert p["n_configs"] == 1 + len(engine.neighbours(p["centre"]))
    assert p["rows"][0]["config"].startswith("centre")


# -- capacity ----------------------------------------------------------------------------------------------
def test_missing_volume_makes_capacity_unknown_not_estimated(session, lab, cand):  # noqa: F811
    base = ctx_of(session, lab, cand)
    frames = [f.copy() for f in base.frames]
    frames[0]["volume"] = np.nan
    ctx = ctx_of(session, lab, cand, frames=frames)
    c = engine.capacity(ctx, [1e6], 1.0, 1e6)
    assert c["status"] == "UNKNOWN" and c["estimate"] is None and "volume" in c["reason"]
    res = fake_res(capacity=c)
    assert report.dimensions(res, {"success": {}}, None, 1e6)["capacity"]["status"] == "INSUFFICIENT_DATA"


def test_insufficient_capacity_fails_and_impact_grows_with_capital(session, lab, cand):  # noqa: F811
    ctx = ctx_of(session, lab, cand)
    c = engine.capacity(ctx, [1e5, 1e7, 1e12], 1.0, 1e6)
    if c["base_mean_net"] > 0:
        lv = c["levels"]
        assert lv[0]["mean_net_after_impact"] >= lv[1]["mean_net_after_impact"] >= lv[2]["mean_net_after_impact"]
        assert lv[2]["trades_over_limit_frac"] > 0.05 and c["estimate"] < 1e12
        assert "not a measurement of real market impact" in c["note"]
    tiny = fake_res(capacity={"status": "APPROXIMATE", "estimate": 1e5})
    assert report.dimensions(tiny, {"success": {}}, None, 1e6)["capacity"]["status"] == "FAIL"


# -- stress: risk controls on bad data --------------------------------------------------------------------
def test_risk_controls_block_simulated_orders_on_bad_data(session, lab, cand):  # noqa: F811
    checks = engine.risk_control_checks(ctx_of(session, lab, cand))
    assert len(checks) == 3 and all(c["passed"] for c in checks)


# -- combinations ----------------------------------------------------------------------------------------
def test_diversification_compares_with_a_reference_portfolio(session, lab, cand):  # noqa: F811
    peer = run_new(session, lab, contract={"title": "Momentum SYN-C", "dataset_ids": [lab["ids"]["SYN-C"]]})
    ctx = ctx_of(session, lab, cand)
    d = engine.diversification(session, ctx, [peer])
    assert {"reference", "combined", "candidate", "corr_with_reference", "corr_on_stress_days", "rolling_corr_126d",
            "active_day_overlap", "candidate_return_in_reference_max_drawdown"} <= set(d)
    assert "not a guarantee" in d["note"]
    none = engine.diversification(session, ctx, [])
    assert none["status_hint"] == "no_peers"
    assert report.dimensions(fake_res(diversification=none), {"success": {}}, None, 1e6)["diversification"]["status"] \
        == "INSUFFICIENT_DATA"


# -- states and criteria ----------------------------------------------------------------------------------
def dims(**over) -> dict:
    d = {k: {"status": "PASS", "reason": "ok"} for k in report.DIMENSIONS}
    for k, v in over.items():
        d[k] = {"status": v, "reason": k}
    return d


def test_state_rules_and_critical_dimensions_are_not_compensated():
    assert report.overall(dims())[0] == "ROBUST_UNDER_TESTED_ASSUMPTIONS"
    for k in report.CORE_FRAGILITY:  # one core failure decides, however good the rest is
        assert report.overall(dims(**{k: "FAIL"}))[0] == "FRAGILE"
    assert report.overall(dims(temporal="FAIL", cross_asset="INSUFFICIENT_DATA"))[0] == "FRAGILE"
    for k in report.ESSENTIAL:
        if k not in report.CORE_FRAGILITY:
            assert report.overall(dims(**{k: "INSUFFICIENT_DATA"}))[0] == "INSUFFICIENT_DATA"
    assert report.overall(dims(stress="FAIL"))[0] == "MIXED_EVIDENCE"
    assert report.overall(dims(cross_asset="FAIL"))[0] == "MIXED_EVIDENCE"
    state, reasons = report.overall(dims(capacity="INSUFFICIENT_DATA"))
    assert state == "ROBUST_UNDER_TESTED_ASSUMPTIONS" and any("capacity" in r for r in reasons)
    assert set(report.CRITERIA) == set(report.DIMENSIONS)


def test_missing_evidence_is_insufficient_data_never_a_pass():
    res = fake_res()
    res["temporal"]["n_windows"] = 2
    res["cross_asset"] = {"rows": [{"category": "design", "status": "evaluated"}], "not_applicable": "pairs"}
    res["parameters"] = {"rows": [{"mean_net": 1e-4}], "n_configs": 1}
    d = report.dimensions(res, {"success": {}}, None, 1e6)
    assert d["temporal"]["status"] == d["cross_asset"]["status"] == d["parameters"]["status"] == "INSUFFICIENT_DATA"
    assert d["uncertainty"]["status"] == "INSUFFICIENT_DATA"  # no lab decision
    assert report.overall(d)[0] == "INSUFFICIENT_DATA"


def fake_res(**over) -> dict:
    r = {"temporal": {"n_windows": 6, "positive_window_frac": 0.8, "positive_year_frac": 0.75, "regime_dependence": []},
         "execution": {"scenarios": {"base": {"bars": 600, "mean_net": 2e-4}, "adverse": {"mean_net": 1e-4},
                                     "severe": {"mean_net": -1e-5}}},
         "cross_asset": {"rows": [{"category": "design", "status": "evaluated"}] +
                         [{"category": "other_market", "status": "evaluated", "mean_net": 1e-4}] * 3, "n_effective": 3},
         "parameters": {"rows": [{"mean_net": 2e-4}], "n_configs": 5, "neighbour_positive_frac": 1.0,
                        "neighbour_sharpe_median": 1.0, "centre_sharpe": 1.2},
         "stress": {"synthetic": [{"scenario": "base", "max_drawdown": -0.1}]},
         "lab_statistics": {"sharpe_ci95": [0.1, 2.0], "dsr": 0.95},
         "capacity": {"status": "APPROXIMATE", "estimate": 1e7},
         "diversification": {"combined": {"sharpe_net": 1.2, "max_drawdown": -0.1},
                             "reference": {"sharpe_net": 1.0, "max_drawdown": -0.12}, "corr_on_stress_days": 0.2}}
    r.update(over)
    return r


# -- job, cancellation, API ------------------------------------------------------------------------------
def test_job_runs_the_evaluation_and_cancellation_is_recorded(session, lab, cand):  # noqa: F811
    job = submit_job(session, service.KIND, cfg(cand).model_dump(mode="json"))
    session.commit()
    run_job(job.id)
    session.expire_all()
    assert session.get(Job, job.id).status == "succeeded"
    stopped = service.run(session, cfg(cand, max_targets=1), store=lab["store"], should_stop=lambda: True)
    assert stopped.status == "cancelled" and stopped.results["state"] == "NOT_EVALUATED"


def test_api_end_to_end(env, lab, session):  # noqa: F811
    from qal.api.main import create_app

    exp = run_new(session, lab)
    session.commit()
    c = TestClient(create_app())
    meta = c.get("/api/v1/robustness/meta").json()
    assert meta["live_trading_enabled"] is False and "INSUFFICIENT_DATA" in meta["states"]
    assert set(meta["criteria"]) == set(meta["dimensions"])
    cands = c.get("/api/v1/robustness/candidates").json()
    assert cands[0]["experiment_id"] == exp.id and cands[0]["robustness_state"] == "NOT_EVALUATED"
    assert c.post("/api/v1/robustness/runs", json={"experiment_id": "nope"}).status_code == 400
    assert c.post("/api/v1/robustness/runs", json={"experiment_id": exp.id, "max_targets": -1}).status_code == 400
    q = c.post("/api/v1/robustness/runs", json={"experiment_id": exp.id, **FAST}).json()
    assert q["job"]["kind"] == service.KIND and q["run"] is None
    r = c.post("/api/v1/robustness/runs", json={"experiment_id": exp.id, **FAST, "run_async": False}).json()
    rid = r["run"]["id"]
    assert r["run"]["state"] in report.STATES and set(r["run"]["dimensions"]) == set(report.DIMENSIONS)
    assert c.get("/api/v1/robustness/runs").json()[0]["id"] == rid
    assert c.get(f"/api/v1/robustness/runs?experiment_id={exp.id}").json()
    detail = c.get(f"/api/v1/robustness/runs/{rid}").json()
    assert detail["results"]["cross_asset"]["rows"]
    assert c.get(f"/api/v1/robustness/runs/{rid}/export.json").status_code == 200
    csv_text = c.get(f"/api/v1/robustness/runs/{rid}/matrix.csv").text
    assert csv_text.splitlines()[0].startswith("category,symbol") and "design" in csv_text
    assert c.get("/api/v1/robustness/runs/nope").status_code == 404
    assert c.get("/api/v1/robustness/candidates").json()[0]["robustness_run_id"] == rid


# -- isolation: no orders, no changes to paper trading or risk policies -------------------------------------
def test_robustness_cannot_place_orders_or_change_paper_trading():
    forbidden_mods = {"requests", "httpx", "urllib", "socket", "websocket", "websockets", "ccxt", "MetaTrader5",
                      "aiohttp", "ib_insync", "alpaca", "oandapyV20", "yfinance"}
    root = pathlib.Path(engine.__file__).parent
    files = [*root.glob("*.py"), root.parent / "api" / "routers" / "robustness.py"]
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            if isinstance(node, ast.ImportFrom) and node.module == "qal.paper":
                assert [a.name for a in node.names] == ["feed"], f"{f.name} imports {node.names}"
            for n in names:
                assert n.split(".")[0] not in forbidden_mods, f"{f.name} imports {n}"
                assert not n.startswith("qal.paper.") and not n.startswith("qal.portfolio"), f"{f.name} imports {n}"
            if isinstance(node, ast.Name):
                assert node.id not in {"PaperOrder", "PaperFill", "PaperSession", "RiskPolicy"}, f"{f.name} uses {node.id}"


def test_dataset_rows_and_files_are_not_modified(session, lab, cand):  # noqa: F811
    before = {d.id: (d.content_hash, d.rows) for d in session.scalars(select(Dataset))}
    service.run(session, cfg(cand), store=lab["store"])
    with db.session_scope() as s2:
        assert {d.id: (d.content_hash, d.rows) for d in s2.scalars(select(Dataset))} == before


def test_historical_episodes_are_distinct_and_windows_carry_their_sample(session, lab, cand):  # noqa: F811
    ctx = ctx_of(session, lab, cand)
    eps = engine._drawdown_episodes(ctx, ctx.wf["oos"]["net"].to_numpy())
    spans = [(e["from"], e["to"]) for e in eps]
    assert len(set(spans)) == len(spans)
    for i, a in enumerate(spans):  # no two episodes overlap
        for b in spans[i + 1:]:
            assert a[1] < b[0] or b[1] < a[0]
    assert all(e["reference_drawdown"] < -0.05 for e in eps)
    t = engine.temporal(ctx)
    assert sum(w["bars"] for w in t["windows"]) == len(ctx.wf["oos"])
    assert all(w["sharpe_se"] is not None and w["sharpe_se"] > 0 for w in t["windows"])


def test_stress_scenarios_keep_the_dividend_adjustment(syn):
    """A frozen or rebuilt bar must not lose adj_close / close: on an asset with large distributions that created
    fake jumps (a -99% drawdown under stale_data on TLT)."""
    f = syn["SYN-A"].copy()
    factor = np.linspace(0.6, 1.0, len(f))  # distributions paid over the period, as on a bond ETF
    f["adj_close"] = f["close"] * factor
    idx = np.arange(300, 900)
    for name, (_, frames) in engine.stress_frames([f], idx).items():
        if frames is None:
            continue
        g = frames[0]
        got = (g["adj_close"] / g["close"]).to_numpy()
        frozen = (g["volume"] == 0) & (f["volume"] != 0) if name == "stale_data" else np.zeros(len(f), bool)
        expected = np.where(frozen, np.roll(factor, 1), factor)
        assert np.allclose(got, expected), name
    stale = engine.stress_frames([f], idx)["stale_data"][1][0]
    r = np.diff(np.log(stale["adj_close"].to_numpy(float)))
    base = np.diff(np.log(f["adj_close"].to_numpy(float)))
    assert np.abs(r).max() <= np.abs(base).max() * 2 + 1e-9  # no artificial jump
    vol2 = engine.stress_frames([f], idx)["volatility_x2"][1][0]
    assert np.allclose(np.diff(np.log(vol2["adj_close"].to_numpy(float))), 2 * base)
