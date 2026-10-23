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
    exact only under exchangeability; volatility clustering makes it optimistic.
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
    pval[window:] = (1 + exceed) / (window + 1)
    return med, scale, pval


def trailing_rank_pvalue(x: np.ndarray, window: int) -> np.ndarray:
    """Upper-tail rank p-value of |x[t]| among |x| of the previous ``window`` values."""
    n = len(x)
    out = np.full(n, np.nan)
    if n <= window:
        return out
    a = np.abs(x)
    win = sliding_window_view(a[:-1], window)
    out[window:] = (1 + (win >= a[window:, None]).sum(axis=1)) / (window + 1)
    return out
