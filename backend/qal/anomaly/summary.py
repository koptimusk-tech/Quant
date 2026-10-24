"""Uniform, human-readable description of a detection: what was observed, against which statistical
reference, by how much it deviates and with which method.

Derived deterministically from the stored detector metrics, so it needs no extra columns and stays
consistent with the reproducibility check (the metrics are what the detector recomputes).
"""
from __future__ import annotations

from typing import Any


def _f(metrics: dict[str, Any], key: str) -> float | None:
    v = metrics.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def summarize(detector: str, metrics: dict[str, Any], statistic: float) -> dict[str, Any]:
    m = metrics or {}
    if detector == "return_shock":
        return {
            "method": f"Robust z-score of the log return vs. the median and MAD of the previous {m.get('window')} bars "
                      "(current bar excluded)",
            "observed_label": "log return", "observed": _f(m, "log_return"),
            "reference_label": "trailing median (scale: MAD × 1.4826)", "reference": _f(m, "baseline_median"),
            "reference_scale": _f(m, "baseline_mad_scale"),
            "deviation": statistic, "deviation_unit": "robust σ",
            "relative": None, "relative_label": None,
        }
    if detector == "volume_anomaly":
        return {
            "method": f"Robust z-score of log volume vs. the previous {m.get('window')} bars (current bar excluded)",
            "observed_label": "volume", "observed": _f(m, "volume"),
            "reference_label": "trailing median volume", "reference": _f(m, "baseline_median_volume"),
            "reference_scale": None,
            "deviation": statistic, "deviation_unit": "robust σ (log volume)",
            "relative": _f(m, "volume_ratio"), "relative_label": "relative volume (observed / median)",
        }
    if detector == "volatility_regime":
        return {
            "method": f"Ratio of {m.get('short_window')}-bar realised volatility to a {m.get('long_window')}-bar "
                      "baseline that ends before the short window",
            "observed_label": "short-window volatility (per bar)", "observed": _f(m, "short_vol"),
            "reference_label": "long-window baseline volatility", "reference": _f(m, "long_vol"),
            "reference_scale": None,
            "deviation": statistic, "deviation_unit": "log2(vol ratio)",
            "relative": _f(m, "vol_ratio"), "relative_label": "volatility ratio",
        }
    if detector == "pair_divergence":
        return {
            "method": f"Residual of a rolling {m.get('window')}-bar OLS hedge (hedge ratio "
                      f"{_f(m, 'hedge_ratio') or 0:.3g}) in units of its residual standard deviation",
            "observed_label": "spread (y − α − β·x)", "observed": _f(m, "spread"),
            "reference_label": "expected spread (0) ± residual sd", "reference": 0.0,
            "reference_scale": _f(m, "residual_sd"),
            "deviation": statistic, "deviation_unit": "residual σ",
            "relative": None, "relative_label": None,
        }
    return {"method": detector, "observed_label": None, "observed": None, "reference_label": None, "reference": None,
            "reference_scale": None, "deviation": statistic, "deviation_unit": "statistic",
            "relative": None, "relative_label": None}
