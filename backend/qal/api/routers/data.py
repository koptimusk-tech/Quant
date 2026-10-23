from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qal.api.schemas import DatasetOut, IngestBatchIn, IngestIn, InstrumentOut, JobOut, ProviderOut, QualityEventOut
from qal.config import get_settings
from qal.data.adapters import ProviderError, provider_catalog
from qal.data.ingest import ingest
from qal.data.store import ParquetStore
from qal.db import get_session
from qal.jobs.runner import submit_job
from qal.jobs.tasks import ingest_request_from
from qal.models import DataQualityEvent, Dataset, Instrument, Provider

router = APIRouter(tags=["data"])


def _dataset_out(ds: Dataset) -> DatasetOut:
    out = DatasetOut.model_validate(ds)
    out.symbol = ds.instrument.symbol
    out.asset_class = ds.instrument.asset_class
    return out


@router.get("/providers", response_model=list[ProviderOut])
def list_providers(s: Session = Depends(get_session)):
    return s.scalars(select(Provider).order_by(Provider.id)).all()


@router.get("/providers/catalog")
def catalog() -> list[dict[str, Any]]:
    """Available adapters with licence, history, delay and limit notes (see docs/DATA_PROVIDERS.md)."""
    return provider_catalog()


@router.get("/instruments", response_model=list[InstrumentOut])
def list_instruments(s: Session = Depends(get_session)):
    return s.scalars(select(Instrument).order_by(Instrument.symbol)).all()


@router.get("/datasets", response_model=list[DatasetOut])
def list_datasets(latest_only: bool = True, s: Session = Depends(get_session)):
    q = select(Dataset).order_by(Dataset.ingested_at.desc())
    if latest_only:
        q = q.where(Dataset.is_latest.is_(True))
    return [_dataset_out(d) for d in s.scalars(q).all()]


@router.get("/datasets/{dataset_id}", response_model=DatasetOut)
def get_dataset(dataset_id: str, s: Session = Depends(get_session)):
    ds = s.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, "dataset not found")
    return _dataset_out(ds)


@router.get("/datasets/{dataset_id}/quality", response_model=list[QualityEventOut])
def dataset_quality(dataset_id: str, s: Session = Depends(get_session)):
    return s.scalars(select(DataQualityEvent).where(DataQualityEvent.dataset_id == dataset_id)).all()


@router.get("/datasets/{dataset_id}/bars")
def dataset_bars(dataset_id: str, start: datetime | None = None, end: datetime | None = None,
                 limit: int = Query(2000, le=20000), s: Session = Depends(get_session)) -> dict[str, Any]:
    ds = s.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, "dataset not found")
    df = ParquetStore.read(ds.path)
    if start:
        df = df[df["ts"] >= start]
    if end:
        df = df[df["ts"] <= end]
    df = df.tail(limit)
    cols = ["ts", "event_ts", "available_ts", "received_ts", "open", "high", "low", "close", "adj_close", "volume"]
    recs = df[cols].replace({np.nan: None}).to_dict(orient="records")
    for r in recs:
        for c in ("ts", "event_ts", "available_ts", "received_ts"):
            r[c] = r[c].isoformat()
    return {"dataset_id": ds.id, "is_synthetic": ds.is_synthetic, "bars": recs}


def _ingest_now(s: Session, items: list[IngestIn]) -> dict[str, Any]:
    results, errors = [], []
    for item in items:
        try:
            results.append(ingest(s, ingest_request_from(item.model_dump(mode="json")), actor="api"))
            s.commit()
        except (ProviderError, ValueError) as exc:
            s.commit()  # keep provider error state + audit entry
            errors.append({"symbol": item.symbol, "adapter": item.adapter, "error": f"{type(exc).__name__}: {exc}"})
    return {"ingested": results, "errors": errors}


@router.post("/data/ingest")
def ingest_data(body: IngestBatchIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Download/load data now, or enqueue a background job with ``run_async``."""
    if body.run_async:
        job = submit_job(s, "ingest", {"items": [i.model_dump(mode="json") for i in body.items]},
                         idempotency_key=body.idempotency_key)
        return {"job": JobOut.model_validate(job).model_dump(mode="json")}
    out = _ingest_now(s, body.items)
    if out["errors"] and not out["ingested"]:
        raise HTTPException(502, detail=out["errors"])
    return out


@router.post("/data/upload")
def upload_file(
    file: UploadFile = File(...),
    symbol: str = Form(...),
    asset_class: str = Form(...),
    frequency: str = Form("1d"),
    market: str = Form(""),
    format: str = Form("auto"),
    source_timezone: str = Form("UTC"),
    availability_delay_minutes: float = Form(0),
    s: Session = Depends(get_session),
) -> dict[str, Any]:
    """Upload a CSV/Parquet/MT4/MT5 export; it is copied into the data directory and ingested."""
    up_dir = Path(get_settings().data_dir) / "uploads"
    up_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "upload.csv").suffix or ".csv"
    dest = up_dir / f"{uuid.uuid4().hex}{suffix}"
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    item = IngestIn(adapter="local_file", symbol=symbol, asset_class=asset_class, frequency=frequency, market=market,
                    incremental=False, options={"path": str(dest), "format": format, "source_timezone": source_timezone,
                                                "availability_delay_minutes": availability_delay_minutes})
    out = _ingest_now(s, [item])
    if out["errors"]:
        raise HTTPException(400, detail=out["errors"])
    return out


@router.get("/data/health")
def data_health(s: Session = Depends(get_session)) -> dict[str, Any]:
    """Provider status, freshness, coverage, gaps, quality, latency and errors."""
    now = datetime.now(UTC)
    providers = []
    for p in s.scalars(select(Provider).order_by(Provider.id)).all():
        n_ds = s.scalar(select(func.count()).select_from(Dataset).where(Dataset.provider_id == p.id,
                                                                         Dataset.is_latest.is_(True)))
        providers.append({**ProviderOut.model_validate(p).model_dump(mode="json"), "datasets": n_ds})
    datasets = []
    for d in s.scalars(select(Dataset).where(Dataset.is_latest.is_(True)).order_by(Dataset.provider_id)).all():
        m = (d.quality_summary or {}).get("metrics", {})
        issues = (d.quality_summary or {}).get("issues", [])
        end = d.end_ts if d.end_ts is None or d.end_ts.tzinfo else d.end_ts.replace(tzinfo=UTC)
        datasets.append({
            "dataset_id": d.id, "provider_id": d.provider_id, "symbol": d.instrument.symbol,
            "asset_class": d.instrument.asset_class, "frequency": d.frequency, "rows": d.rows,
            "start_ts": d.start_ts, "end_ts": d.end_ts, "is_synthetic": d.is_synthetic,
            "quality_status": d.quality_status, "coverage": m.get("coverage"), "missing_bars": m.get("missing_bars"),
            "n_gaps": m.get("n_gaps"), "largest_gaps": m.get("gaps", [])[:5],
            "age_hours": None if end is None else round((now - end).total_seconds() / 3600, 1),
            "stale": m.get("stale"), "issues": [{"check": i["check"], "severity": i["severity"],
                                                 "message": i["message"], "count": i["count"]} for i in issues],
            "ingested_at": d.ingested_at,
        })
    return {"generated_at": now, "providers": providers, "datasets": datasets}
