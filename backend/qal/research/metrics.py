"""Performance metrics computed from per-bar returns (fractions of capital)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy import stats


def _sharpe(r: np.ndarray, ppy: float) -> float:
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    return float(r.mean() / sd * np.sqrt(ppy)) if sd > 0 else 0.0


def drawdown(r: np.ndarray) -> np.ndarray:
    eq = np.cumprod(1 + r)
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
    return eq / peak - 1


def summary(bars: pd.DataFrame, trades: list[dict[str, Any]], ppy: float, *, capacity_breaches: int = 0) -> dict[str, Any]:
    g, n_, c = bars["gross"].to_numpy(), bars["net"].to_numpy(), bars["cost"].to_numpy()
    T = len(n_)
    out: dict[str, Any] = {"bars": T}
    if T < 2:
        return out
    eq_n, eq_g = np.prod(1 + n_), np.prod(1 + g)
    years = T / ppy
    dd = drawdown(n_)
    downside = n_[n_ < 0]
    ds = np.sqrt((downside**2).sum() / T) if len(downside) else 0.0
    wins = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in trades if t["net_pnl"] <= 0]
    total_cost = float(c.sum())
    pnl_abs = sorted((abs(t["net_pnl"]) for t in trades), reverse=True)
    total_net = float(n_.sum())
    top5 = sum(sorted((t["net_pnl"] for t in trades), reverse=True)[:5])
    out.update({
        "total_return_gross": float(eq_g - 1), "total_return_net": float(eq_n - 1),
        "cagr_net": float(eq_n ** (1 / years) - 1) if years > 0 and eq_n > 0 else None,
        "volatility_ann": float(n_.std(ddof=1) * np.sqrt(ppy)),
        "sharpe_gross": _sharpe(g, ppy), "sharpe_net": _sharpe(n_, ppy),
        "sortino_net": float(n_.mean() / ds * np.sqrt(ppy)) if ds > 0 else None,
        "max_drawdown": float(dd.min()),
        "calmar": float((eq_n ** (1 / years) - 1) / abs(dd.min())) if dd.min() < 0 and years > 0 and eq_n > 0 else None,
        "n_trades": len(trades),
        "hit_rate": float(len(wins) / len(trades)) if trades else None,
        "profit_factor": float(sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None,
        "avg_trade_net": float(np.mean([t["net_pnl"] for t in trades])) if trades else None,
        "exposure_avg": float(bars["gross_exposure"].mean()),
        "turnover_ann": float(bars["turnover"].sum() / years) if years > 0 else None,
        "total_cost": total_cost, "cost_per_trade": total_cost / len(trades) if trades else None,
        "cost_drag_ann": total_cost / years if years > 0 else None,
        "top5_trades_share": float(top5 / total_net) if total_net > 0 and trades else None,
        "largest_trade_share_abs": float(pnl_abs[0] / sum(pnl_abs)) if pnl_abs and sum(pnl_abs) > 0 else None,
        "skew": float(stats.skew(n_)), "kurtosis": float(stats.kurtosis(n_, fisher=False)),
        "capacity_breaches": capacity_breaches,
        "mean_net": float(n_.mean()),
    })
    return out


def block_bootstrap_sharpe(r: np.ndarray, ppy: float, *, n_boot: int = 1000, block: int | None = None,
                           seed: int = 0) -> dict[str, float]:
    """Stationary (Politis-Romano) block bootstrap CI for the annualised Sharpe ratio."""
    T = len(r)
    if T < 20:
        return {}
    rng = np.random.default_rng(seed)
    block = block or max(5, int(round(T ** (1 / 3))))
    p = 1.0 / block
    out = np.empty(n_boot)
    t = np.arange(T)
    for b in range(n_boot):
        jumps = rng.random(T) < p
        jumps[0] = True
        seg = np.cumsum(jumps) - 1
        seg_t0 = t[jumps]
        seg_start = rng.integers(T, size=len(seg_t0))
        idx = (seg_start[seg] + (t - seg_t0[seg])) % T
        out[b] = _sharpe(r[idx], ppy)
    return {"sharpe_ci_low": float(np.quantile(out, 0.025)), "sharpe_ci_high": float(np.quantile(out, 0.975)),
            "prob_sharpe_le_0": float((out <= 0).mean()), "block": block, "n_boot": n_boot, "seed": seed}


def deflated_sharpe(sr_per_period: float, T: int, skew: float, kurt: float, n_trials: int,
                    trial_sr_var: float) -> dict[str, float]:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014), non-annualised inputs.

    Probability that the true Sharpe exceeds the expected maximum Sharpe of ``n_trials`` unskilled
    trials with variance ``trial_sr_var``.
    """
    emc = 0.5772156649
    n = max(int(n_trials), 1)
    if n > 1 and trial_sr_var > 0:
        sr0 = np.sqrt(trial_sr_var) * ((1 - emc) * stats.norm.ppf(1 - 1 / n) + emc * stats.norm.ppf(1 - 1 / (n * np.e)))
    else:
        sr0 = 0.0
    denom = np.sqrt(max(1 - skew * sr_per_period + (kurt - 1) / 4 * sr_per_period**2, 1e-12))
    z = (sr_per_period - sr0) * np.sqrt(max(T - 1, 1)) / denom
    return {"dsr": float(stats.norm.cdf(z)), "sr0_per_period": float(sr0), "n_trials": n,
            "trial_sr_var": float(trial_sr_var)}


def probability_of_backtest_overfitting(perf: np.ndarray, n_blocks: int = 8) -> dict[str, float] | None:
    """PBO by combinatorially symmetric cross-validation (Bailey et al.).

    ``perf`` is a T x N matrix of per-bar returns for N configurations. Returns the share of splits in
    which the in-sample best configuration ranks below the median out of sample.
    """
    from itertools import combinations

    T, N = perf.shape
    if N < 2 or T < n_blocks * 5:
        return None
    blocks = np.array_split(np.arange(T), n_blocks)
    logits = []
    for is_idx in combinations(range(n_blocks), n_blocks // 2):
        is_rows = np.concatenate([blocks[i] for i in is_idx])
        oos_rows = np.concatenate([blocks[i] for i in range(n_blocks) if i not in is_idx])

        def sr(rows):
            m, s = perf[rows].mean(axis=0), perf[rows].std(axis=0, ddof=1)
            return np.where(s > 0, m / s, 0.0)

        best = int(np.argmax(sr(is_rows)))
        oos = sr(oos_rows)
        rank = (stats.rankdata(oos)[best]) / (N + 1)
        logits.append(np.log(rank / (1 - rank)))
    logits = np.array(logits)
    return {"pbo": float((logits <= 0).mean()), "n_splits": len(logits), "n_configs": N}
