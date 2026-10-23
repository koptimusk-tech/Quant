from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProviderOut(ORM):
    id: str
    kind: str
    description: str
    is_synthetic: bool
    enabled: bool
    last_success_at: datetime | None
    last_error_at: datetime | None
    last_error: str | None
    last_latency_ms: float | None


class InstrumentOut(ORM):
    id: str
    provider_id: str
    symbol: str
    asset_class: str
    market: str
    currency: str | None


class DatasetOut(ORM):
    id: str
    instrument_id: str
    provider_id: str
    frequency: str
    content_hash: str
    rows: int
    start_ts: datetime | None
    end_ts: datetime | None
    is_synthetic: bool
    source_ref: str
    adjustment: str
    quality_status: str
    ingested_at: datetime
    is_latest: bool
    symbol: str | None = None
    asset_class: str | None = None


class QualityEventOut(ORM):
    id: str
    check: str
    severity: str
    message: str
    count: int
    details: dict[str, Any]


class IngestIn(BaseModel):
    adapter: Literal["local_file", "yahoo", "ccxt", "metatrader5", "stooq", "synthetic"]
    symbol: str = Field(examples=["SPY", "BTC/USDT", "EURUSD", "SYN-A"])
    asset_class: Literal["equity", "etf", "future", "fx", "crypto", "index", "other"]
    frequency: str = "1d"
    market: str = ""
    currency: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    incremental: bool = True
    options: dict[str, Any] = Field(default_factory=dict,
                                    description="Adapter options, e.g. {'path': ..., 'format': 'mt5'} for files")
    adapter_config: dict[str, Any] = Field(default_factory=dict,
                                           description="Non-secret adapter settings, e.g. {'exchange': 'kraken'}")


class IngestBatchIn(BaseModel):
    items: list[IngestIn]
    run_async: bool = False
    idempotency_key: str | None = None


class ScanIn(BaseModel):
    dataset_ids: list[str] = Field(default_factory=list)
    instrument_ids: list[str] = Field(default_factory=list,
                                      description="Resolved to each instrument's latest dataset when the scan runs")
    detectors: dict[str, dict[str, Any]] | None = None
    pairs: list[tuple[str, str]] = Field(default_factory=list, description="(dataset_id_y, dataset_id_x)")
    cost_bps: dict[str, float | None] = Field(default_factory=dict)
    min_dollar_volume: float = 1_000_000.0
    label: str = ""
    force: bool = False
    run_async: bool = False
    idempotency_key: str | None = None


class ExperimentOut(ORM):
    id: str
    kind: str
    hypothesis_id: str | None = None
    status: str
    algorithm_version: str
    code_version: str
    config: dict[str, Any]
    config_hash: str
    data_refs: list[dict[str, Any]]
    seed: int | None
    n_tests: int
    results: dict[str, Any]
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class AnomalyOut(ORM):
    id: str
    experiment_id: str
    detector: str
    detector_version: str
    anomaly_type: str
    instrument_id: str
    related_instrument_id: str | None
    dataset_id: str
    related_dataset_id: str | None
    frequency: str
    event_ts: datetime
    available_ts: datetime
    direction: int
    statistic: float
    empirical_pvalue: float | None
    adjusted_pvalue: float | None
    statistical_score: float
    economic_score: float | None
    metrics: dict[str, Any]
    economic_context: dict[str, Any]
    data_quality: dict[str, Any]
    alternative_explanations: list[str]
    is_synthetic: bool
    validation_status: str
    created_at: datetime
    symbol: str | None = None
    related_symbol: str | None = None
    asset_class: str | None = None
    market: str | None = None
    provider_id: str | None = None


class AnomalyPage(BaseModel):
    total: int
    items: list[AnomalyOut]


class JobOut(ORM):
    id: str
    kind: str
    payload: dict[str, Any]
    idempotency_key: str | None
    status: str
    attempts: int
    max_attempts: int
    result: dict[str, Any]
    error: str | None
    schedule_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ScheduleIn(BaseModel):
    name: str
    kind: Literal["ingest", "scan", "ingest_and_scan"] = "ingest_and_scan"
    payload: dict[str, Any]
    interval_seconds: int = Field(ge=60, description="Minimum 60 s")
    enabled: bool = True
    start_at: datetime | None = None


class SchedulePatch(BaseModel):
    name: str | None = None
    payload: dict[str, Any] | None = None
    interval_seconds: int | None = Field(default=None, ge=60)
    enabled: bool | None = None


class ScheduleOut(ORM):
    id: str
    name: str
    kind: str
    payload: dict[str, Any]
    interval_seconds: int
    enabled: bool
    next_run_at: datetime
    last_run_at: datetime | None
    created_at: datetime


class SavedQueryIn(BaseModel):
    name: str
    filters: dict[str, Any]


class SavedQueryOut(ORM):
    id: str
    name: str
    filters: dict[str, Any]
    created_at: datetime


class AuditOut(ORM):
    id: str
    actor: str
    action: str
    entity_type: str
    entity_id: str | None
    details: dict[str, Any]
    created_at: datetime
