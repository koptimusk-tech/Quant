"""Point-in-time backtesting engine (deterministic, no LLM involvement).

Timing model
------------
* Target weights w_t are decided with information available at the close of bar t.
* They are executed at the open of bar t+1 (the first bar that starts at or after the decision's
  available_ts), never at the close of t. Executed position e_s = w_{s-1}.
* Bar return for an asset over bar s: overnight leg e_{s-1} * (O_s / C_{s-1} - 1) plus intrabar leg
  e_s * (C_s / O_s - 1). Opens are scaled by the bar's adjustment factor (adj_close / close).
* Trading cost at the open of s: |e_s - e_{s-1}| * (commission + half spread + slippage
  + slippage_range_frac * range_{s-1}), all known before trading. Holding costs accrue per bar.
* Liquidity: traded notional above max_participation of the previous bar's dollar volume is
  clipped (the order is only partly filled) and counted as a capacity breach.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from qal.research.costs import CostModel, periods_per_year


@dataclass
class BacktestResult:
    bars: pd.DataFrame  # per bar: ts, gross, cost, net, gross_exposure, net_exposure, turnover
    positions: pd.DataFrame  # executed positions per asset
    trades: list[dict[str, Any]]
    capacity_breaches: int
    periods_per_year: float
    asset_pnl: dict[str, float] = field(default_factory=dict)


def align(frames: list[pd.DataFrame]) -> list[pd.DataFrame]:
    """Inner-join frames on bar start so multi-asset strategies see the same bars."""
    if len(frames) == 1:
        return [frames[0].reset_index(drop=True)]
    common = set(frames[0]["ts"])
    for f in frames[1:]:
        common &= set(f["ts"])
    out = []
    for f in frames:
        g = f[f["ts"].isin(common)].sort_values("ts").reset_index(drop=True)
        out.append(g)
    # A multi-asset bar is only available once every leg is.
    avail = pd.concat([g["available_ts"] for g in out], axis=1).max(axis=1)
    for g in out:
        g["available_ts"] = avail
    return out


def run_backtest(frames: list[pd.DataFrame], weights: pd.DataFrame, costs: list[CostModel], *,
                 capital: float = 1_000_000.0, asset_class: str = "other", names: list[str] | None = None,
                 cost_multiplier: float = 1.0) -> BacktestResult:
    n = len(frames[0])
    k = len(frames)
    names = names or [str(i) for i in range(k)]
    W = np.nan_to_num(weights.to_numpy(dtype=float)[:, :k])
    gross_target = np.abs(W).sum(axis=1, keepdims=True)
    W = np.where(gross_target > 1, W / np.maximum(gross_target, 1e-12), W)

    E = np.zeros((n, k))  # executed positions
    gross = np.zeros(n)
    cost = np.zeros(n)
    turnover = np.zeros(n)
    breaches = 0
    asset_pnl = np.zeros(k)
    freq = str(frames[0]["frequency"].iloc[0])
    ppy = periods_per_year(freq, asset_class)
    dt_years = 1.0 / ppy
    bars_per_day = ppy / (365.0 if asset_class == "crypto" else 252.0)

    for j, (df, cm0) in enumerate(zip(frames, costs, strict=True)):
        cm = cm0.scaled(cost_multiplier) if cost_multiplier != 1.0 else cm0
        c = df["close"].to_numpy(float)
        factor = (df["adj_close"] / df["close"]).to_numpy(float) if df["adj_close"].notna().mean() > 0.95 else np.ones(n)
        factor = np.where(np.isfinite(factor), factor, 1.0)
        ac = c * factor
        ao = df["open"].to_numpy(float) * factor
        rng = ((df["high"] - df["low"]) / df["close"]).to_numpy(float)
        dvol = (df["close"] * df["volume"]).to_numpy(float)
        prev_e = 0.0
        for s in range(1, n):
            target = W[s - 1, j]
            trade = target - prev_e
            limit = cm.max_participation * dvol[s - 1] / capital if np.isfinite(dvol[s - 1]) and dvol[s - 1] > 0 else np.inf
            if abs(trade) > limit:
                breaches += 1
                trade = np.sign(trade) * limit
            e = prev_e + trade
            E[s, j] = e
            overnight = prev_e * (ao[s] / ac[s - 1] - 1.0)
            intrabar = e * (ac[s] / ao[s] - 1.0)
            pnl = overnight + intrabar
            per_side_bps = cm.commission_bps + cm.half_spread_bps + cm.slippage_bps + cm.slippage_range_frac * rng[s - 1] * 1e4
            tc = abs(trade) * per_side_bps / 1e4
            hold = (max(-e, 0.0) * cm.borrow_bps_annual + max(e, 0.0) * cm.financing_bps_annual) / 1e4 * dt_years
            hold += abs(e) * cm.funding_bps_daily / 1e4 / max(bars_per_day, 1e-9)
            if cm.roll_every_bars and s % cm.roll_every_bars == 0 and e != 0:
                hold += abs(e) * cm.rollover_bps_per_roll / 1e4
            gross[s] += pnl
            cost[s] += tc + hold
            turnover[s] += abs(trade)
            asset_pnl[j] += pnl - tc - hold
            prev_e = e

    bars = pd.DataFrame({
        "ts": frames[0]["ts"].to_numpy(), "gross": gross, "cost": cost, "net": gross - cost,
        "gross_exposure": np.abs(E).sum(axis=1), "net_exposure": E.sum(axis=1), "turnover": turnover,
    })
    positions = pd.DataFrame(E, columns=names)
    positions.insert(0, "ts", frames[0]["ts"].to_numpy())
    trades = extract_trades(positions, bars, names)
    return BacktestResult(bars, positions, trades, breaches, ppy, dict(zip(names, asset_pnl.tolist(), strict=True)))


def extract_trades(positions: pd.DataFrame, bars: pd.DataFrame, names: list[str]) -> list[dict[str, Any]]:
    """A trade is a run of non-zero exposure with constant sign in the first leg (pairs: by the first leg).

    With more than two legs (a cross-sectional portfolio) a trade is a holding period: a run of bars with
    the same non-zero position vector, from one rebalance to the next.
    """
    if len(names) > 2:
        return _portfolio_trades(positions, bars, names)
    lead = positions[names[0]].to_numpy()
    sign = np.sign(np.round(lead, 12))
    net = bars["net"].to_numpy()
    gross = bars["gross"].to_numpy()
    trades, start = [], None
    for i in range(len(sign) + 1):
        cur = sign[i] if i < len(sign) else 0
        prev = sign[i - 1] if i > 0 else 0
        if start is not None and cur != prev:
            # include the bar where the position is closed (its overnight leg still carries exposure)
            end = min(i, len(sign) - 1)
            trades.append({"entry_ts": str(positions["ts"].iloc[start]), "exit_ts": str(positions["ts"].iloc[end]),
                           "direction": int(prev), "bars": int(i - start),
                           "net_pnl": float(net[start:end + 1].sum()), "gross_pnl": float(gross[start:end + 1].sum())})
            start = None
        if start is None and cur != 0 and cur != prev:
            start = i
    return trades


def _portfolio_trades(positions: pd.DataFrame, bars: pd.DataFrame, names: list[str]) -> list[dict[str, Any]]:
    P = np.round(positions[names].to_numpy(dtype=float), 12)
    net, gross = bars["net"].to_numpy(), bars["gross"].to_numpy()
    trades, start = [], None
    for i in range(len(P) + 1):
        changed = i == len(P) or i == 0 or not np.array_equal(P[i], P[i - 1])
        if start is not None and changed:
            end = min(i, len(P) - 1)
            trades.append({"entry_ts": str(positions["ts"].iloc[start]), "exit_ts": str(positions["ts"].iloc[end]),
                           "direction": 0, "bars": int(i - start),
                           "net_pnl": float(net[start:end + 1].sum()), "gross_pnl": float(gross[start:end + 1].sum())})
            start = None
        if start is None and i < len(P) and changed and np.abs(P[i]).sum() > 0:
            start = i
    return trades
