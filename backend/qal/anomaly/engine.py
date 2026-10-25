"""Anomaly scan: runs detectors over registered datasets and records an experiment.

* Gate A: datasets whose quality status is ``fail`` are blocked and listed in the results.
* Multiple testing: every evaluated bar counts as a test; Benjamini-Hochberg adjusted p-values
  are computed over all tests of the scan (all detectors, all instruments).
* Reproducibility: the experiment stores config, detector versions, code version and the
  content hashes of the datasets; an identical request returns the cached experiment.
* Negative results are kept: a scan with zero anomalies or a failed scan is still recorded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal import audit
from qal.anomaly.base import Detection, Detector
from qal.anomaly.detectors import DETECTORS
from qal.data.store import ParquetStore
from qal.models import AnomalyDetection, Dataset, Experiment, Instrument
from qal.versioning import code_version, stable_hash

# Placeholder round-trip cost assumptions (bps) for the *preliminary* economic score.
# They are not a cost model: Phase 2 adds per-instrument commissions, spreads, slippage and funding.
DEFAULT_COST_BPS: dict[str, float | None] = {
    "equity": 10.0, "etf": 5.0, "future": 4.0, "fx": 3.0, "crypto": 20.0, "index": None, "other": 10.0,
}
DEFAULT_MIN_DOLLAR_VOLUME = 1_000_000.0

# Version of the preliminary economic score. 2: detectors whose statistic is not a price move get no score.
ECONOMIC_METHOD = "move_vs_cost@2"

# Detectors whose detected variable has no tradable price magnitude. Scoring them with move_bps / cost would
# compare an incompatible variable with a cost (a volatility level, or the price return of a bar that was
# flagged for its volume). Their economic relevance can only come from the event study of later returns.
NO_PRICE_MAGNITUDE: dict[str, str] = {
    "volatility_regime": "A volatility regime change is a change in the scale of returns, not a return that can be "
                         "traded; its size is not compared with costs. Economic relevance can only come from the "
                         "event study of the returns that follow.",
    "volume_anomaly": "The detected variable is volume, which has no price magnitude; the bar's price return is a "
                      "different variable and is not used as the event's size. Economic relevance can only come "
                      "from the event study of the returns that follow.",
    "isolation_forest": "A multivariate outlier is an unusual combination of features, not a price move; the bar's "
                        "return is not the detected variable. Economic relevance can only come from the event study.",
    "local_outlier_factor": "A multivariate outlier is an unusual combination of features, not a price move; the "
                            "bar's return is not the detected variable. Economic relevance can only come from the "
                            "event study.",
}


@dataclass
class ScanConfig:
    dataset_ids: list[str] = field(default_factory=list)
    detectors: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {"return_shock": {}, "volume_anomaly": {}, "volatility_regime": {}}
    )
    pairs: list[tuple[str, str]] = field(default_factory=list)  # (dataset_id_y, dataset_id_x)
    cost_bps: dict[str, float | None] = field(default_factory=dict)
    min_dollar_volume: float = DEFAULT_MIN_DOLLAR_VOLUME
    label: str = ""
    force: bool = False


def benjamini_hochberg(p: np.ndarray) -> np.ndarray:
    p = np.asarray(p, dtype=float)
    m = len(p)
    if m == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * m / np.arange(1, m + 1)
    adj = np.minimum.accumulate(ranked[::-1])[::-1].clip(max=1.0)
    out = np.empty(m)
    out[order] = adj
    return out


def economic_context(det: Detection, inst: Instrument, df: pd.DataFrame, cost_bps: dict[str, float | None],
                     min_dollar_volume: float, legs: int = 1) -> tuple[float | None, dict[str, Any]]:
    """Preliminary tradability context. Not evidence of alpha; no backtest is involved."""
    cost = cost_bps.get(inst.asset_class, DEFAULT_COST_BPS.get(inst.asset_class))
    ctx: dict[str, Any] = {"assumed_round_trip_cost_bps": cost, "legs": legs, "method": ECONOMIC_METHOD,
                           "note": "Preliminary: move size vs a placeholder cost assumption. Not a backtest."}
    prior = df[df["event_ts"] < det.event_ts].tail(20)
    dv = (prior["close"] * prior["volume"]).median() if prior["volume"].notna().any() else np.nan
    ctx["median_dollar_volume_20"] = None if not np.isfinite(dv) else float(dv)
    if cost is None:
        ctx["reason"] = f"Asset class {inst.asset_class!r} is not directly tradable; use an ETF/future proxy."
        return None, ctx
    if det.detector in NO_PRICE_MAGNITUDE:
        ctx["reason"] = NO_PRICE_MAGNITUDE[det.detector]
        return None, ctx
    if det.move_bps is None:
        ctx["reason"] = "No price move associated with the event."
        return None, ctx
    total_cost = cost * legs
    ratio = det.move_bps / total_cost if total_cost > 0 else np.inf
    score = float(ratio / (ratio + 3.0))
    ctx.update({"move_bps": round(det.move_bps, 2), "move_to_cost_ratio": round(float(ratio), 3)})
    if np.isfinite(dv) and dv < min_dollar_volume:
        score *= 0.5
        ctx["liquidity_flag"] = f"Median dollar volume {dv:,.0f} below {min_dollar_volume:,.0f}"
    elif not np.isfinite(dv):
        ctx["liquidity_flag"] = "No volume data to assess liquidity"
    return score, ctx


def _data_quality_for(ds: Dataset) -> dict[str, Any]:
    summ = ds.quality_summary or {}
    m = summ.get("metrics", {})
    return {"dataset_status": ds.quality_status, "coverage": m.get("coverage"), "missing_bars": m.get("missing_bars"),
            "n_gaps": m.get("n_gaps"), "issues": [i["check"] for i in summ.get("issues", [])]}


def run_scan(session: Session, cfg: ScanConfig, *, store: ParquetStore, actor: str = "api") -> Experiment:
    datasets: dict[str, Dataset] = {}
    wanted = set(cfg.dataset_ids) | {d for p in cfg.pairs for d in p}
    for ds_id in sorted(wanted):
        ds = session.get(Dataset, ds_id)
        if ds is None:
            raise ValueError(f"Unknown dataset {ds_id}")
        datasets[ds_id] = ds
    for name, params in cfg.detectors.items():
        if name not in DETECTORS:
            raise ValueError(f"Unknown detector {name!r}; available: {list(DETECTORS)}")
        DETECTORS[name](**(params or {}))  # validate parameters early
    if cfg.pairs and "pair_divergence" not in cfg.detectors:
        cfg.detectors = {**cfg.detectors, "pair_divergence": {}}

    detectors: dict[str, Detector] = {n: DETECTORS[n](**(p or {})) for n, p in cfg.detectors.items()}
    config = {
        "dataset_ids": sorted(cfg.dataset_ids), "pairs": [list(p) for p in cfg.pairs],
        "detectors": {n: d.params for n, d in detectors.items()},
        "cost_bps": {**DEFAULT_COST_BPS, **cfg.cost_bps}, "min_dollar_volume": cfg.min_dollar_volume,
        "label": cfg.label,
    }
    data_refs = [{"dataset_id": d.id, "content_hash": d.content_hash, "instrument_id": d.instrument_id,
                  "provider": d.provider_id, "frequency": d.frequency, "rows": d.rows,
                  "is_synthetic": d.is_synthetic, "quality_status": d.quality_status}
                 for d in datasets.values()]
    algo = "+".join(f"{d.name}@{d.version}" for d in detectors.values())
    config_hash = stable_hash({"config": {k: v for k, v in config.items() if k != "label"},
                               "data": sorted(r["content_hash"] for r in data_refs), "algo": algo})

    if not cfg.force:
        cached = session.scalar(select(Experiment).where(Experiment.config_hash == config_hash,
                                                         Experiment.status == "succeeded"))
        if cached is not None:
            audit.record(session, actor, "scan_cache_hit", "experiment", cached.id)
            return cached

    exp = Experiment(kind="anomaly_scan", status="running", algorithm_version=algo, code_version=code_version(),
                     config=config, config_hash=config_hash, data_refs=data_refs, seed=None)
    session.add(exp)
    session.flush()
    t0 = time.perf_counter()
    try:
        _execute(session, exp, cfg, detectors, datasets, store, config["cost_bps"])
        exp.status = "succeeded"
    except Exception as exc:
        exp.status = "failed"
        exp.error = f"{type(exc).__name__}: {exc}"
        for obj in [o for o in session.new if isinstance(o, AnomalyDetection)]:
            session.expunge(obj)
    exp.results = {**(exp.results or {}), "duration_s": round(time.perf_counter() - t0, 3)}
    exp.finished_at = datetime.now(UTC)
    audit.record(session, actor, "scan_finished", "experiment", exp.id, status=exp.status,
                 n_anomalies=exp.results.get("n_anomalies"), error=exp.error)
    return exp


def _execute(session: Session, exp: Experiment, cfg: ScanConfig, detectors: dict[str, Detector],
             datasets: dict[str, Dataset], store: ParquetStore, cost_bps: dict[str, float | None]) -> None:
    blocked = {d.id: d.quality_status for d in datasets.values() if d.quality_status == "fail"}
    frames = {d.id: store.read(d.path) for d in datasets.values() if d.id not in blocked}
    instruments = {d.id: session.get(Instrument, d.instrument_id) for d in datasets.values()}

    pending: list[tuple[Detection, str, str | None]] = []
    all_p: list[np.ndarray] = []
    per_detector: dict[str, dict[str, Any]] = {}
    not_applicable: list[dict[str, str]] = []

    def collect(name: str, out, ds_id: str, rel_id: str | None = None) -> None:
        stats = per_detector.setdefault(name, {"n_tests": 0, "n_detections": 0})
        if not out.applicable:
            not_applicable.append({"detector": name, "dataset_id": ds_id, "reason": out.reason})
            return
        stats["n_tests"] += out.n_tests
        stats["n_detections"] += len(out.detections)
        all_p.append(out.pvalues[np.isfinite(out.pvalues)])
        pending.extend((d, ds_id, rel_id) for d in out.detections)

    for ds_id in cfg.dataset_ids:
        if ds_id in blocked:
            continue
        for name, det in detectors.items():
            if not det.pairwise:
                collect(name, det.run(frames[ds_id]), ds_id)
    if "pair_divergence" in detectors:
        for y_id, x_id in cfg.pairs:
            if y_id in blocked or x_id in blocked:
                continue
            collect("pair_divergence", detectors["pair_divergence"].run(frames[y_id], frames[x_id]), y_id, x_id)

    pvals = np.concatenate(all_p) if all_p else np.array([])
    n_tests = int(len(pvals))
    # BH over every test of the scan. Detected events are matched to their adjusted value by
    # recomputing on (all p) and looking up each event's raw p rank.
    adjusted_sorted = np.sort(benjamini_hochberg(pvals)) if n_tests else np.array([])
    sorted_p = np.sort(pvals)

    def adjusted(p: float) -> float:
        k = int(np.searchsorted(sorted_p, p, side="right")) - 1
        return float(adjusted_sorted[max(k, 0)]) if n_tests else 1.0

    for det, ds_id, rel_id in pending:
        ds, inst = datasets[ds_id], instruments[ds_id]
        econ, ctx = economic_context(det, inst, frames[ds_id], cost_bps, cfg.min_dollar_volume,
                                     legs=2 if rel_id else 1)
        dq = _data_quality_for(ds)
        if rel_id:
            dq["related_dataset_status"] = datasets[rel_id].quality_status
        session.add(AnomalyDetection(
            experiment_id=exp.id, detector=det.detector, detector_version=det.version,
            anomaly_type=det.anomaly_type, instrument_id=inst.id,
            related_instrument_id=instruments[rel_id].id if rel_id else None,
            dataset_id=ds_id, related_dataset_id=rel_id, frequency=ds.frequency,
            event_ts=det.event_ts.to_pydatetime(), available_ts=det.available_ts.to_pydatetime(),
            direction=det.direction, statistic=det.statistic, empirical_pvalue=det.empirical_pvalue,
            adjusted_pvalue=adjusted(det.empirical_pvalue), statistical_score=detectors[det.detector].severity(det.statistic),
            economic_score=econ, metrics=det.metrics, economic_context=ctx, data_quality=dq,
            alternative_explanations=det.alternative_explanations,
            is_synthetic=bool(ds.is_synthetic or (rel_id and datasets[rel_id].is_synthetic)),
            validation_status="DISCOVERED",
        ))
    exp.n_tests = n_tests
    exp.results = {
        "n_anomalies": len(pending), "n_tests": n_tests, "per_detector": per_detector,
        "blocked_by_gate_a": [{"dataset_id": k, "quality_status": v} for k, v in blocked.items()],
        "not_applicable": not_applicable,
        "multiple_testing": "Benjamini-Hochberg over all evaluated bars of all detectors in this scan; "
                            "empirical p-values are rank-based and optimistic under volatility clustering.",
    }
