from __future__ import annotations

from typing import Any

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, aliased

from qal.anomaly.detectors import DETECTORS
from qal.api.schemas import AnomalyOut, AnomalyPage, SavedQueryIn, SavedQueryOut
from qal.data.store import ParquetStore
from qal.db import get_session
from qal.models import AnomalyDetection, Dataset, Experiment, Instrument, SavedQuery

router = APIRouter(tags=["anomalies"])

SORTS = {
    "event_ts": AnomalyDetection.event_ts,
    "statistical_score": AnomalyDetection.statistical_score,
    "economic_score": AnomalyDetection.economic_score,
    "adjusted_pvalue": AnomalyDetection.adjusted_pvalue,
    "created_at": AnomalyDetection.created_at,
}


def _out(a: AnomalyDetection) -> AnomalyOut:
    o = AnomalyOut.model_validate(a)
    o.symbol, o.asset_class, o.market, o.provider_id = (a.instrument.symbol, a.instrument.asset_class,
                                                        a.instrument.market, a.instrument.provider_id)
    o.related_symbol = a.related_instrument.symbol if a.related_instrument else None
    return o


def _filtered(q: Select, *, asset_class, market, symbol, provider, frequency, anomaly_type, detector, status,
              min_stat_score, min_econ_score, synthetic, experiment_id, search) -> Select:
    q = q.join(Instrument, AnomalyDetection.instrument_id == Instrument.id)
    if asset_class:
        q = q.where(Instrument.asset_class.in_(asset_class))
    if market:
        q = q.where(Instrument.market == market)
    if symbol:
        q = q.where(Instrument.symbol.in_(symbol))
    if provider:
        q = q.where(Instrument.provider_id == provider)
    if frequency:
        q = q.where(AnomalyDetection.frequency.in_(frequency))
    if anomaly_type:
        q = q.where(AnomalyDetection.anomaly_type.in_(anomaly_type))
    if detector:
        q = q.where(AnomalyDetection.detector.in_(detector))
    if status:
        q = q.where(AnomalyDetection.validation_status.in_(status))
    if min_stat_score is not None:
        q = q.where(AnomalyDetection.statistical_score >= min_stat_score)
    if min_econ_score is not None:
        q = q.where(AnomalyDetection.economic_score >= min_econ_score)
    if synthetic is not None:
        q = q.where(AnomalyDetection.is_synthetic.is_(synthetic))
    if experiment_id:
        q = q.where(AnomalyDetection.experiment_id == experiment_id)
    if search:
        rel = aliased(Instrument)
        like = f"%{search.lower()}%"
        q = q.outerjoin(rel, AnomalyDetection.related_instrument_id == rel.id).where(or_(
            func.lower(Instrument.symbol).like(like), func.lower(rel.symbol).like(like),
            func.lower(AnomalyDetection.anomaly_type).like(like), func.lower(AnomalyDetection.detector).like(like)))
    return q


@router.get("/anomalies", response_model=AnomalyPage)
def list_anomalies(
    asset_class: list[str] = Query(default=[]),
    market: str | None = None,
    symbol: list[str] = Query(default=[]),
    provider: str | None = None,
    frequency: list[str] = Query(default=[], description="Horizon / bar frequency"),
    anomaly_type: list[str] = Query(default=[]),
    detector: list[str] = Query(default=[]),
    status: list[str] = Query(default=[]),
    min_stat_score: float | None = None,
    min_econ_score: float | None = None,
    synthetic: bool | None = None,
    experiment_id: str | None = None,
    latest_scan_only: bool = Query(False, description="Only anomalies from the most recent succeeded scan"),
    search: str | None = None,
    sort: str = "event_ts",
    order: str = "desc",
    limit: int = Query(100, le=1000),
    offset: int = 0,
    s: Session = Depends(get_session),
):
    if sort not in SORTS:
        raise HTTPException(400, f"sort must be one of {list(SORTS)}")
    if latest_scan_only and not experiment_id:
        experiment_id = s.scalar(select(Experiment.id).where(Experiment.kind == "anomaly_scan",
                                                             Experiment.status == "succeeded")
                                 .order_by(Experiment.created_at.desc()).limit(1))
    kw = dict(asset_class=asset_class, market=market, symbol=symbol, provider=provider, frequency=frequency,
              anomaly_type=anomaly_type, detector=detector, status=status, min_stat_score=min_stat_score,
              min_econ_score=min_econ_score, synthetic=synthetic, experiment_id=experiment_id, search=search)
    total = s.scalar(_filtered(select(func.count(AnomalyDetection.id)), **kw))
    col = SORTS[sort]
    q = _filtered(select(AnomalyDetection), **kw).order_by(
        col.desc().nulls_last() if order == "desc" else col.asc().nulls_last(), AnomalyDetection.id)
    items = s.scalars(q.limit(limit).offset(offset)).unique().all()
    return AnomalyPage(total=total or 0, items=[_out(a) for a in items])


@router.get("/anomalies/facets")
def anomaly_facets(s: Session = Depends(get_session)) -> dict[str, list[str]]:
    def distinct(col):
        return sorted(v for v in s.scalars(select(col).distinct()).all() if v)
    return {
        "asset_class": distinct(Instrument.asset_class), "market": distinct(Instrument.market),
        "provider": distinct(Instrument.provider_id), "frequency": distinct(AnomalyDetection.frequency),
        "anomaly_type": distinct(AnomalyDetection.anomaly_type), "detector": distinct(AnomalyDetection.detector),
        "status": distinct(AnomalyDetection.validation_status), "symbol": distinct(Instrument.symbol),
    }


@router.get("/anomalies/{anomaly_id}")
def anomaly_detail(anomaly_id: str, context_bars: int = Query(60, le=500), s: Session = Depends(get_session)) -> dict[str, Any]:
    """Anomaly with event chart data, reference distribution and a reproduction check.

    The detector is re-run on the stored dataset snapshot with the stored parameters; ``reproduced``
    is true when the same event and statistic come out. Bars after the event are shown for context
    only and were not used by the detector.
    """
    a = s.get(AnomalyDetection, anomaly_id)
    if a is None:
        raise HTTPException(404, "anomaly not found")
    exp = s.get(Experiment, a.experiment_id)
    ds = s.get(Dataset, a.dataset_id)
    df = ParquetStore.read(ds.path)
    other = ParquetStore.read(s.get(Dataset, a.related_dataset_id).path) if a.related_dataset_id else None
    params = exp.config["detectors"].get(a.detector, {})
    det = DETECTORS[a.detector](**params)
    res = det.run(df, other)
    series = res.series if res.series is not None else None
    ev = np.datetime64(a.event_ts.replace(tzinfo=None) if a.event_ts.tzinfo else a.event_ts, "ns")

    reproduced = False
    for d in res.detections:
        if np.datetime64(d.event_ts.tz_convert("UTC").tz_localize(None), "ns") == ev and abs(d.statistic - a.statistic) < 1e-9:
            reproduced = True
    reference: dict[str, Any] = {}
    stat_series: list[dict[str, Any]] = []
    if series is not None:
        idx = series.index.tz_localize(None) if getattr(series.index, "tz", None) is not None else series.index
        idx = np.asarray(idx, dtype="datetime64[ns]")
        k = int(np.searchsorted(idx, ev))
        w = int(params.get("window") or params.get("long_window") or 250)
        base = series.to_numpy()[max(0, k - w):k]
        base = base[np.isfinite(base)]
        if len(base):
            counts, edges = np.histogram(base, bins=30)
            reference = {"values_n": int(len(base)), "bins": edges.tolist(), "counts": counts.tolist(),
                         "event_value": a.statistic, "mean": float(base.mean()), "std": float(base.std()),
                         "quantiles": {q: float(np.quantile(base, q)) for q in (0.01, 0.05, 0.5, 0.95, 0.99)},
                         "label": f"{a.detector} statistic over the {len(base)} bars before the event"}
        lo, hi = max(0, k - context_bars), min(len(idx), k + context_bars // 2 + 1)
        stat_series = [{"event_ts": str(np.datetime_as_string(idx[i], unit="s")) + "Z",
                        "value": None if not np.isfinite(series.iloc[i]) else float(series.iloc[i])} for i in range(lo, hi)]

    ev_ts = df["event_ts"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
    j = int(np.searchsorted(ev_ts, ev))
    lo, hi = max(0, j - context_bars), min(len(df), j + context_bars // 2 + 1)

    def bars(frame, a_, b_):
        out = frame.iloc[a_:b_][["ts", "event_ts", "open", "high", "low", "close", "adj_close", "volume"]].copy()
        post = (out["event_ts"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy() > ev).tolist()
        recs = out.replace({np.nan: None}).to_dict(orient="records")
        for r, p in zip(recs, post, strict=True):
            r["ts"], r["event_ts"], r["post_event"] = r["ts"].isoformat(), r["event_ts"].isoformat(), bool(p)
        return recs

    context = bars(df, lo, hi)
    related_context = None
    if other is not None:
        ev2 = other["event_ts"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
        j2 = int(np.searchsorted(ev2, ev))
        related_context = bars(other, max(0, j2 - context_bars), min(len(other), j2 + context_bars // 2 + 1))

    return {
        "anomaly": _out(a).model_dump(mode="json"),
        "experiment": {"id": exp.id, "algorithm_version": exp.algorithm_version, "code_version": exp.code_version,
                       "config_hash": exp.config_hash, "created_at": exp.created_at, "n_tests": exp.n_tests,
                       "detector_params": params},
        "dataset": {"id": ds.id, "content_hash": ds.content_hash, "provider_id": ds.provider_id,
                    "source_ref": ds.source_ref, "rows": ds.rows, "start_ts": ds.start_ts, "end_ts": ds.end_ts,
                    "is_synthetic": ds.is_synthetic, "quality_status": ds.quality_status, "adjustment": ds.adjustment},
        "detector": {"name": det.name, "version": det.version, "description": det.description},
        "reproduced": reproduced,
        "context_bars": context,
        "related_context_bars": related_context,
        "statistic_series": stat_series,
        "reference_distribution": reference,
        "related_hypotheses": [],  # Phase 2 (hypothesis contracts)
        "historical_results": None,  # Phase 2 (backtests)
        "notes": [
            "Detected anomalies are observations, not trading signals; no backtest has been run.",
            "Economic score is a preliminary move-vs-assumed-cost ratio, not evidence of profitability.",
        ],
    }


@router.get("/saved-queries", response_model=list[SavedQueryOut])
def list_saved(s: Session = Depends(get_session)):
    return s.scalars(select(SavedQuery).order_by(SavedQuery.created_at.desc())).all()


@router.post("/saved-queries", response_model=SavedQueryOut, status_code=201)
def create_saved(body: SavedQueryIn, s: Session = Depends(get_session)):
    q = SavedQuery(name=body.name, filters=body.filters)
    s.add(q)
    s.flush()
    return q


@router.delete("/saved-queries/{qid}", status_code=204)
def delete_saved(qid: str, s: Session = Depends(get_session)):
    q = s.get(SavedQuery, qid)
    if q is None:
        raise HTTPException(404, "not found")
    s.delete(q)
