"""Investigation priority: which anomalies to look at first. A research ordering, never a trading signal.

    base     = 0.35 E + 0.30 P + 0.20 C + 0.15 R
    priority = base x Q x D x A, capped at CAP_WITHOUT_EVIDENCE when there is neither statistical
               significance (adjusted p < 0.05) nor out-of-sample predictive support.

E evidence      min(1, -log10(adjusted p) / 3); 0 when adjusted p = 1 or missing.
P predictive    from the latest event study of the anomaly type on the dataset (see PREDICTIVE_VALUE).
C economic      ECON. SCORE, halved where it is not interpretable (pairs); 0 for volatility and volume events.
R rarity        (STAT. SCORE - 0.5) / 0.5: how far past the detection threshold.
Q data quality  pass 1, warn/unknown 0.8, fail 0; x 0.5 for synthetic data.
D duplication   0.6 when a stronger event of the same detector and dataset lies within DUPLICATE_BARS bars
                in the same scan (the weaker one is likely an echo of it).
A recency       0.5 + 0.5 x 2^(-age / RECENCY_HALF_LIFE_BARS), age counted in bars to the end of the dataset.

Weights and thresholds live in PriorityConfig so they can be changed and are returned with every result.
Because E and P carry 65 % of the weight and the cap applies without evidence, a high ECON. SCORE alone
cannot put an anomaly at the top.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.anomaly.engine import NO_PRICE_MAGNITUDE
from qal.anomaly.event_study import latest_verdicts
from qal.anomaly.explain import MOVE_DEFINITIONS
from qal.models import AnomalyDetection, Dataset, Hypothesis

PREDICTIVE_VALUE = {"oos_supported": 1.0, "in_sample_only": 0.4, "not_evaluated": 0.2,
                    "insufficient_sample": 0.1, "no_evidence": 0.0}
PREDICTIVE_LABEL = {"oos_supported": "supported out of sample", "in_sample_only": "in-sample only",
                    "not_evaluated": "not evaluated", "insufficient_sample": "insufficient sample",
                    "no_evidence": "no evidence"}


@dataclass(frozen=True)
class PriorityConfig:
    weights: dict[str, float] = field(default_factory=lambda: {"evidence": 0.35, "predictive": 0.30,
                                                               "economic": 0.20, "rarity": 0.15})
    evidence_full_at_p: float = 0.001
    significance_alpha: float = 0.05
    cap_without_evidence: float = 0.49
    quality_factor: dict[str, float] = field(default_factory=lambda: {"pass": 1.0, "warn": 0.8, "unknown": 0.8,
                                                                      "fail": 0.0})
    synthetic_factor: float = 0.5
    duplicate_bars: int = 10
    duplicate_factor: float = 0.6
    recency_half_life_bars: float = 500.0
    tiers: dict[str, float] = field(default_factory=lambda: {"high": 0.5, "medium": 0.25})


DEFAULT_CONFIG = PriorityConfig()


def evidence_component(adjusted_p: float | None, cfg: PriorityConfig = DEFAULT_CONFIG) -> float:
    if adjusted_p is None or not math.isfinite(adjusted_p) or adjusted_p <= 0:
        return 0.0 if adjusted_p is None or not math.isfinite(adjusted_p) else 1.0
    return float(min(1.0, max(0.0, -math.log10(adjusted_p) / -math.log10(cfg.evidence_full_at_p))))


def rarity_component(stat_score: float | None) -> float:
    if stat_score is None or not math.isfinite(stat_score):
        return 0.0
    return float(min(1.0, max(0.0, (stat_score - 0.5) / 0.5)))


def economic_component(detector: str, econ: float | None) -> tuple[float, bool]:
    # Volatility and volume events have no tradable price magnitude: a stored score from an older scan
    # does not count towards priority (qal.anomaly.engine.NO_PRICE_MAGNITUDE).
    if econ is None or not math.isfinite(econ) or detector in NO_PRICE_MAGNITUDE:
        return 0.0, False
    interpretable = MOVE_DEFINITIONS.get(detector, {"interpretable": True})["interpretable"]
    return (float(econ) if interpretable else float(econ) * 0.5), bool(interpretable)


def recency_factor(age_bars: float | None, cfg: PriorityConfig = DEFAULT_CONFIG) -> float:
    if age_bars is None or not math.isfinite(age_bars):
        return 0.75
    return 0.5 + 0.5 * 2 ** (-max(age_bars, 0.0) / cfg.recency_half_life_bars)


def score(*, detector: str, adjusted_p: float | None, stat_score: float | None, econ: float | None,
          verdict: str, quality: str, synthetic: bool, duplicate_of: str | None, age_bars: float | None,
          cfg: PriorityConfig = DEFAULT_CONFIG) -> dict[str, Any]:
    """Pure scoring function: every input is explicit so the result can be explained and tested."""
    w = cfg.weights
    e = evidence_component(adjusted_p, cfg)
    p = PREDICTIVE_VALUE.get(verdict, PREDICTIVE_VALUE["not_evaluated"])
    c, interpretable = economic_component(detector, econ)
    r = rarity_component(stat_score)
    comps = [
        {"name": "evidence", "value": round(e, 4), "weight": w["evidence"], "contribution": round(w["evidence"] * e, 4),
         "source": f"adjusted p = {f'{adjusted_p:.3g}' if adjusted_p is not None else 'n/a'}"},
        {"name": "predictive", "value": p, "weight": w["predictive"], "contribution": round(w["predictive"] * p, 4),
         "source": f"event study: {PREDICTIVE_LABEL.get(verdict, verdict)}"},
        {"name": "economic", "value": round(c, 4), "weight": w["economic"], "contribution": round(w["economic"] * c, 4),
         "source": f"ECON. SCORE {f'{econ:.2f}' if econ is not None else 'n/a'}"
                   + ("" if interpretable or econ is None else ", not counted: no price magnitude for this detector"
                      if detector in NO_PRICE_MAGNITUDE else ", halved: not interpretable for this detector")},
        {"name": "rarity", "value": round(r, 4), "weight": w["rarity"], "contribution": round(w["rarity"] * r, 4),
         "source": f"STAT. SCORE {stat_score:.2f}" if stat_score is not None else "n/a"},
    ]
    base = sum(x["contribution"] for x in comps)
    q = cfg.quality_factor.get(quality or "unknown", cfg.quality_factor["unknown"])
    if synthetic:
        q *= cfg.synthetic_factor
    d = cfg.duplicate_factor if duplicate_of else 1.0
    a = recency_factor(age_bars, cfg)
    mults = [
        {"name": "data_quality", "value": round(q, 4),
         "source": f"dataset quality {quality or 'unknown'}" + (", synthetic data" if synthetic else "")},
        {"name": "duplication", "value": d,
         "source": f"stronger event {duplicate_of} within {cfg.duplicate_bars} bars" if duplicate_of
         else "no stronger nearby event"},
        {"name": "recency", "value": round(a, 4),
         "source": "age unknown" if age_bars is None else f"{age_bars:,.0f} bars before the end of the data"},
    ]
    raw = base * q * d * a
    significant = adjusted_p is not None and adjusted_p < cfg.significance_alpha
    capped = (not significant) and verdict != "oos_supported" and raw > cfg.cap_without_evidence
    value = min(raw, cfg.cap_without_evidence) if capped else raw
    tier = "high" if value >= cfg.tiers["high"] else "medium" if value >= cfg.tiers["medium"] else "low"

    drivers = sorted(comps, key=lambda x: -x["contribution"])
    reasons = [f"largest contribution: {drivers[0]['name']} ({drivers[0]['contribution']:.2f})"]
    if e == 0:
        reasons.append("no statistical evidence after multiple-testing control")
    if verdict != "oos_supported":
        reasons.append(f"predictive capacity {PREDICTIVE_LABEL.get(verdict, verdict)}")
    reasons += [f"{m['name']} x{m['value']:.2f}" for m in mults if m["value"] < 1]
    if capped:
        reasons.append(f"capped at {cfg.cap_without_evidence}: no significance and no out-of-sample support")
    return {
        "value": round(value, 4), "tier": tier, "base": round(base, 4), "components": comps, "multipliers": mults,
        "capped": capped, "explanation": "; ".join(reasons) + ".",
        "label": "Research priority, not an execution signal"
                 + ("" if verdict == "oos_supported" else ": there is no out-of-sample predictive evidence"),
        "formula": "(0.35 E + 0.30 P + 0.20 C + 0.15 R) x data_quality x duplication x recency",
    }


def _age_bars(event_ts: datetime, ds: Dataset | None) -> float | None:
    if ds is None or ds.start_ts is None or ds.end_ts is None or ds.rows < 2:
        return None
    span = (ds.end_ts - ds.start_ts).total_seconds()
    if span <= 0:
        return None
    ev = event_ts if event_ts.tzinfo else event_ts.replace(tzinfo=ds.end_ts.tzinfo)
    end = ds.end_ts if ds.end_ts.tzinfo else ds.end_ts.replace(tzinfo=ev.tzinfo)
    return max((end - ev).total_seconds(), 0.0) / span * (ds.rows - 1)


def _duplicates(session: Session, items: list[AnomalyDetection], datasets: dict[str, Dataset],
                cfg: PriorityConfig) -> dict[str, str]:
    """Map anomaly id -> id of a stronger event of the same detector/dataset/scan within duplicate_bars."""
    groups = {(a.experiment_id, a.dataset_id, a.detector, a.related_dataset_id) for a in items}
    out: dict[str, str] = {}
    for exp_id, ds_id, det, rel in groups:
        ds = datasets.get(ds_id)
        if ds is None or ds.start_ts is None or ds.end_ts is None or ds.rows < 2:
            continue
        bar_s = (ds.end_ts - ds.start_ts).total_seconds() / (ds.rows - 1)
        q = select(AnomalyDetection.id, AnomalyDetection.event_ts, AnomalyDetection.statistic).where(
            AnomalyDetection.experiment_id == exp_id, AnomalyDetection.dataset_id == ds_id,
            AnomalyDetection.detector == det)
        q = q.where(AnomalyDetection.related_dataset_id.is_(None) if rel is None
                    else AnomalyDetection.related_dataset_id == rel)
        rows = sorted(session.execute(q).all(), key=lambda r: r[1])
        t = np.array([r[1].replace(tzinfo=None).timestamp() for r in rows])  # all UTC; naive for uniformity
        s = np.abs(np.array([r[2] for r in rows], dtype=float))
        win = cfg.duplicate_bars * bar_s
        for k, (aid, _, _) in enumerate(rows):
            lo, hi = np.searchsorted(t, t[k] - win), np.searchsorted(t, t[k] + win, side="right")
            stronger = [j for j in range(lo, hi) if j != k and (s[j] > s[k] or (s[j] == s[k] and j < k))]
            if stronger:
                out[aid] = rows[max(stronger, key=lambda j: s[j])][0]
    return out


def stages(session: Session, anomaly_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Research stage of each anomaly, from the hypotheses that cite it (detection -> paper-trading readiness)."""
    wanted = set(anomaly_ids)
    linked: dict[str, list[Hypothesis]] = {}
    for h in session.scalars(select(Hypothesis)).all():
        for aid in (h.contract or {}).get("source_anomaly_ids", []) or []:
            if aid in wanted:
                linked.setdefault(aid, []).append(h)
    order = {"detection": 0, "investigation": 1, "oos_failed": 2, "oos_passed": 3, "paper_trading_ready": 4}
    out = {}
    for aid in anomaly_ids:
        hyps = linked.get(aid, [])
        stage = "detection"
        for h in hyps:
            st = h.status
            s = ("paper_trading_ready" if st in ("PAPER_TRADING", "RISK_APPROVED", "LIVE_ELIGIBLE", "LIVE_ENABLED")
                 else "oos_passed" if st == "VALIDATION_PASSED"
                 else "oos_failed" if st in ("VALIDATION_FAILED", "REJECTED")
                 else "investigation")
            if order[s] > order[stage]:
                stage = s
        out[aid] = {"stage": stage, "hypotheses": [{"id": h.id, "title": h.title, "status": h.status} for h in hyps]}
    return out


STAGE_LABEL = {"detection": "Detected", "investigation": "Under investigation", "oos_failed": "OOS validation failed",
               "oos_passed": "OOS validation passed", "paper_trading_ready": "Paper-trading ready"}


def enrich(session: Session, items: list[AnomalyDetection], cfg: PriorityConfig = DEFAULT_CONFIG) -> dict[str, dict]:
    """Priority and stage for each anomaly, computed at read time from stored values (no migration needed)."""
    if not items:
        return {}
    ds_ids = sorted({a.dataset_id for a in items})
    datasets = {d.id: d for d in session.scalars(select(Dataset).where(Dataset.id.in_(ds_ids))).all()}
    verdicts = latest_verdicts(session, ds_ids)
    dups = _duplicates(session, items, datasets, cfg)
    stg = stages(session, [a.id for a in items])
    out = {}
    for a in items:
        ds = datasets.get(a.dataset_id)
        v = verdicts.get((a.dataset_id, a.detector))
        verdict = v["verdict"] if v and v.get("verdict") else "not_evaluated"
        pr = score(detector=a.detector, adjusted_p=a.adjusted_pvalue, stat_score=a.statistical_score,
                   econ=a.economic_score, verdict=verdict,
                   quality=(a.data_quality or {}).get("dataset_status") or (ds.quality_status if ds else "unknown"),
                   synthetic=bool(a.is_synthetic), duplicate_of=dups.get(a.id),
                   age_bars=_age_bars(a.event_ts, ds), cfg=cfg)
        pr["event_study"] = v
        st = stg[a.id]
        out[a.id] = {"priority": pr, "stage": {**st, "label": STAGE_LABEL[st["stage"]]}}
    return out


def config_dict(cfg: PriorityConfig = DEFAULT_CONFIG) -> dict[str, Any]:
    return asdict(cfg)
