"""Portfolio construction methods. Inputs are covariance estimates from data up to the decision date; no
method uses future returns. Weights are long-only allocations of capital to strategies (each strategy keeps
its own internal long/short positions).

* equal_weight: 1/N, then caps.
* inverse_vol: w_i proportional to 1/sigma_i, then caps.
* risk_budget: weights whose risk contributions match configured budgets (equal risk contribution when the
  budgets are equal), solved by cyclical coordinate descent; caps are applied after, and the achieved
  contributions are reported against the targets.
* mean_variance (optional): maximum Sharpe with a heavily shrunk mean estimate. Expected returns estimated
  from history are noisy; this method is offered only to compare its weight instability with the others.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

METHODS = ("equal_weight", "inverse_vol", "risk_budget", "mean_variance")


def apply_caps(w: np.ndarray, cap: float | np.ndarray, total: float = 1.0, iters: int = 50) -> np.ndarray:
    """Clip weights at ``cap`` and hand the excess to uncapped names pro rata (water filling).

    If every name is capped the remainder stays in cash (sum < total).
    """
    w = np.clip(np.asarray(w, float), 0, None)
    caps = np.broadcast_to(np.asarray(cap, float), w.shape).copy()
    if w.sum() <= 0:
        return w
    w = w / w.sum() * total
    for _ in range(iters):
        over = w > caps + 1e-12
        if not over.any():
            break
        excess = float((w[over] - caps[over]).sum())
        w[over] = caps[over]
        free = w < caps - 1e-12
        if not free.any() or w[free].sum() <= 0:
            break
        w[free] += excess * w[free] / w[free].sum()
    return np.minimum(w, caps)


def equal_weight(n: int, cap: float = 1.0) -> np.ndarray:
    return apply_caps(np.ones(n) / n, cap)


def inverse_vol(C: pd.DataFrame, cap: float = 1.0) -> np.ndarray:
    vol = np.sqrt(np.clip(np.diag(C.to_numpy()), 1e-18, None))
    return apply_caps(1 / vol, cap)


def risk_budget(C: pd.DataFrame, budgets: np.ndarray | None = None, cap: float = 1.0,
                tol: float = 1e-10, max_iter: int = 500) -> np.ndarray:
    """Spinu / Griveau-Billion cyclical coordinate descent for risk budgeting (long-only)."""
    S = C.to_numpy()
    n = S.shape[0]
    b = np.ones(n) / n if budgets is None else np.asarray(budgets, float)
    if (b < 0).any() or b.sum() <= 0:
        raise ValueError("risk budgets must be non-negative and not all zero")
    b = b / b.sum()
    w = 1 / np.sqrt(np.clip(np.diag(S), 1e-18, None))
    w /= w.sum()
    for _ in range(max_iter):
        w_old = w.copy()
        for i in range(n):
            sig = np.sqrt(max(w @ S @ w, 1e-24))
            a = S[i, i]
            c = S[i] @ w - a * w[i]
            # a w^2 + c w - b sigma = 0, positive root
            w[i] = (-c + np.sqrt(c * c + 4 * a * b[i] * sig)) / (2 * a) if a > 0 else w[i]
        if np.abs(w - w_old).max() < tol:
            break
    w = w / w.sum()
    return apply_caps(w, cap)


def mean_variance(R: pd.DataFrame, C: pd.DataFrame, cap: float = 1.0, shrink: float = 0.5,
                  risk_aversion: float = 5.0) -> np.ndarray:
    """Long-only mean-variance on a shrunk historical mean (projected gradient). Optional and fragile."""
    X = R[C.columns].dropna(how="any")
    mu = X.mean().to_numpy() * 252
    mu = (1 - shrink) * mu + shrink * mu.mean()
    S = C.to_numpy()
    n = len(mu)
    from scipy.optimize import minimize

    res = minimize(lambda w: -(mu @ w) + 0.5 * risk_aversion * w @ S @ w, np.ones(n) / n,
                   jac=lambda w: -mu + risk_aversion * S @ w, method="SLSQP", bounds=[(0.0, cap)] * n,
                   constraints=[{"type": "eq", "fun": lambda w: w.sum() - 1.0, "jac": lambda w: np.ones(n)}],
                   options={"maxiter": 200, "ftol": 1e-12})
    return np.clip(res.x, 0, cap)


def build(method: str, R: pd.DataFrame, C: pd.DataFrame, *, cap: float = 1.0,
          budgets: dict[str, float] | None = None) -> np.ndarray:
    n = C.shape[0]
    cap = max(cap, 1.0 / n)  # a cap below 1/N cannot be met while fully invested; reported by the caller
    if method == "equal_weight":
        return equal_weight(n, cap)
    if method == "inverse_vol":
        return inverse_vol(C, cap)
    if method == "risk_budget":
        b = None if not budgets else np.array([budgets.get(s, 1.0) for s in C.columns], float)
        return risk_budget(C, b, cap)
    if method == "mean_variance":
        return mean_variance(R, C, cap)
    raise ValueError(f"unknown method {method!r}; use one of {METHODS}")


def vol_target_scale(w: np.ndarray, C: pd.DataFrame, target_vol: float | None, max_leverage: float) -> tuple[float, float]:
    """Scale so that the ex-ante volatility (from the trailing covariance) equals ``target_vol``.

    Returns (scale, ex-ante vol before scaling). The scale is capped so the sum of weights stays within
    ``max_leverage``. Ex-ante volatility is an estimate from past data, not a forecast guarantee.
    """
    vol = float(np.sqrt(max(w @ C.to_numpy() @ w, 0.0)))
    if not target_vol or vol <= 0:
        return 1.0, vol
    scale = target_vol / vol
    if w.sum() * scale > max_leverage:
        scale = max_leverage / max(w.sum(), 1e-12)
    return float(scale), vol


def describe(method: str) -> dict[str, Any]:
    return {"equal_weight": "1/N per strategy, capped",
            "inverse_vol": "weights proportional to 1 / trailing volatility, capped",
            "risk_budget": "risk contributions matched to budgets (equal by default), capped",
            "mean_variance": "optional: max Sharpe on a 50% shrunk historical mean; unstable by construction"}.get(method, "")
