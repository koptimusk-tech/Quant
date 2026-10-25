"""Plain explanations of the stored scores, kept as four separate concepts.

* Rarity: how far the statistic is from its trailing reference (STATISTIC, STAT. SCORE).
* Significance: evidence after multiple-testing control (ADJ. P, Benjamini-Hochberg).
* Economic relevance: the preliminary move-versus-assumed-cost ratio (ECON. SCORE), broken into its parts.
* Predictive capacity: not here; it comes from an event study (``qal.anomaly.event_study``).

Nothing is recomputed: every number below is derived from the values stored at scan time, so the
explanation always matches what the table shows. Scores are ranking indices, never probabilities.
"""

from __future__ import annotations

import math
from typing import Any

from qal.anomaly.engine import DEFAULT_MIN_DOLLAR_VOLUME

SIGNIFICANCE_LEVELS = [  # (upper bound on adjusted p, level, label)
    (0.01, "strong", "Strong evidence"),
    (0.05, "significant", "Significant"),
    (0.10, "weak", "Weak evidence"),
]

# What ``move_bps`` is for each detector, and whether comparing it with a round-trip cost is meaningful.
MOVE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "return_shock": {"label": "Absolute log return of the event bar", "interpretable": True},
    "volume_anomaly": {"label": "Absolute log return of the bar with unusual volume", "interpretable": False,
                       "caveat": "The price move is not what was detected (volume was). Scans since economic method "
                                 "move_vs_cost@2 give volume anomalies no economic score; an older stored score "
                                 "is shown for the record only."},
    "volatility_regime": {
        "label": "Realised volatility per bar over the short window (a level, not a move caused by the event)",
        "interpretable": False,
        "caveat": "Any liquid instrument moves several times its cost per bar, so this score is high for "
                  "almost every volatility event, including volatility drops. It says nothing about whether "
                  "the regime change can be traded. Scans since economic method move_vs_cost@2 give volatility "
                  "events no economic score; an older stored score is shown for the record only."},
    "pair_divergence": {
        "label": "Absolute spread (log Y minus the fitted log X), which depends on the regression level",
        "interpretable": False,
        "caveat": "The spread level is not the expected convergence profit; the hedge ratio may not hold."},
}

ECON_MISSING = [
    "Forward returns after comparable events (run the event study).",
    "The real bid-ask spread and slippage at the event time; the cost used is a placeholder per asset class.",
    "Capacity: how much could be traded without moving the price.",
    "Out-of-sample confirmation and multiple-testing control on the trading rule itself.",
]


def significance(adjusted_p: float | None, empirical_p: float | None, n_tests: int | None = None) -> dict[str, Any]:
    """Level of statistical evidence from the BH-adjusted p-value (lower is stronger)."""
    if adjusted_p is None or not math.isfinite(adjusted_p):
        return {"level": "unknown", "label": "Not available", "adjusted_pvalue": None, "empirical_pvalue": empirical_p,
                "text": "No adjusted p-value was stored for this event."}
    level, label = "not_significant", "Not significant"
    for bound, lv, lb in SIGNIFICANCE_LEVELS:
        if adjusted_p < bound:
            level, label = lv, lb
            break
    tests = f" across the {n_tests:,} tests of the scan" if n_tests else " across all tests of the scan"
    if level == "not_significant":
        text = (f"After Benjamini-Hochberg control{tests}, adjusted p = {adjusted_p:.3g}. The event is not "
                "distinguishable from what chance alone produces with that many tests. A value near 1 is "
                "absence of evidence, not evidence of an effect.")
    else:
        text = (f"After Benjamini-Hochberg control{tests}, adjusted p = {adjusted_p:.3g}: the expected share of "
                f"false discoveries among events at least this extreme is about {adjusted_p:.2g}. This is about "
                "rarity relative to the trailing window; it says nothing about future returns.")
    return {"level": level, "label": label, "adjusted_pvalue": adjusted_p, "empirical_pvalue": empirical_p,
            "thresholds": {"strong": 0.01, "significant": 0.05, "weak": 0.10}, "text": text,
            "method": "Rank p-value against the trailing window (Pareto tail beyond its maximum), then "
                      "Benjamini-Hochberg over every evaluated bar of every detector in the scan."}


def rarity(detector: str, statistic: float, stat_score: float, metrics: dict[str, Any]) -> dict[str, Any]:
    thr = metrics.get("threshold")
    text = (f"Statistic {statistic:+.3g} against a detection threshold of {thr}. STAT. SCORE = |s| / (|s| + "
            f"threshold) = {stat_score:.2f}; 0.5 means exactly at the threshold, so every stored event has at least "
            "0.5. It orders events by size within a detector; it is not a probability and ignores how many tests "
            "were run.") if thr is not None else f"Statistic {statistic:+.3g}; STAT. SCORE {stat_score:.2f}."
    return {"statistic": statistic, "statistical_score": stat_score, "threshold": thr,
            "formula": "|statistic| / (|statistic| + threshold)", "range": "[0.5, 1) for stored events",
            "higher_is": "rarer", "text": text}


def economic(detector: str, economic_score: float | None, ctx: dict[str, Any] | None) -> dict[str, Any]:
    """Break ECON. SCORE into the parts it is made of, with what each part measures."""
    ctx = ctx or {}
    move = MOVE_DEFINITIONS.get(detector, {"label": "Price move associated with the event", "interpretable": True})
    cost, legs = ctx.get("assumed_round_trip_cost_bps"), ctx.get("legs", 1)
    out: dict[str, Any] = {
        "score": economic_score, "interpretable": bool(move["interpretable"]) and economic_score is not None,
        "formula": "ratio = move_bps / (cost_bps x legs);  score = ratio / (ratio + 3) x liquidity_factor",
        "aggregation": "Single ratio passed through a saturating transform, times a liquidity factor. There is no "
                       "weighted sum of components; 0.5 means the move is 3 times the assumed cost.",
        "missing": ECON_MISSING, "caveat": move.get("caveat"), "range": "[0, 1)", "higher_is": "larger move vs cost",
    }
    if economic_score is None:
        out["components"] = [{"name": "reason", "value": ctx.get("reason", "Not computed"), "source": "scan"}]
        out["text"] = f"No economic score: {ctx.get('reason', 'not computed')}"
        return out
    ratio = ctx.get("move_to_cost_ratio")
    dv = ctx.get("median_dollar_volume_20")
    # The scan writes this flag only when it applied the 0.5 penalty (the threshold is part of the scan config).
    liquidity_factor = 0.5 if str(ctx.get("liquidity_flag", "")).startswith("Median dollar volume") else 1.0
    transformed = (ratio / (ratio + 3.0)) if ratio is not None else None
    out["components"] = [
        {"name": "move_bps", "value": ctx.get("move_bps"), "role": "numerator", "source": move["label"]},
        {"name": "assumed_round_trip_cost_bps", "value": cost, "role": "denominator",
         "source": "Placeholder per asset class (scan config), not a broker quote"},
        {"name": "legs", "value": legs, "role": "multiplies the cost", "source": "2 for pairs, else 1"},
        {"name": "move_to_cost_ratio", "value": ratio, "role": "ratio", "source": "move_bps / (cost x legs)"},
        {"name": "saturated_ratio", "value": None if transformed is None else round(transformed, 4),
         "role": "ratio / (ratio + 3)", "source": "transform to [0, 1)"},
        {"name": "liquidity_factor", "value": liquidity_factor, "role": "multiplier",
         "source": "0.5 if the median dollar volume of the 20 prior bars is below the scan minimum "
                   f"(default {DEFAULT_MIN_DOLLAR_VOLUME:,.0f}); 1 otherwise or when volume is missing "
                   f"(median here: {'n/a' if dv is None else f'{dv:,.0f}'})"},
    ]
    parts = [f"The move ({ctx.get('move_bps')} bps: {move['label'].lower()}) is {ratio} times the assumed "
             f"round-trip cost of {cost} bps x {legs} leg(s), which maps to {transformed:.2f}"
             if ratio is not None and transformed is not None else "Move-to-cost ratio not stored"]
    if liquidity_factor < 1:
        parts.append("halved for low liquidity")
    text = "; ".join(parts) + f". ECON. SCORE = {economic_score:.2f}."
    if move.get("caveat"):
        text += " " + move["caveat"]
    text += " It is not a return estimate and no backtest is involved."
    out["text"] = text
    if ctx.get("liquidity_flag"):
        out["liquidity_flag"] = ctx["liquidity_flag"]
    return out


def explain(detector: str, statistic: float, stat_score: float, metrics: dict[str, Any], adjusted_p: float | None,
            empirical_p: float | None, economic_score: float | None, ctx: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "rarity": rarity(detector, statistic, stat_score, metrics),
        "significance": significance(adjusted_p, empirical_p),
        "economic": economic(detector, economic_score, ctx),
        "predictive": {"text": "Predictive capacity is not part of any stored score. It needs an event study of "
                               "this anomaly type on this dataset, evaluated out of sample."},
    }
