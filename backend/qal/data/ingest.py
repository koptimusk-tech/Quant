"""Ingestion service: fetch -> normalise -> merge -> quality check (Gate A) -> store -> register."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal import audit
from qal.config import get_settings
from qal.data.adapters import DataAdapter, FetchRequest, build_adapter
from qal.data.quality import QualityIssue, check_bars
from qal.data.schema import InstrumentSpec, bar_delta, normalize_bars
from qal.data.store import ParquetStore
from qal.models import DataQualityEvent, Dataset, Instrument, Provider

PRICE_COLS = ["open", "high", "low", "close", "volume"]


@dataclass
class IngestRequest:
    adapter: str
    symbol: str
    asset_class: str
    frequency: str = "1d"
    market: str = ""
    currency: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    incremental: bool = True
    options: dict[str, Any] = field(default_factory=dict)
    adapter_config: dict[str, Any] = field(default_factory=dict)


def ensure_provider(session: Session, adapter: DataAdapter) -> Provider:
    prov = session.get(Provider, adapter.provider_id)
    if prov is None:
        cat = adapter.catalog()
        prov = Provider(id=adapter.provider_id, kind=adapter.kind, description=cat.name,
                        is_synthetic=adapter.is_synthetic)
        session.add(prov)
        session.flush()
    return prov


def ensure_instrument(session: Session, provider_id: str, spec: InstrumentSpec) -> Instrument:
    inst = session.scalar(
        select(Instrument).where(Instrument.provider_id == provider_id, Instrument.symbol == spec.symbol)
    )
    if inst is None:
        inst = Instrument(provider_id=provider_id, symbol=spec.symbol, asset_class=spec.asset_class,
                          market=spec.market, currency=spec.currency, timezone=spec.timezone)
        session.add(inst)
        session.flush()
    return inst


def latest_dataset(session: Session, instrument_id: str, frequency: str) -> Dataset | None:
    return session.scalar(
        select(Dataset).where(Dataset.instrument_id == instrument_id, Dataset.frequency == frequency,
                              Dataset.is_latest.is_(True))
    )


def _merge(prev: pd.DataFrame, new: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Append new bars to the previous snapshot. Overlapping bars that changed are revisions."""
    overlap = prev.merge(new, on="ts", suffixes=("_old", "_new"))
    revised = pd.Series(False, index=overlap.index)
    for c in PRICE_COLS:
        a, b = overlap[f"{c}_old"], overlap[f"{c}_new"]
        revised |= ~((a - b).abs() <= 1e-9 * b.abs().clip(lower=1) ) & ~(a.isna() & b.isna())
    info = {"overlap_bars": int(len(overlap)), "revised_bars": int(revised.sum()),
            "revised_ts": [str(t) for t in overlap.loc[revised, "ts"].head(10)]}
    # Unchanged overlapping bars keep their original received_ts (first time we saw them).
    keep_old = prev[~prev["ts"].isin(new["ts"])]
    unchanged = set(overlap.loc[~revised, "ts"])
    new = new.copy()
    if unchanged:
        first_seen = prev.set_index("ts")["received_ts"]
        m = new["ts"].isin(unchanged)
        new.loc[m, "received_ts"] = new.loc[m, "ts"].map(first_seen)
    merged = pd.concat([keep_old, new], ignore_index=True).sort_values("ts", kind="stable").reset_index(drop=True)
    return merged, info


def ingest(session: Session, req: IngestRequest, *, adapter: DataAdapter | None = None,
           actor: str = "api", now: datetime | None = None) -> dict[str, Any]:
    settings = get_settings()
    adapter = adapter or build_adapter(req.adapter, **req.adapter_config)
    spec = InstrumentSpec(req.symbol, req.asset_class, req.market, req.currency)
    provider = ensure_provider(session, adapter)
    inst = ensure_instrument(session, provider.id, spec)
    prev = latest_dataset(session, inst.id, req.frequency) if req.incremental else None
    store = ParquetStore(settings.data_dir)

    start = req.start
    prev_df = None
    if prev is not None:
        prev_df = store.read(prev.path)
        if start is None and len(prev_df):
            start = (prev_df["ts"].iloc[-1] - 5 * bar_delta(req.frequency)).to_pydatetime()

    freq_req = FetchRequest(spec, req.frequency, start, req.end, dict(req.options))
    t0 = time.perf_counter()
    try:
        raw = adapter.fetch(freq_req)
    except Exception as exc:
        provider.last_error_at = datetime.now(UTC)
        provider.last_error = f"{type(exc).__name__}: {exc}"
        audit.record(session, actor, "ingest_failed", "instrument", inst.id, error=provider.last_error,
                     adapter=req.adapter, symbol=req.symbol)
        raise
    provider.last_latency_ms = (time.perf_counter() - t0) * 1000

    bars, norm = normalize_bars(
        raw, provider=provider.id, spec=spec, frequency=req.frequency,
        availability_delay=adapter.availability_delay(freq_req), is_synthetic=adapter.is_synthetic,
        source_ref=adapter.source_ref(freq_req), received_ts=now,
    )
    merge_info: dict[str, Any] = {}
    if prev_df is not None and len(prev_df):
        bars, merge_info = _merge(prev_df, bars)

    report = check_bars(bars, frequency=req.frequency, asset_class=spec.asset_class, normalization=norm, now=now)
    if merge_info.get("revised_bars"):
        report.issues.append(QualityIssue("revisions", "warn", "Provider revised previously stored bars",
                                          merge_info["revised_bars"], merge_info))
        if report.status == "pass":
            report.status = "warn"

    path, digest = store.write(bars, provider.id, spec.symbol, req.frequency)
    provider.last_success_at = datetime.now(UTC)
    provider.last_error = None

    if prev is not None and prev.content_hash == digest:
        audit.record(session, actor, "ingest_unchanged", "dataset", prev.id, rows=prev.rows)
        return {"dataset_id": prev.id, "instrument_id": inst.id, "symbol": spec.symbol, "unchanged": True, "rows": prev.rows,
                "quality_status": prev.quality_status, "content_hash": digest}

    if prev is not None:
        prev.is_latest = False
    ds = Dataset(
        instrument_id=inst.id, provider_id=provider.id, frequency=req.frequency, content_hash=digest,
        path=str(path), rows=len(bars), start_ts=bars["ts"].iloc[0].to_pydatetime() if len(bars) else None,
        end_ts=bars["ts"].iloc[-1].to_pydatetime() if len(bars) else None, is_synthetic=adapter.is_synthetic,
        source_ref=adapter.source_ref(freq_req), adjustment=adapter.adjustment, quality_status=report.status,
        quality_summary=report.summary(), is_latest=True,
    )
    session.add(ds)
    session.flush()
    for issue in report.issues:
        session.add(DataQualityEvent(dataset_id=ds.id, check=issue.check, severity=issue.severity,
                                     message=issue.message, count=issue.count, details=issue.details))
    audit.record(session, actor, "dataset_created", "dataset", ds.id, rows=ds.rows, hash=digest,
                 quality=report.status, provider=provider.id, symbol=spec.symbol, merge=merge_info)
    return {"dataset_id": ds.id, "instrument_id": inst.id, "symbol": spec.symbol, "unchanged": False, "rows": ds.rows,
            "quality_status": report.status, "content_hash": digest, "merge": merge_info}
