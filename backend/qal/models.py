"""ORM data model.

Phase 1 covers providers, instruments, market datasets, data quality, the experiment
registry, anomaly detections, background jobs, schedules, saved queries and audit events.
Later-phase entities (hypotheses, backtests, strategies, risk policies, gate decisions,
paper orders, positions) are listed in ARCHITECTURE.md and arrive with their phases.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from qal.db import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. "yahoo", "ccxt:binance"
    kind: Mapped[str] = mapped_column(String(32))  # adapter name
    description: Mapped[str] = mapped_column(Text, default="")
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_latency_ms: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("provider_id", "symbol", name="uq_instrument_provider_symbol"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"))
    symbol: Mapped[str] = mapped_column(String(64))
    asset_class: Mapped[str] = mapped_column(String(16))  # equity, etf, future, fx, crypto, index, other
    market: Mapped[str] = mapped_column(String(64), default="")  # venue / exchange
    currency: Mapped[str | None] = mapped_column(String(16))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Dataset(Base):
    """An immutable, content-addressed snapshot of bars for one instrument and frequency."""

    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.id"))
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"))
    frequency: Mapped[str] = mapped_column(String(8))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    path: Mapped[str] = mapped_column(Text)
    rows: Mapped[int] = mapped_column(Integer)
    start_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    source_ref: Mapped[str] = mapped_column(Text, default="")
    adjustment: Mapped[str] = mapped_column(String(32), default="raw")
    quality_status: Mapped[str] = mapped_column(String(16), default="unknown")  # pass, warn, fail
    quality_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=True)

    instrument: Mapped[Instrument] = relationship()


class DataQualityEvent(Base):
    __tablename__ = "data_quality_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    check: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(8))  # info, warn, fail
    message: Mapped[str] = mapped_column(Text)
    count: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Experiment(Base):
    """Experiment registry. Every run is stored, including failed and empty ones."""

    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))  # anomaly_scan, backtest, validation, final_test
    hypothesis_id: Mapped[str | None] = mapped_column(ForeignKey("hypotheses.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running, succeeded, failed
    algorithm_version: Mapped[str] = mapped_column(Text)
    code_version: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    data_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    seed: Mapped[int | None] = mapped_column(Integer)
    n_tests: Mapped[int] = mapped_column(Integer, default=0)
    results: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnomalyDetection(Base):
    __tablename__ = "anomaly_detections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    detector: Mapped[str] = mapped_column(String(64))
    detector_version: Mapped[str] = mapped_column(String(16))
    anomaly_type: Mapped[str] = mapped_column(String(32), index=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.id"), index=True)
    related_instrument_id: Mapped[str | None] = mapped_column(ForeignKey("instruments.id"))
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"))
    related_dataset_id: Mapped[str | None] = mapped_column(ForeignKey("datasets.id"))
    frequency: Mapped[str] = mapped_column(String(8))
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    available_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    direction: Mapped[int] = mapped_column(Integer, default=0)
    statistic: Mapped[float] = mapped_column(Float)  # e.g. robust z
    empirical_pvalue: Mapped[float | None] = mapped_column(Float)
    adjusted_pvalue: Mapped[float | None] = mapped_column(Float)  # BH within the scan
    statistical_score: Mapped[float] = mapped_column(Float)  # 0..1
    economic_score: Mapped[float | None] = mapped_column(Float)  # 0..1, preliminary
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    economic_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    data_quality: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    alternative_explanations: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    validation_status: Mapped[str] = mapped_column(String(32), default="DISCOVERED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    instrument: Mapped[Instrument] = relationship(foreign_keys=[instrument_id])
    related_instrument: Mapped[Instrument | None] = relationship(foreign_keys=[related_instrument_id])


class Job(Base):
    """Persistent background job with retries and an idempotency key."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))  # ingest, scan
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued, running, succeeded, failed, cancelled
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    schedule_id: Mapped[str | None] = mapped_column(ForeignKey("schedules.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Schedule(Base):
    """Recurring job (e.g. automatic data download followed by a scan)."""

    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    interval_seconds: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SavedQuery(Base):
    __tablename__ = "saved_queries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128))
    filters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    actor: Mapped[str] = mapped_column(String(64))  # "system", "api", "scheduler", later agent ids
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Hypothesis(Base):
    """A falsifiable alpha hypothesis. The contract (incl. success criteria) is frozen once created."""

    __tablename__ = "hypotheses"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    family_id: Mapped[str] = mapped_column(String(32), index=True)  # shared by all versions
    version: Mapped[int] = mapped_column(Integer, default=1)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("hypotheses.id"))
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="HYPOTHESIS_FORMULATED", index=True)
    contract: Mapped[dict[str, Any]] = mapped_column(JSON)
    contract_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64), default="api")
    final_test_accesses: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GateDecision(Base):
    """Deterministic policy decision. Only the policy engine writes these."""

    __tablename__ = "gate_decisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    hypothesis_id: Mapped[str | None] = mapped_column(ForeignKey("hypotheses.id"), index=True)
    experiment_id: Mapped[str | None] = mapped_column(ForeignKey("experiments.id"))
    gate: Mapped[str] = mapped_column(String(8))  # A..F
    decision: Mapped[str] = mapped_column(String(8))  # pass, block
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    policy_version: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
