"""Statistical uncertainty of a paper session's results.

* Dependence: autocorrelation of the per-bar net returns (lags 1-5) and an effective sample size
  ``n_eff = n / (1 + 2 * sum_k (1 - k / (K + 1)) * rho_k)`` (Bartlett weights, K = min(10, n // 4)).
* Interval of the mean net return and of the excess over the baseline: moving-block bootstrap (fixed seed) with a
  block at least as long as the typical holding period, so overlapping holdings are not treated as independent.
  Used only with 50 bars or more.
* Test of the excess over the baseline: Newey-West (HAC) t-statistic with the same lag, one-sided p-value. When
  several strategies are reviewed together the p-values are adjusted (Holm), and the adjusted value is the one used.
* Independent trades: trades whose holding windows do not overlap (greedy, by entry time).
* Sharpe: the i.i.d. standard error of Lo (2002); in samples under 252 bars or with fewer than 100 effective
  observations it is reported but flagged as not interpretable.

A p-value is the probability of data at least this extreme if the true mean excess were zero under these
assumptions. It is not the probability that the strategy is profitable or that the hypothesis is true.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from qal.lab.pipeline import hac_t
from qal.program.tracking import block_bootstrap_ci

MIN_BOOT = 50
MIN_TEST = 30
SHARPE_MIN_BARS = 252
SHARPE_MIN_NEFF = 100
P_NOTE = ("p is the probability of an excess at least this large if the true mean excess were zero, under the stated "
          "assumptions. It is not the probability that the strategy is profitable.")


def fmt_ci(ci: list[float] | None) -> str:
    return f"[{ci[0]:.4g}, {ci[1]:.4g}]" if ci else "n/a"


def acf(x: np.ndarray, lags: int) -> list[float | None]:
    n = len(x)
    if n < 3 or np.std(x) == 0:
        return [None] * lags
    u = x - x.mean()
    d = float(u @ u)
    return [float(u[k:] @ u[:-k] / d) if k < n else None for k in range(1, lags + 1)]


def effective_n(x: np.ndarray) -> float | None:
    n = len(x)
    if n < 8:
        return None
    K = min(10, n // 4)
    rho = acf(x, K)
    if rho[0] is None:
        return float(n)
    s = 1 + 2 * sum((1 - k / (K + 1)) * (rho[k - 1] or 0.0) for k in range(1, K + 1))
    return float(min(n, max(1.0, n / s))) if s > 0 else float(n)


def independent_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    spans = sorted((str(t["entry_ts"]), str(t["exit_ts"])) for t in trades)
    count, overlaps, last_end = 0, 0, None
    for a, b in spans:
        if last_end is None or a >= last_end:
            count += 1
            last_end = b
        else:
            overlaps += 1
            last_end = max(last_end, b)
    return {"trades": len(trades), "independent": count, "overlapping": overlaps,
            "rule": "holding windows that do not overlap (greedy by entry); a trade that starts before the exit bar of "
                    "the previous one is not counted as independent (a reversal on the exit bar is a new trade)"}


def holm(pvals: dict[str, float | None]) -> dict[str, float | None]:
    """Holm step-down adjustment (family-wise error), on the p-values that exist."""
    have = sorted(((p, k) for k, p in pvals.items() if p is not None), key=lambda x: x[0])
    m = len(have)
    out: dict[str, float | None] = {k: None for k in pvals}
    run = 0.0
    for i, (p, k) in enumerate(have):
        run = max(run, min(1.0, (m - i) * p))
        out[k] = run
    return out


def analyse(r: np.ndarray, excess: np.ndarray, trades: list[dict[str, Any]], ppy: float) -> dict[str, Any]:
    n = len(r)
    hold = [int(t.get("bars") or 1) for t in trades]
    block = max(5, math.ceil(float(np.mean(hold)))) if hold else 5
    neff = effective_n(r)
    out: dict[str, Any] = {"n": n, "block_bars": block,
                           "block_rule": "max(5, mean holding period of the trades in bars)",
                           "autocorrelation": {f"lag_{k + 1}": v for k, v in enumerate(acf(r, 5))},
                           "effective_n": neff, "independent_trades": independent_trades(trades), "p_note": P_NOTE}
    if n >= MIN_BOOT:
        out["mean_net_ci95"] = block_bootstrap_ci(r, block)
        out["excess_ci95"] = block_bootstrap_ci(excess, block)
        out["bootstrap"] = {"status": "OK", "method": "moving-block bootstrap, 1000 resamples, fixed seed",
                            "assumption": "the dependence is shorter than the block and the period is stationary"}
    else:
        out["mean_net_ci95"] = out["excess_ci95"] = None
        out["bootstrap"] = {"status": "INSUFFICIENT_DATA", "note": f"needs at least {MIN_BOOT} bars (has {n})"}
    t, p = hac_t(excess, block) if n >= MIN_TEST else (None, None)
    out["excess_test"] = {"status": "OK" if p is not None else "INSUFFICIENT_DATA", "t_hac": t, "p_one_sided": p,
                          "lag": block, "h1": "mean excess over the baseline > 0",
                          "note": P_NOTE if p is not None else f"needs at least {MIN_TEST} bars with variation"}
    sd = float(np.std(r, ddof=1)) if n > 1 else 0.0
    if sd > 0:
        sr = float(r.mean() / sd * math.sqrt(ppy))
        se = math.sqrt((1 + (sr / math.sqrt(ppy)) ** 2 / 2) / n) * math.sqrt(ppy)
        ok = n >= SHARPE_MIN_BARS and (neff or 0) >= SHARPE_MIN_NEFF
        out["sharpe"] = {"value": sr, "se": se, "ci95": [sr - 1.96 * se, sr + 1.96 * se], "interpretable": ok,
                         "note": "" if ok else f"small sample ({n} bars, {neff and round(neff)} effective): the Sharpe "
                         "ratio is reported but not interpreted"}
    else:
        out["sharpe"] = {"value": None, "interpretable": False, "note": "no variation in returns"}
    # stability: the two halves of the period
    if n >= 2 * MIN_TEST:
        h = n // 2
        a, b = float(excess[:h].mean()), float(excess[h:].mean())
        out["halves"] = {"first_mean_excess": a, "second_mean_excess": b, "same_sign": (a > 0) == (b > 0)}
    else:
        out["halves"] = None
    return out


def layers(obs: dict[str, Any], unc: dict[str, Any], adj_p: float | None, cost_per_bar: float | None,
           limitations: list[str]) -> dict[str, Any]:
    """The five layers the review keeps apart: observed result, statistical evidence, economic evidence, limitations
    of the evaluation and the conclusion it allows (filled in by the decision engine)."""
    ci = unc.get("excess_ci95")
    if ci is None:
        stat = "not evaluable: the sample is too small for an interval"
    elif ci[0] > 0:
        stat = f"the 95% interval of the excess over the baseline is above zero {fmt_ci(ci)}"
    elif ci[1] < 0:
        stat = f"the 95% interval of the excess over the baseline is below zero {fmt_ci(ci)}"
    else:
        stat = f"the 95% interval of the excess over the baseline includes zero {fmt_ci(ci)}"
    if adj_p is not None:
        stat += f"; Holm-adjusted one-sided p = {adj_p:.3g} ({P_NOTE})"
    mean_ex = obs.get("mean_excess")
    if mean_ex is None or cost_per_bar is None:
        econ = "not evaluable"
    else:
        stressed = mean_ex - cost_per_bar  # the same costs paid once more: a 2x cost stress
        econ = (f"mean excess {mean_ex * 1e4:.2f} bps per bar net of the configured (estimated) costs; with the costs "
                f"doubled it would be {stressed * 1e4:.2f} bps"
                + ("" if stressed > 0 else ": the result does not survive doubled costs"))
    return {"observed": obs, "statistical_evidence": stat, "economic_evidence": econ, "limitations": limitations}
