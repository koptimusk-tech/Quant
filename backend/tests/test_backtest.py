import numpy as np
import pandas as pd
import pytest

from qal.research.backtest import run_backtest
from qal.research.costs import CostModel
from qal.research.metrics import deflated_sharpe, probability_of_backtest_overfitting
from qal.research.strategies import STRATEGIES
from qal.research.validation import purged_kfold, windows

ZERO = CostModel(commission_bps=0, half_spread_bps=0, slippage_bps=0, slippage_range_frac=0, borrow_bps_annual=0,
                 max_participation=1.0)


def frame(closes, opens=None, volume=1e9):
    n = len(closes)
    ts = pd.bdate_range("2024-01-01", periods=n, tz="UTC")
    c = np.asarray(closes, float)
    o = np.asarray(opens if opens is not None else c, float)
    return pd.DataFrame({"ts": ts, "event_ts": ts + pd.Timedelta(days=1), "available_ts": ts + pd.Timedelta(days=1),
                         "open": o, "high": np.maximum(o, c) * 1.001, "low": np.minimum(o, c) * 0.999, "close": c,
                         "adj_close": np.nan, "volume": volume, "frequency": "1d", "is_synthetic": True})


def test_execution_happens_next_open_and_pnl_is_exact():
    # Decision at bar 1 close; fill at bar 2 open (=110); bar 2 closes 121; overnight into bar 3: 121 -> 133.1.
    df = frame([100, 100, 121, 133.1, 133.1], opens=[100, 100, 110, 133.1, 133.1])
    w = pd.DataFrame({0: [0, 1, 1, 0, 0]})
    r = run_backtest([df], w, [ZERO])
    assert r.positions["0"].tolist() == [0, 0, 1, 1, 0]
    assert r.bars["gross"].iloc[1] == 0  # the jump from 100 to 110 overnight is not captured
    assert r.bars["gross"].iloc[2] == pytest.approx(121 / 110 - 1)
    assert r.bars["gross"].iloc[3] == pytest.approx(133.1 / 121 - 1)


def test_costs_reduce_returns_and_scale_with_multiplier():
    rng = np.random.default_rng(0)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 300)))
    df = frame(c)
    w = pd.DataFrame({0: np.sign(rng.normal(size=300))})
    cm = CostModel(commission_bps=5, half_spread_bps=5, slippage_bps=5, max_participation=1.0)
    r1 = run_backtest([df], w, [cm])
    r2 = run_backtest([df], w, [cm], cost_multiplier=2.0)
    r0 = run_backtest([df], w, [cm], cost_multiplier=0.0)
    assert r0.bars["cost"].sum() == 0
    assert r1.bars["net"].sum() < r1.bars["gross"].sum()
    assert r2.bars["cost"].sum() == pytest.approx(2 * r1.bars["cost"].sum(), rel=0.02)
    assert (r1.bars["gross"] == r0.bars["gross"]).all()


def test_short_borrow_cost_accrues():
    df = frame([100] * 253)
    w = pd.DataFrame({0: [-1.0] * 253})
    r = run_backtest([df], w, [ZERO.model_copy(update={"borrow_bps_annual": 100})])
    assert r.bars["cost"].sum() == pytest.approx(0.01 * 252 / 252, rel=1e-6)  # held on bars 1..252 = one year


def test_liquidity_limits_clip_orders():
    df = frame([100] * 10, volume=100)  # $10k per bar
    w = pd.DataFrame({0: [1.0] * 10})
    r = run_backtest([df], w, [ZERO.model_copy(update={"max_participation": 0.05})], capital=1_000_000)
    assert r.capacity_breaches > 0
    assert r.positions["0"].iloc[1] == pytest.approx(0.0005)  # partial fill, keeps working the order
    assert r.positions["0"].iloc[2] == pytest.approx(0.001)


def test_gross_exposure_is_capped_at_one():
    df = frame([100] * 5)
    r = run_backtest([df, df], pd.DataFrame({0: [1.0] * 5, 1: [1.0] * 5}), [ZERO, ZERO])
    assert r.bars["gross_exposure"].max() == pytest.approx(1.0)


@pytest.mark.parametrize("name,params", [("event", {}), ("ts_momentum", {"lookback": 20}),
                                         ("pair_mean_reversion", {}), ("buy_and_hold", {})])
def test_strategies_are_causal(syn, name, params):
    cls = STRATEGIES[name]
    frames = [syn["SYN-B"], syn["SYN-A"]][: cls.n_assets]
    full = cls(**params).weights(frames).to_numpy()
    cut = 900
    part = cls(**params).weights([f.iloc[:cut].copy() for f in frames]).to_numpy()
    assert np.allclose(part, full[:cut])


def test_walk_forward_windows_respect_embargo():
    ws = windows(1000, 300, 100, 10)
    assert ws and all(w["test_start"] == w["train_end"] + 10 for w in ws)
    assert all(w["test_end"] <= 1000 for w in ws)
    assert ws[1]["train_start"] - ws[0]["train_start"] == 100


def test_purged_kfold_removes_overlapping_and_embargoed_events():
    starts = np.arange(0, 100)
    ends = starts + 5  # labels span 5 bars
    for train, test in purged_kfold(starts, ends, 5, embargo=3):
        t0, t1 = starts[test].min(), ends[test].max()
        assert not ((starts[train] <= t1 + 3) & (ends[train] >= t0)).any()
        assert len(np.intersect1d(train, test)) == 0


def test_deflated_sharpe_penalises_more_trials():
    one = deflated_sharpe(0.1, 750, 0.0, 3.0, 1, 0.001)["dsr"]
    many = deflated_sharpe(0.1, 750, 0.0, 3.0, 100, 0.001)["dsr"]
    assert one > many


def test_pbo_high_for_noise_low_for_dominant_config():
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 0.01, (800, 20))
    assert probability_of_backtest_overfitting(noise)["pbo"] > 0.2
    good = noise.copy()
    good[:, 0] += 0.004
    assert probability_of_backtest_overfitting(good)["pbo"] < 0.1
