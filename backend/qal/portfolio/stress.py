"""Stress tests of a portfolio allocation. Scenarios are labelled by kind and are not predictions:

* ``historical``: what the allocation would have lost in observed windows (worst rolling periods and named
  market episodes inside the data);
* ``hypothetical``: deterministic shocks applied to the estimated risk or to the backtest (volatility jumps,
  correlations going to one, simultaneous drops, higher costs, less liquidity, gaps, regime covariance,
  data delays, market interruptions, simulated execution errors);
* ``synthetic``: Monte Carlo by stationary block bootstrap of joint daily strategy returns. Resampling whole
  days keeps cross-strategy dependence and fat tails; blocks keep short-term serial dependence. The
  distribution only recombines the past.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from qal.portfolio.risk import covariance, fx_legs

EPISODES = [
    ("GFC (Lehman)", "2008-09-15", "2008-12-31"),
    ("Euro debt crisis", "2011-08-01", "2011-10-31"),
    ("SNB removes EURCHF floor", "2015-01-15", "2015-01-30"),
    ("Brexit referendum", "2016-06-23", "2016-07-08"),
    ("COVID crash", "2020-02-20", "2020-04-30"),
    ("2022 rate shock", "2022-02-24", "2022-10-31"),
]
DISCLAIMER = ("Scenarios describe losses under stated assumptions. They are not forecasts, and the worst "
              "outcome in the future can exceed every scenario shown.")


def _var(w: np.ndarray, C: np.ndarray, z: float = 2.326, days: int = 1) -> float:
    return float(z * np.sqrt(max(w @ C @ w, 0.0)) * np.sqrt(days / 252.0))


def historical(port: pd.Series, windows: tuple[int, ...] = (1, 5, 20)) -> list[dict[str, Any]]:
    out = []
    r = port.fillna(0.0)
    logc = np.log1p(r).cumsum()
    for k in windows:
        roll = np.expm1(logc - logc.shift(k).fillna(0.0)) if k > 1 else r
        if len(roll) < k:
            continue
        end = roll.idxmin()
        out.append({"name": f"worst {k}-day window", "kind": "historical", "pnl": float(roll.min()),
                    "start": str((r.index[max(r.index.get_loc(end) - k + 1, 0)]).date()), "end": str(end.date()),
                    "description": f"Largest observed loss over {k} consecutive days with these weights"})
    for name, a, b in EPISODES:
        seg = r[(r.index >= pd.Timestamp(a, tz="UTC")) & (r.index <= pd.Timestamp(b, tz="UTC"))]
        if len(seg) < 3:
            continue
        eq = (1 + seg).cumprod()
        out.append({"name": name, "kind": "historical", "pnl": float(eq.iloc[-1] - 1),
                    "max_drawdown": float((eq / eq.cummax() - 1).min()), "start": a, "end": b,
                    "description": "Observed returns of the strategies in this episode, current weights"})
    return out


def hypothetical(w: dict[str, float], R: pd.DataFrame, expo: dict[str, dict[str, float]], asset_class: dict[str, str],
                 cost_bps: dict[str, float], *, vol_mult: float = 2.0, corr_to: float = 0.9,
                 drop_sigmas: float = 3.0, gap: float = 0.03, chf_gap: float = 0.15) -> list[dict[str, Any]]:
    ids = list(w)
    wv = np.array([w[s] for s in ids])
    X = R[ids].dropna(how="any")
    C = covariance(X, "sample").to_numpy() if len(X) >= 20 else np.diag(np.full(len(ids), 1e-4))
    vols = np.sqrt(np.clip(np.diag(C), 0, None))
    base = _var(wv, C)
    out = [{"name": "baseline 1-day VaR 99% (normal, trailing covariance)", "kind": "hypothetical", "pnl": -base,
            "description": "Reference point: parametric VaR from the estimated covariance"}]
    Cv = C * vol_mult**2
    out.append({"name": f"volatility x{vol_mult:g}", "kind": "hypothetical", "pnl": -_var(wv, Cv),
                "description": "Every strategy's volatility multiplied, correlations unchanged"})
    Cc = np.outer(vols, vols) * corr_to
    np.fill_diagonal(Cc, vols**2)
    out.append({"name": f"diversification disappears (all correlations {corr_to})", "kind": "hypothetical",
                "pnl": -_var(wv, Cc), "description": "Historical correlations replaced by a common stress correlation"})
    daily = vols / np.sqrt(252)
    out.append({"name": f"simultaneous {drop_sigmas:g}-sigma drop", "kind": "hypothetical",
                "pnl": float(-(wv * daily * drop_sigmas).sum()),
                "description": "Every strategy loses a 3-sigma day at once (no offsetting gains)"})
    # Instrument gaps on look-through exposure.
    inst: dict[str, float] = {}
    for s in ids:
        for sym, e in (expo.get(s) or {}).items():
            inst[sym] = inst.get(sym, 0.0) + w[s] * e
    gap_loss = -sum(abs(e) * gap for e in inst.values())
    out.append({"name": f"{gap:.0%} adverse gap on every instrument", "kind": "hypothetical", "pnl": gap_loss,
                "description": "Prices jump against every position before stops or rebalancing can act"})
    chf = sum(abs(e) for sym, e in inst.items() if asset_class.get(sym) == "fx" and "CHF" in (fx_legs(sym) or ()))
    if chf:
        out.append({"name": f"CHF {chf_gap:.0%} gap (SNB 2015-like)", "kind": "hypothetical", "pnl": -chf * chf_gap,
                    "description": "Exposure to CHF pairs hit by a de-pegging style jump"})
    liq = -sum(abs(e) * cost_bps.get(sym, 2.0) * 5 / 1e4 for sym, e in inst.items())
    out.append({"name": "forced exit with spreads x5", "kind": "hypothetical", "pnl": liq,
                "description": "Closing every position at five times the assumed per-side cost"})
    # Regime covariance: high-volatility days only (measured causally).
    ew = X.mean(axis=1)
    vol = ew.rolling(60).std().shift(1)
    hi = X[(vol > vol.expanding(60).median().shift(1)).to_numpy()]
    if len(hi) >= 30:
        Ch = covariance(hi, "sample").to_numpy()
        out.append({"name": "high-volatility regime covariance", "kind": "hypothetical", "pnl": -_var(wv, Ch),
                    "description": "1-day VaR 99% using the covariance of past high-volatility days only"})
    for o in out:
        o.setdefault("basis", "estimate")
    return out


def stationary_bootstrap(X: np.ndarray, n_paths: int, horizon: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """Indices of resampled days, shape (n_paths, horizon) (Politis & Romano)."""
    T = len(X)
    p = 1.0 / block
    idx = np.empty((n_paths, horizon), dtype=int)
    idx[:, 0] = rng.integers(T, size=n_paths)
    jumps = rng.random((n_paths, horizon)) < p
    fresh = rng.integers(T, size=(n_paths, horizon))
    for h in range(1, horizon):
        idx[:, h] = np.where(jumps[:, h], fresh[:, h], (idx[:, h - 1] + 1) % T)
    return idx


def monte_carlo(w: dict[str, float], R: pd.DataFrame, *, n_paths: int = 2000, horizon: int = 252, block: int = 10,
                seed: int = 7, stressed: bool = False) -> dict[str, Any]:
    ids = list(w)
    X = R[ids].dropna(how="any")
    if len(X) < 60:
        return {"kind": "synthetic", "note": "not enough joint history for a bootstrap"}
    if stressed:
        ew = X.mean(axis=1)
        vol = ew.rolling(60).std().shift(1)
        X = X[(vol > vol.expanding(60).median().shift(1)).to_numpy()]
        if len(X) < 60:
            return {"kind": "synthetic", "note": "not enough high-volatility days"}
    rng = np.random.default_rng(seed)
    A = X.to_numpy()
    wv = np.array([w[s] for s in ids])
    port = A @ wv
    idx = stationary_bootstrap(A, n_paths, horizon, block, rng)
    paths = port[idx]
    eq = np.cumprod(1 + paths, axis=1)
    total = eq[:, -1] - 1
    mdd = (eq / np.maximum.accumulate(np.concatenate([np.ones((n_paths, 1)), eq], axis=1)[:, 1:], axis=1) - 1).min(axis=1)
    day = paths.ravel()
    q = lambda a, p: float(np.quantile(a, p))
    es = lambda a, p: float(a[a <= np.quantile(a, p)].mean())
    return {"kind": "synthetic", "name": "stationary block bootstrap" + (" (high-vol days only)" if stressed else ""),
            "paths": n_paths, "horizon_days": horizon, "block": block, "seed": seed, "sample_days": len(A),
            "daily": {"var95": q(day, 0.05), "var99": q(day, 0.01), "es95": es(day, 0.05), "es99": es(day, 0.01),
                      "skew": float(stats.skew(port)), "kurtosis": float(stats.kurtosis(port, fisher=False))},
            "horizon": {"p01": q(total, 0.01), "p05": q(total, 0.05), "median": q(total, 0.5), "p95": q(total, 0.95),
                        "prob_loss": float((total < 0).mean())},
            "max_drawdown": {"median": q(mdd, 0.5), "p95_worst": q(mdd, 0.05), "p99_worst": q(mdd, 0.01)},
            "hist": np.histogram(total, bins=30)[0].tolist(), "hist_edges": np.histogram(total, bins=30)[1].tolist(),
            "note": "Resamples observed days; it cannot produce a crisis worse than any day in the sample. Not a forecast."}
