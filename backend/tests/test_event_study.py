"""Event study v2: methodology regression tests (look-ahead, overlap, purging, seeds, BH, costs, pairs)."""

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from qal.anomaly.event_study import CostScenario, evaluate
from qal.data.schema import InstrumentSpec, normalize_bars


def _frame(returns, delay=timedelta(0), start="2000-01-03", symbol="T"):
    idx = pd.date_range(start, periods=len(returns), freq="D", tz="UTC")
    close = 100 * np.exp(np.cumsum(returns))
    open_ = np.r_[100.0, close[:-1]]
    raw = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.001,
                        "low": np.minimum(open_, close) * 0.999, "close": close, "volume": 1e6}, index=idx)
    df, _ = normalize_bars(raw, provider="test", spec=InstrumentSpec(symbol, "fx"), frequency="1d",
                           availability_delay=delay, is_synthetic=False, source_ref="test")
    return df


def _shocks(n=6000, every=60, drift_after=0.0, seed=0, size=0.08, positive_only=False, drift=0.0):
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, 0.005, n)
    for t in range(300, n - 25, every):
        s = size if positive_only or rng.random() < 0.5 else -size
        r[t] += s
        r[t + 1:t + 6] += np.sign(s) * drift_after
    return r


H = [1, 5, 10, 20]


def test_bootstrap_is_reproducible_and_rows_do_not_depend_on_each_other():
    df = _frame(_shocks(drift_after=0.002))
    a = evaluate(df, "return_shock", {}, H, "fx", seed=7)
    b = evaluate(df, "return_shock", {}, H, "fx", seed=7)
    assert a["table"] == b["table"] and a["out_of_sample"] == b["out_of_sample"]
    only5 = evaluate(df, "return_shock", {}, [5], "fx", seed=7)
    row = lambda res: next(r for r in res["table"] if r["horizon"] == 5 and r["rule"] == "continuation")  # noqa: E731
    assert row(only5)["all"]["ci95_excess_bps"] == row(a)["all"]["ci95_excess_bps"]
    other = evaluate(df, "return_shock", {}, H, "fx", seed=8)
    assert row(other)["all"]["mean_excess_bps"] == row(a)["all"]["mean_excess_bps"]  # seeds only move the CIs


def test_overlapping_events_are_not_counted_as_independent():
    r = _shocks(every=4, n=3000)  # a shock every 4 bars, horizons up to 20
    res = evaluate(_frame(r), "return_shock", {"threshold": 4.0}, [1, 20], "fx")
    r20 = next(x for x in res["table"] if x["horizon"] == 20)
    r1 = next(x for x in res["table"] if x["horizon"] == 1)
    assert r20["n_independent"] < r1["n_independent"] <= r1["n_events_total"]
    assert r20["n_independent"] <= 3000 // 20 + 1


def test_in_sample_trades_are_purged_at_the_split_and_ignore_the_future():
    r = _shocks(drift_after=0.003)
    base = evaluate(_frame(r), "return_shock", {}, H, "fx")
    for row in base["table"]:
        assert row["n_in_sample"] + row["n_out_of_sample"] + row["n_purged"] == row["n_independent"]
    # Poison everything after the split: in-sample statistics and the selected rule must not change.
    split = pd.Timestamp(base["split_ts"]).tz_localize("UTC")
    df = _frame(r)
    poisoned = df.copy()
    late = poisoned["ts"] > split + pd.Timedelta(days=40)
    rng = np.random.default_rng(1)
    factor = np.exp(np.cumsum(rng.normal(0, 0.05, int(late.sum()))))
    for c in ("open", "high", "low", "close"):
        poisoned.loc[late, c] = poisoned.loc[late, c].to_numpy() * factor
    # With the split fixed in advance, the later data cannot move it or anything selected before it.
    fixed = evaluate(df, "return_shock", {}, H, "fx", split_ts=split)
    p = evaluate(poisoned, "return_shock", {}, H, "fx", split_ts=split)
    assert p["split_rule"] == "pre-registered"
    assert [x["in_sample"]["mean_excess_bps"] for x in p["table"]] == \
           [x["in_sample"]["mean_excess_bps"] for x in fixed["table"]]
    assert p["selected"] == fixed["selected"]


def test_drift_alone_is_not_predictive_information():
    # Every shock is positive and the series trends up: continuation earns the drift, not information.
    r = _shocks(n=12000, every=120, size=0.035, positive_only=True, drift=0.002, drift_after=0.0)
    res = evaluate(_frame(r), "return_shock", {}, H, "fx")
    row = next(x for x in res["table"] if x["horizon"] == 20 and x["rule"] == "continuation")
    assert row["all"]["mean_gross_bps"] > 20  # looks profitable gross...
    assert abs(row["all"]["mean_excess_bps"]) < row["all"]["mean_gross_bps"] / 2  # ...but it is the drift
    assert res["verdict"] != "oos_supported"


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_noise_does_not_produce_oos_support(seed):
    r = np.random.default_rng(seed).standard_t(4, 6000) * 0.006
    res = evaluate(_frame(r), "return_shock", {"threshold": 3.0}, H, "fx", seed=seed)
    assert res["verdict"] in ("no_evidence", "insufficient_sample", "in_sample_only")
    adj = [x["in_sample"]["adjusted_pvalue"] for x in res["table"]]
    raw = [x["in_sample"]["pvalue"] or 1.0 for x in res["table"]]
    assert all(a >= p - 1e-12 for a, p in zip(adj, raw, strict=True))


def test_cost_scenarios_and_missing_historical_spread():
    df = _frame(_shocks(drift_after=0.003))
    spread = pd.Series(1.5, index=pd.DatetimeIndex(df["ts"]))  # 1.5 bps quoted spread
    spread.iloc[::7] = np.nan  # gaps in the spread history
    res = evaluate(df, "return_shock", {}, H, "fx", spread_bps=spread)
    names = [s["name"] for s in res["cost_scenarios"]]
    assert names == ["zero_cost", "low_estimate", "base_estimate", "high_estimate", "historical_spread"]
    row = res["table"][0]
    costs = {k: v["mean_cost_bps"] for k, v in row["all"]["by_cost"].items()}
    assert costs["zero_cost"] == 0 < costs["low_estimate"] < costs["base_estimate"] < costs["high_estimate"]
    assert row["spread_missing_bars"] > 0  # counted, filled with the scenario's fixed half spread
    assert set(res["survives_costs_oos"]) == set(names)
    custom = evaluate(df, "return_shock", {}, [5], "fx",
                      cost_scenarios=[CostScenario("broker_quote", 0.0, 0.4, 0.2)], base_scenario="broker_quote")
    assert list(custom["table"][0]["all"]["by_cost"]) == ["broker_quote"]


def test_pair_reversion_is_measured_on_the_relationship_with_two_legs_of_cost():
    rng = np.random.default_rng(3)
    n = 4000
    rx = rng.normal(0, 0.006, n)
    spread = np.zeros(n)
    for t in range(1, n):
        spread[t] = 0.9 * spread[t - 1] + rng.normal(0, 0.002)
    for t in range(400, n - 30, 70):  # divergences that close over the next bars
        spread[t:t + 25] += 0.05 * 0.8 ** np.arange(25)
    ly = 0.8 * np.cumsum(rx) + spread
    x = _frame(rx, symbol="X")
    y = _frame(np.r_[ly[0], np.diff(ly)], symbol="Y")
    res = evaluate(y, "pair_divergence", {}, H, "fx", other=x)
    assert res["position"].startswith("spread") and res["rules"] == ["reversion", "continuation"]
    assert res["verdict"] == "oos_supported", res["verdict_text"]
    assert res["selected"]["rule"] == "reversion"
    single = evaluate(y, "return_shock", {"threshold": 3.0}, [5], "fx")
    pair_cost = next(r for r in res["table"] if r["horizon"] == 5)["all"]["by_cost"]["zero_cost"]["mean_cost_bps"]
    assert pair_cost == 0
    pc = next(r for r in res["table"] if r["horizon"] == 5)["all"]["by_cost"]["base_estimate"]["mean_cost_bps"]
    sc = single["table"][0]["all"]["by_cost"]["base_estimate"]["mean_cost_bps"] if single["table"] else 0
    assert pc > 0 and (not sc or pc > 0.5 * sc)
    with pytest.raises(ValueError):
        evaluate(y, "pair_divergence", {}, H, "fx")


def test_volatility_events_use_returns_not_the_volatility_level():
    r = np.random.default_rng(5).normal(0, 0.004, 5000)
    for t in range(600, 4900, 300):
        r[t:t + 40] *= 4  # volatility regimes with no directional follow-through
    res = evaluate(_frame(r), "volatility_regime", {}, H, "fx")
    for row in res["table"]:
        assert abs(row["all"].get("mean_gross_bps", 0)) < 400  # forward returns, not a vol level in bps
    assert res["verdict"] != "oos_supported"
