"""Causal feature transforms. Every value at bar t uses only bars <= t.

Documented assumptions (see docs/DETECTORS.md, "Features"):
* log_return uses the adjusted price when available (point-in-time safe for returns).
* Rolling statistics used as *baselines* are shifted by one bar so the current bar is excluded.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from qal.data.schema import return_price


def log_returns(df: pd.DataFrame) -> pd.Series:
    px = return_price(df)
    return np.log(px).diff()


def ewma_vol(r: pd.Series, lam: float = 0.94, min_periods: int = 30) -> pd.Series:
    """RiskMetrics-style EWMA volatility *before* bar t (shifted), so it can scale r_t."""
    var = (r**2).ewm(alpha=1 - lam, min_periods=min_periods, adjust=False).mean()
    return np.sqrt(var).shift(1)


def dollar_volume(df: pd.DataFrame) -> pd.Series:
    return df["close"] * df["volume"]


def trailing_robust(x: np.ndarray, window: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Median, MAD-based scale and rank p-value of x[t] versus the trailing window x[t-window:t].

    The current observation is excluded from its own baseline. Returns arrays aligned with x
    (NaN where fewer than ``window`` prior observations exist). ``x`` must not contain NaN.

    The p-value is (1 + #{s in window: |x_s - med| >= |x_t - med|}) / (window + 1), which is
    exact only under exchangeability; volatility clustering makes it optimistic. When the current value is
    beyond everything in the window, the rank p-value would sit at its floor 1/(window+1); there the tail is
    extrapolated instead (see ``tail_extrapolated_pvalue``).
    """
    n = len(x)
    med = np.full(n, np.nan)
    scale = np.full(n, np.nan)
    pval = np.full(n, np.nan)
    if n <= window:
        return med, scale, pval
    win = sliding_window_view(x[:-1], window)  # row i -> x[i:i+window], baseline for t = i+window
    m = np.median(win, axis=1)
    dev = np.abs(win - m[:, None])
    mad = np.median(dev, axis=1) * 1.4826
    cur = np.abs(x[window:] - m)
    exceed = (dev >= cur[:, None]).sum(axis=1)
    med[window:] = m
    scale[window:] = np.where(mad > 0, mad, np.nan)
    pval[window:] = _extend_tail((1 + exceed) / (window + 1), dev, cur, exceed)
    return med, scale, pval


def trailing_rank_pvalue(x: np.ndarray, window: int) -> np.ndarray:
    """Upper-tail rank p-value of |x[t]| among |x| of the previous ``window`` values."""
    n = len(x)
    out = np.full(n, np.nan)
    if n <= window:
        return out
    a = np.abs(x)
    win = sliding_window_view(a[:-1], window)
    cur = a[window:]
    exceed = (win >= cur[:, None]).sum(axis=1)
    out[window:] = _extend_tail((1 + exceed) / (window + 1), win, cur, exceed)
    return out


def tail_extrapolated_pvalue(window_values: np.ndarray, current: np.ndarray, k: int | None = None) -> np.ndarray:
    """Peaks-over-threshold tail p-value P(V >= current) from each row of trailing magnitudes.

    Fits a Pareto tail to the ``k`` largest values of each window with the Hill estimator,
    xi = mean(log(top_k / u)) with threshold u the (k+1)-th largest value, and returns
    (k / (n + 1)) * (current / u) ** (-1 / xi). This only extends the empirical distribution beyond its
    largest observation; it inherits the Pareto-tail assumption and is noisy for short windows, so it is
    capped at the empirical floor and never makes a value inside the window look rarer than its rank.
    Rows with a degenerate tail (u <= 0 or xi <= 0), or windows too short to hold k + 1 values, return NaN.
    """
    window_values = np.atleast_2d(window_values)
    n = window_values.shape[1]
    k = k or max(10, n // 10)
    if k + 1 > n:
        return np.full(window_values.shape[0], np.nan)
    top = -np.sort(-np.partition(window_values, n - k - 1, axis=1)[:, n - k - 1:], axis=1)
    u = top[:, k]
    with np.errstate(divide="ignore", invalid="ignore"):
        xi = np.log(top[:, :k] / u[:, None]).mean(axis=1)
        p = (k / (n + 1)) * (np.asarray(current) / u) ** (-1.0 / xi)
    return np.where((u > 0) & (xi > 0) & np.isfinite(p), p, np.nan)


def _extend_tail(pval: np.ndarray, window_values: np.ndarray, current: np.ndarray, exceed: np.ndarray) -> np.ndarray:
    """Replace floor-level rank p-values (nothing in the window as extreme) by the extrapolated tail."""
    beyond = np.flatnonzero(exceed == 0)
    if beyond.size == 0:
        return pval
    tail = tail_extrapolated_pvalue(window_values[beyond], current[beyond])
    floor = 1.0 / (window_values.shape[1] + 1)
    pval = pval.copy()
    pval[beyond] = np.where(np.isfinite(tail), np.minimum(tail, floor), floor)
    return pval
