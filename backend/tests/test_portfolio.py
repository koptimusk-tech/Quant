"""Phase 6: portfolio construction, risk engine, hierarchical risk manager, sizing, stress and the portfolio
backtest. Synthetic data with known structure; every synthetic result is labelled as such."""

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from qal.portfolio import backtest as bt
from qal.portfolio import construction, sizing, stress
from qal.portfolio.risk import (
    correlations,
    covariance,
    diversification,
    exposures,
    risk_contributions,
)
from qal.portfolio.risk_manager import HRM, Context, PolicyLimits, StrategyState
from qal.portfolio.strategies import (
    EligibilityCriteria,
    check_eligibility,
    synthetic_strategies,
)


def _corr_returns(rho: float, n: int = 4000, seed: int = 0, vols=(0.01, 0.02, 0.01)) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    z = rng.standard_normal((n, 3))
    a = z[:, 0]
    b = rho * z[:, 0] + np.sqrt(1 - rho**2) * z[:, 1]
    c = z[:, 2]
    idx = pd.bdate_range("2010-01-01", periods=n, tz="UTC")
    return pd.DataFrame({"A": a * vols[0], "B": b * vols[1], "C": c * vols[2]}, index=idx)


@pytest.fixture(scope="module")
def syn():
    recs, series = synthetic_strategies()
    for r in recs:
        check_eligibility(r, EligibilityCriteria(allow_synthetic=True))
    return {r.id: r for r in recs}, series


# -- risk engine ------------------------------------------------------------------------------------
def test_known_correlations_and_covariance_methods():
    R = _corr_returns(0.8)
    c = correlations(R)
    M = np.array(c["static"])
    assert M[0, 1] == pytest.approx(0.8, abs=0.03) and abs(M[0, 2]) < 0.05
    S = covariance(R, "sample")
    assert np.sqrt(S.loc["B", "B"]) == pytest.approx(0.02 * np.sqrt(252), rel=0.05)
    lw = covariance(R, "ledoit_wolf")
    ew = covariance(R, "ewma", halflife=60)
    assert np.allclose(lw.to_numpy(), S.to_numpy(), rtol=0.1, atol=1e-4)  # many observations: little shrinkage
    assert np.all(np.linalg.eigvalsh(ew.to_numpy()) > 0)
    with pytest.raises(ValueError, match="common observations"):
        covariance(R.iloc[:5])


def test_risk_contributions_sum_to_vol_and_risk_budget_equalises():
    R = _corr_returns(0.5)
    C = covariance(R, "sample")
    w = construction.risk_budget(C)
    rc = risk_contributions(w, C)
    assert sum(rc["contrib"]) == pytest.approx(rc["vol"], rel=1e-9)
    assert np.allclose(rc["pct"], 1 / 3, atol=1e-6)
    w2 = construction.risk_budget(C, np.array([2.0, 1.0, 1.0]))
    pct = risk_contributions(w2, C)["pct"]
    assert pct[0] == pytest.approx(0.5, abs=1e-5) and pct[1] == pytest.approx(0.25, abs=1e-5)
    iv = construction.inverse_vol(C)
    vols = np.sqrt(np.diag(C))
    assert np.allclose(iv * vols, (iv * vols)[0])
    d = diversification(w, C)
    assert d["diversification_ratio"] > 1 and 1 < d["effective_number_of_bets"] <= 3


def test_caps_redistribute_and_mean_variance_is_optional():
    w = construction.apply_caps(np.array([0.7, 0.2, 0.1]), 0.4)
    assert w.max() <= 0.4 + 1e-12 and w.sum() == pytest.approx(1.0)
    assert construction.apply_caps(np.array([1.0, 1.0]), 0.3).sum() == pytest.approx(0.6)  # rest stays cash
    R = _corr_returns(0.2)
    C = covariance(R)
    mv = construction.mean_variance(R, C, cap=0.6)
    assert mv.sum() == pytest.approx(1.0, abs=1e-6) and mv.max() <= 0.6 + 1e-9
    with pytest.raises(ValueError, match="unknown method"):
        construction.build("kelly", R, C)


def test_exposures_look_through_and_currency_legs():
    ex = exposures({"s1": 0.5, "s2": 0.5}, {"s1": {"EURUSD": 1.0}, "s2": {"GBPUSD": 1.0, "USDJPY": -0.4}},
                   {"EURUSD": "fx", "GBPUSD": "fx", "USDJPY": "fx"})
    assert ex["currency_net"]["USD"] == pytest.approx(-1.2)  # short USD three ways
    assert ex["instrument"]["USDJPY"] == pytest.approx(-0.2) and ex["gross"] == pytest.approx(1.2)


def test_regime_and_stress_correlations_rise_in_synthetic_stress(syn):
    _, series = syn
    R = pd.DataFrame({s: series[s].net for s in ("SYN:carry", "SYN:vol-short")}).dropna()
    c = correlations(R)
    assert c["stress"]["mean_pairwise"] > c["regime"]["low_vol"]["mean_pairwise"] + 0.2
    assert c["regime"]["high_vol"]["mean_pairwise"] > c["regime"]["low_vol"]["mean_pairwise"]
    assert c["rolling"]["max"] > c["static_mean_pairwise"] + 0.3  # unstable: static hides the stress episode


# -- hierarchical risk manager ----------------------------------------------------------------------
PAIR = {"a": "EURUSD", "b": "GBPUSD", "c": "AUDUSD"}


def _ctx(**over):
    ids = ["a", "b", "c"]
    C = pd.DataFrame(np.diag([0.01, 0.01, 0.01]), index=ids, columns=ids)
    st = {s: StrategyState(group="fx", counterparty="unassigned", quality={"X": "pass"},
                           last_available=pd.Timestamp("2020-01-10", tz="UTC"), history_days=500, drawdown=-0.05,
                           exposure={PAIR[s]: 1.0}) for s in ids}
    base = dict(as_of=pd.Timestamp("2020-01-10", tz="UTC"), strategies=st, cov=C,
                asset_class={p: "fx" for p in PAIR.values()}, dollar_volume={},
                max_participation={}, capital=1e6)
    base.update(over)
    return Context(**base)


def test_hrm_decisions_carry_rule_observed_limit_reason_and_version():
    hrm = HRM(PolicyLimits(), "default@v1#abc")
    w, dec = hrm.enforce({"a": 0.6, "b": 0.2, "c": 0.2}, _ctx())
    assert w["a"] <= 0.35 + 1e-12
    cap = next(d for d in dec if d["rule"] == "max_weight" and d["scope"] == "a")
    assert cap["decision"] == "blocked" and cap["observed"] == 0.6 and cap["limit"] == 0.35
    assert cap["policy_version"] == "default@v1#abc" and cap["reason"] and cap["ts"].startswith("2020-01-10")
    assert {d["level"] for d in dec} == {"strategy", "group", "portfolio"}
    usd = [d for d in dec if d["scope"] == "currency:USD" and d["rule"] == "max_currency_net"]
    # short USD across three pairs is limited at the currency level, then re-checked
    assert usd[0]["decision"] == "blocked" and usd[0]["observed"] == pytest.approx(0.75)
    assert usd[-1]["decision"] == "approved" and usd[-1]["observed"] <= 0.6 + 1e-6
    assert sum(w.values()) <= PolicyLimits().portfolio.max_leverage


def test_hrm_blocks_bad_quality_stale_short_history_and_drawdown():
    ctx = _ctx()
    ctx.strategies["a"].quality = {"X": "fail"}
    ctx.strategies["b"].last_available = pd.Timestamp("2019-12-01", tz="UTC")
    ctx.strategies["c"].history_days = 30
    w, dec = HRM(PolicyLimits(), "v").enforce({"a": 0.3, "b": 0.3, "c": 0.3}, ctx)
    assert w == {"a": 0.0, "b": 0.0, "c": 0.0}
    rules = {(d["scope"], d["rule"]) for d in dec if d["decision"] == "blocked"}
    assert {("a", "data_quality"), ("b", "data_staleness_days"), ("c", "min_history_days")} <= rules
    ctx = _ctx()
    ctx.strategies["a"].drawdown = -0.4
    w, _ = HRM(PolicyLimits(), "v").enforce({"a": 0.3, "b": 0.3, "c": 0.3}, ctx)
    assert w["a"] == 0.0 and w["b"] > 0


def test_hrm_limits_leverage_concentration_liquidity_and_execution_failures():
    L = PolicyLimits.model_validate({"portfolio": {"max_leverage": 0.5, "max_risk_share": 0.4},
                                     "group": {"max_currency_net": 5, "max_instrument_gross": 5}})
    ids = ["a", "b", "c"]
    C = pd.DataFrame(np.diag([0.04, 0.01, 0.01]), index=ids, columns=ids)
    w, dec = HRM(L, "v").enforce({"a": 0.3, "b": 0.3, "c": 0.3}, _ctx(cov=C))
    assert sum(w.values()) == pytest.approx(0.5)
    assert max(risk_contributions(np.array([w[s] for s in ids]), C)["pct"]) <= 0.4 + 1e-6
    # liquidity: tiny traded volume scales everything down; unknown volume is reported, not assumed infinite
    ctx = _ctx(dollar_volume={"EURUSD": 1e6, "GBPUSD": None}, max_participation={"EURUSD": 0.05})
    w, dec = HRM(PolicyLimits(), "v").enforce({"a": 0.3, "b": 0.3, "c": 0.3}, ctx)
    liq = [d for d in dec if d["rule"] == "min_capacity_multiple"]
    assert any(d["decision"] == "blocked" for d in liq) and any("not evaluable" in d["reason"] for d in liq)
    ctx = _ctx(execution_failure_frac=0.5, previous={"a": 0.1, "b": 0.1, "c": 0.1})
    w, dec = HRM(PolicyLimits(), "v").enforce({"a": 0.3, "b": 0.3, "c": 0.3}, ctx)
    assert w == {"a": 0.1, "b": 0.1, "c": 0.1} and dec[0]["rule"] == "execution_failures"


# -- sizing -----------------------------------------------------------------------------------------
def test_sizing_rounds_to_lots_and_respects_contract():
    spec = sizing.default_contract("fx", "EURUSD")
    q = sizing.to_quantity(0.123456, capital=1e6, price=1.1, spec=spec)
    assert q["lots"] == pytest.approx(1.12) and abs(q["rounding_residual"]) < 0.01 * 1.1 * 1e5
    assert sizing.to_quantity(1e-5, capital=1e6, price=1.1, spec=spec)["below_min_lot"]
    assert sizing.default_contract("fx", "USDJPY").tick_size == 0.001
    assert not sizing.to_quantity(40, capital=1e6, price=1.1, spec=spec)["leverage_ok"]
    meta = {"contract": {"multiplier": 50, "lot_size": 1, "min_lot": 1, "tick_size": 0.25, "max_leverage": 5}}
    assert sizing.contract_for(meta, "future", "ES").source == "instrument metadata"


def test_vol_sizing_uses_only_past_returns_and_stops_do_not_cap_losses():
    idx = pd.bdate_range("2020-01-01", periods=200, tz="UTC")
    r = pd.Series(np.random.default_rng(1).normal(0, 0.01, 200), index=idx)
    as_of = idx[150]
    v1 = sizing.trailing_vol(r, as_of)
    r2 = r.copy()
    r2.iloc[150:] = 0.2  # a future shock must not change today's size
    assert sizing.trailing_vol(r2, as_of) == v1
    t = sizing.target_exposure("vol_target", capital=1e6, vol=v1, target_vol=0.1)
    assert t["exposure"] == pytest.approx(0.1 / v1)
    sl = sizing.stop_loss_risk(1.0, capital=1e6, stop_distance=0.01, returns=pd.Series([-0.05, 0.01]))
    assert sl["loss_if_gap"] > sl["loss_at_stop"] and "does not guarantee" in sl["note"]


# -- stress -----------------------------------------------------------------------------------------
def test_stress_scenarios_and_reproducible_monte_carlo():
    R = _corr_returns(0.2, n=1500)
    w = {"A": 0.4, "B": 0.3, "C": 0.3}
    hyp = {s["name"]: s for s in stress.hypothetical(w, R, {"A": {"EURUSD": 1}, "B": {"USDCHF": -1}}, {"EURUSD": "fx",
                                                                                                  "USDCHF": "fx"}, {})}
    base = hyp["baseline 1-day VaR 99% (normal, trailing covariance)"]["pnl"]
    assert hyp["diversification disappears (all correlations 0.9)"]["pnl"] < base
    assert hyp["volatility x2"]["pnl"] == pytest.approx(2 * base)
    assert "CHF 15% gap (SNB 2015-like)" in hyp and all(s["kind"] == "hypothetical" for s in hyp.values())
    a = stress.monte_carlo(w, R, n_paths=300, seed=3)
    b = stress.monte_carlo(w, R, n_paths=300, seed=3)
    assert a == b and a["kind"] == "synthetic" and "Not a forecast" in a["note"]
    assert a["daily"]["es99"] <= a["daily"]["var99"] <= a["daily"]["var95"] < 0
    port = pd.Series([0.0, -0.01, -0.02, 0.03, 0.0], index=pd.bdate_range("2020-01-01", periods=5, tz="UTC"))
    worst = {s["name"]: s for s in stress.historical(port)}
    assert worst["worst 1-day window"]["pnl"] == pytest.approx(-0.02)
    assert worst["worst 1-day window"]["kind"] == "historical"


# -- portfolio backtest -----------------------------------------------------------------------------
def _panel(syn, ids=None):
    recs, series = syn
    ids = ids or [s for s in recs if recs[s].eligible]
    return bt.make_panel(series, recs, ids), recs, ids


def test_backtest_has_no_lookahead_and_executes_after_the_decision(syn):
    panel, recs, ids = _panel(syn)
    cfg = bt.PortfolioConfig(strategy_ids=ids, execution_delay_days=1)
    full = bt.run(panel, recs, cfg, "risk_budget", PolicyLimits(), "v")
    chk = bt.lookahead_check(panel, recs, cfg, "risk_budget", PolicyLimits(), "v", full)
    assert chk["passed"] and chk["decisions_compared"] > 20
    assert all(ex >= dec + 2 for dec, ex in full["executed"])
    # Changing returns after a date leaves every earlier decision unchanged.
    cut = panel.dates[1000]
    panel2, _, _ = _panel(syn)
    panel2.net.loc[panel2.dates > cut] *= -3
    other = bt.run(panel2, recs, cfg, "risk_budget", PolicyLimits(), "v")
    before = lambda o: [x["final"] for x in o["weights"] if pd.Timestamp(x["ts"], tz="UTC") <= cut]
    assert before(full) == before(other)


def test_incomplete_history_gets_no_allocation_until_enough_data(syn):
    panel, recs, ids = _panel(syn)
    out = bt.run(panel, recs, bt.PortfolioConfig(strategy_ids=ids), "equal_weight", PolicyLimits(), "v")
    start = pd.Timestamp(recs["SYN:late"].history["start"])
    early = [x for x in out["weights"] if pd.Timestamp(x["ts"], tz="UTC") < start + pd.Timedelta(days=200)]
    assert early and all(x["final"]["SYN:late"] == 0.0 for x in early)
    assert out["weights"][-1]["final"]["SYN:late"] > 0


def test_rebalancing_costs_scale_with_frequency_and_multiplier(syn):
    panel, recs, ids = _panel(syn)
    cfg = bt.PortfolioConfig(strategy_ids=ids)
    monthly = bt.run(panel, recs, cfg, "inverse_vol", PolicyLimits(), "v")["bars"]
    weekly = bt.run(panel, recs, cfg.model_copy(update={"rebalance": "weekly"}), "inverse_vol", PolicyLimits(), "v")["bars"]
    assert weekly["rebalance_cost"].sum() > monthly["rebalance_cost"].sum() > 0
    dear = bt.run(panel, recs, cfg.model_copy(update={"cost_multiplier": 3.0}), "inverse_vol", PolicyLimits(), "v")["bars"]
    assert (1 + dear["net"]).prod() < (1 + monthly["net"]).prod()
    assert np.allclose(monthly["gross"] - monthly["cost"], monthly["net"])


def test_drawdown_limit_derisks_and_is_recorded(syn):
    panel, recs, ids = _panel(syn)
    L = PolicyLimits.model_validate({"portfolio": {"max_drawdown": 0.02, "drawdown_action": "flatten",
                                                   "cooldown_days": 5}})
    out = bt.run(panel, recs, bt.PortfolioConfig(strategy_ids=ids), "equal_weight", L, "v")
    blocks = [d for d in out["daily_blocks"] if d["rule"] == "max_drawdown"]
    assert blocks and blocks[0]["decision"] == "blocked" and blocks[0]["observed"] < -0.02
    day = pd.Timestamp(blocks[0]["ts"])
    nxt = out["bars"][out["bars"]["ts"] > day].iloc[0]
    assert nxt["leverage"] == 0.0


# -- service, API, agents ---------------------------------------------------------------------------
@pytest.fixture()
def client(env):
    from qal.api.main import create_app

    with TestClient(create_app()) as c:
        yield c


FAST = {"methods": ["risk_budget", "equal_weight"], "mc_paths": 200, "stress_backtests": False, "run_async": False}


def test_portfolio_run_via_api_is_stored_audited_and_reproducible(client):
    ids = [r.id for r in synthetic_strategies()[0]]
    body = {"strategy_ids": ids, "criteria": {"allow_synthetic": True}, **FAST}
    r = client.post("/api/v1/portfolio/runs", json=body)
    assert r.status_code == 202, r.text
    run = client.get(f"/api/v1/portfolio/runs/{r.json()['experiment']['id']}").json()
    res = run["results"]
    assert run["status"] == "succeeded" and res["is_synthetic"] and "SYNTHETIC" in res["synthetic_note"]
    assert [x["id"] for x in res["rejected"]] == ["SYN:weak"] and res["rejected"][0]["reasons"]
    assert all(c["passed"] for c in res["leakage_checks"])
    assert res["risk"]["basis"].startswith("estimate") and res["historical_risk"]["basis"].startswith("historical")
    assert sum(res["risk"]["risk_contributions"].values()) == pytest.approx(1.0, abs=1e-6)
    assert {s["kind"] for s in res["stress"]["hypothetical"]} == {"hypothetical"}
    assert res["stress"]["monte_carlo"][0]["kind"] == "synthetic"
    assert [c["multiplier"] for c in res["cost_sensitivity"]] == [0.0, 1.0, 2.0, 3.0]
    dec = client.get(f"/api/v1/portfolio/runs/{run['id']}/decisions", params={"decision": "blocked"}).json()["items"]
    assert dec and all(d["policy_version"].startswith("default@v1") and d["reason"] for d in dec)
    again = client.post("/api/v1/portfolio/runs", json=body).json()["experiment"]["id"]
    res2 = client.get(f"/api/v1/portfolio/runs/{again}").json()["results"]
    assert res2["primary"]["metrics"] == res["primary"]["metrics"]
    assert res2["methods"]["equal_weight"]["metrics"] == res["methods"]["equal_weight"]["metrics"]
    audit = client.get("/api/v1/audit").json()
    assert any(a["action"] == "portfolio_run_finished" for a in audit)


def test_no_eligible_strategy_is_a_recorded_failure(client):
    r = client.post("/api/v1/portfolio/runs", json={"strategy_ids": ["SYN:trend-eur"], **FAST})
    assert r.status_code == 202
    run = client.get(f"/api/v1/portfolio/runs/{r.json()['experiment']['id']}").json()
    assert run["status"] == "failed" and "no eligible" in run["error"]
    assert "synthetic data" in run["results"]["rejected"][0]["reasons"][0]


def test_policies_are_versioned_and_only_people_change_them(client):
    active = client.get("/api/v1/portfolio/policies/active").json()
    assert active["version"] == 1 and active["active"]
    limits = active["limits"]
    limits["strategy"]["max_weight"] = 0.25
    r = client.post("/api/v1/portfolio/policies", json={"limits": limits, "approved_by": "agent-x",
                                                        "actor_kind": "agent", "confirm": True})
    assert r.status_code == 403
    r = client.post("/api/v1/portfolio/policies", json={"limits": limits, "approved_by": "ana", "confirm": False})
    assert r.status_code == 403
    r = client.post("/api/v1/portfolio/policies", json={"limits": limits, "approved_by": "ana", "confirm": True})
    assert r.status_code == 201 and r.json()["version"] == 2 and r.json()["limits"]["strategy"]["max_weight"] == 0.25
    pols = client.get("/api/v1/portfolio/policies").json()
    assert [p["active"] for p in pols] == [True, False]
    assert any(a["action"] == "risk_policy_change_refused" for a in client.get("/api/v1/audit").json())


def test_agent_proposals_are_computed_and_decided_by_the_risk_manager(client):
    r = client.post("/api/v1/portfolio/proposals", json={"include_synthetic": True, "limits": {"x": 1},
                                                         "run_async": False})
    assert r.status_code == 403
    r = client.post("/api/v1/portfolio/proposals", json={"include_synthetic": True, "mc_paths": 100,
                                                         "run_async": False})
    assert r.status_code == 202, r.text
    p = client.get(f"/api/v1/portfolio/proposals/{r.json()['experiment']['id']}").json()
    keys = [x["key"] for x in p["results"]["proposals"]]
    assert keys[0] == "all-risk-budget" and "drop-redundant" in keys
    drop = next(x for x in p["results"]["proposals"] if x["key"] == "drop-redundant")
    assert len({"SYN:trend-eur", "SYN:trend-gbp"} & set(drop["strategies"])) == 1
    kinds = [m["kind"] for m in p["messages"]]
    assert "objection" in kinds and kinds.count("proposal") == kinds.count("decision") == len(keys)
    decisions = [m for m in p["messages"] if m["kind"] == "decision"]
    assert all(m["sender"] == "risk_manager" and "human decision" in m["payload"]["note"] for m in decisions)


def test_real_hypotheses_are_rejected_with_reasons_and_series_are_walk_forward(client, env):
    from qal import db
    from qal.models import Hypothesis
    from qal.portfolio.strategies import catalog, record_from_hypothesis, series_for
    from tests.test_research import _ingest, contract

    ds = _ingest(client)
    hid = client.post("/api/v1/hypotheses", json={"contract": contract(ds)}).json()["id"]
    assert client.post(f"/api/v1/hypotheses/{hid}/validate", json={}).status_code in (200, 202)
    lst = client.post("/api/v1/portfolio/strategies").json()
    rec = next(x for x in lst if x["id"] == f"H:{hid}")
    assert rec["eligible"] is False and rec["reasons"] and rec["is_synthetic"]
    with db.session_scope() as s:
        r = record_from_hypothesis(s, s.get(Hypothesis, hid))
        ser = series_for(s, r, env.data_dir)
        exp = s.get(Hypothesis, hid)
        assert len(ser.net) > 50 and list(ser.exposure.columns) == r.universe
        assert ser.net.index.is_monotonic_increasing
        assert not catalog(s, EligibilityCriteria())[0].eligible and exp is not None


def test_marginal_analysis_flags_the_redundant_pair(syn):
    from qal.portfolio.risk import marginal_analysis

    _, series = syn
    ids = ["SYN:trend-eur", "SYN:trend-gbp", "SYN:carry", "SYN:meanrev"]
    R = pd.DataFrame({s: series[s].net for s in ids}).dropna()
    C = covariance(R)
    rows = {m["strategy"]: m for m in marginal_analysis(R, C, construction.equal_weight(4))}
    assert rows["SYN:trend-eur"]["redundant"] and rows["SYN:trend-eur"]["most_correlated_with"] == "SYN:trend-gbp"
    assert rows["SYN:trend-gbp"]["adds_risk_without_diversifying"]
    assert not rows["SYN:meanrev"]["redundant"]
