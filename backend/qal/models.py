"""ORM data model.

Phase 1 covers providers, instruments, market datasets, data quality, the experiment
registry, anomaly detections, background jobs, schedules, saved queries and audit events.
Later phases added hypotheses, gate decisions, agent messages, risk policies and risk decisions (Phase 6)
and the simulated paper trading tables (Phase 7: sessions, signals, orders, fills, positions, cash ledger,
equity snapshots and events). Nothing here connects to a broker: paper orders are filled only by the
execution simulator.
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
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued, running, cancelling, succeeded, failed, cancelled
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


class AgentMessage(Base):
    """Typed message between research agents. Payloads carry references to stored evidence, never free numbers.

    ``kind``: idea, data_gap, formalization, objection, decision, experiment_result. Objections from the
    adversarial reviewer are kept for every hypothesis, including the ones that were not registered.
    """

    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    sender: Mapped[str] = mapped_column(String(32))
    receiver: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32), index=True)
    subject_key: Mapped[str] = mapped_column(String(64), index=True)  # idea key (stable across runs)
    hypothesis_id: Mapped[str | None] = mapped_column(ForeignKey("hypotheses.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RiskPolicy(Base):
    """Versioned, immutable limits of the Hierarchical Risk Manager. Only a person creates new versions."""

    __tablename__ = "risk_policies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    limits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    policy_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RiskDecision(Base):
    """One rule checked by the risk manager: approved or blocked, with observed value, limit and reason."""

    __tablename__ = "risk_decisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    policy_id: Mapped[str] = mapped_column(ForeignKey("risk_policies.id"))
    policy_version: Mapped[str] = mapped_column(String(96))
    level: Mapped[str] = mapped_column(String(16))  # strategy, group, portfolio
    scope: Mapped[str] = mapped_column(String(128))
    rule: Mapped[str] = mapped_column(String(64), index=True)
    observed: Mapped[str | None] = mapped_column(Text)
    limit_value: Mapped[str] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(String(16), index=True)  # approved, blocked
    action: Mapped[str] = mapped_column(String(256), default="")
    reason: Mapped[str] = mapped_column(Text)
    as_of: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# -- Paper trading (simulation only) ---------------------------------------------------------------
class PaperSession(Base):
    """A simulated paper trading session. Reproducible from config, code version and data refs."""

    __tablename__ = "paper_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="CREATED", index=True)
    mode: Mapped[str] = mapped_column(String(16))  # replay, new_data
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    config_hash: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    config_history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    criteria: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    criteria_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(64))
    data_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"))
    policy_id: Mapped[str] = mapped_column(ForeignKey("risk_policies.id"))
    policy_version: Mapped[str] = mapped_column(String(96))
    initial_capital: Mapped[float] = mapped_column(Float)
    account_currency: Mapped[str] = mapped_column(String(8), default="USD")
    cash: Mapped[float] = mapped_column(Float)
    peak_equity: Mapped[float] = mapped_column(Float)
    day_start_equity: Mapped[float] = mapped_column(Float)
    day: Mapped[str | None] = mapped_column(String(10))
    cursor_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clock_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bars_processed: Mapped[int] = mapped_column(Integer, default=0)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_reason: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evaluation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaperSignal(Base):
    """Target weights a strategy decided at the close of one bar (before any order or risk check)."""

    __tablename__ = "paper_signals"
    __table_args__ = (UniqueConstraint("session_id", "strategy_id", "bar_ts", name="uq_paper_signal"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("paper_sessions.id"), index=True)
    strategy_id: Mapped[str] = mapped_column(String(64))
    bar_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # simulated clock (bar available_ts)
    targets: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    changed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaperOrder(Base):
    __tablename__ = "paper_orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("paper_sessions.id"), index=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)  # idempotency key
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    instrument: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(4))  # buy, sell
    quantity: Mapped[float] = mapped_column(Float)  # units of the underlying (lots x multiplier)
    lots: Mapped[float] = mapped_column(Float)
    order_type: Mapped[str] = mapped_column(String(16))  # market, limit, stop, stop_limit
    limit_price: Mapped[float | None] = mapped_column(Float)
    stop_price: Mapped[float | None] = mapped_column(Float)
    tif_bars: Mapped[int | None] = mapped_column(Integer)  # None = good till cancelled
    delay_bars: Mapped[int] = mapped_column(Integer, default=0)
    purpose: Mapped[str] = mapped_column(String(16))  # rebalance, protective_stop, manual
    status: Mapped[str] = mapped_column(String(20), default="CREATED", index=True)
    triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    bars_active: Mapped[int] = mapped_column(Integer, default=0)
    created_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # simulated decision time
    reference_price: Mapped[float] = mapped_column(Float)  # price known at the decision (last close)
    filled_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    avg_fill_price: Mapped[float | None] = mapped_column(Float)
    last_fill_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_cost: Mapped[float] = mapped_column(Float, default=0.0)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaperFill(Base):
    __tablename__ = "paper_fills"
    __table_args__ = (UniqueConstraint("order_id", "seq", name="uq_paper_fill_seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("paper_sessions.id"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("paper_orders.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    strategy_id: Mapped[str] = mapped_column(String(64))
    instrument: Mapped[str] = mapped_column(String(64))
    bar_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    reference_price: Mapped[float] = mapped_column(Float)
    fx_rate: Mapped[float] = mapped_column(Float, default=1.0)  # quote currency -> account currency
    commission: Mapped[float] = mapped_column(Float, default=0.0)  # account currency
    spread_cost: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_cost: Mapped[float] = mapped_column(Float, default=0.0)
    rule: Mapped[str] = mapped_column(Text, default="")  # which execution rule produced the price
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaperPosition(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (UniqueConstraint("session_id", "strategy_id", "instrument", name="uq_paper_position"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("paper_sessions.id"), index=True)
    strategy_id: Mapped[str] = mapped_column(String(64))
    instrument: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    holding_costs: Mapped[float] = mapped_column(Float, default=0.0)
    last_price: Mapped[float | None] = mapped_column(Float)
    fx_rate: Mapped[float] = mapped_column(Float, default=1.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    updated_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaperLedger(Base):
    """Cash movements in the account currency: deposit, realized P&L, commission, financing, funding, rollover."""

    __tablename__ = "paper_ledger"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("paper_sessions.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(16))
    amount: Mapped[float] = mapped_column(Float)
    strategy_id: Mapped[str | None] = mapped_column(String(64))
    instrument: Mapped[str | None] = mapped_column(String(64))
    ref_id: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaperEquity(Base):
    __tablename__ = "paper_equity"
    __table_args__ = (UniqueConstraint("session_id", "bar_ts", name="uq_paper_equity"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("paper_sessions.id"), index=True)
    bar_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cash: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)
    fees: Mapped[float] = mapped_column(Float)
    holding_costs: Mapped[float] = mapped_column(Float)
    gross_exposure: Mapped[float] = mapped_column(Float)
    net_exposure: Mapped[float] = mapped_column(Float)
    margin_used: Mapped[float] = mapped_column(Float)
    drawdown: Mapped[float] = mapped_column(Float)


class PaperEvent(Base):
    """Audit trail and alerts of a session: data, signal, order, fill, risk, reconciliation, config, system."""

    __tablename__ = "paper_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("paper_sessions.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    sim_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    level: Mapped[str] = mapped_column(String(8), index=True)  # info, warn, error, critical
    category: Mapped[str] = mapped_column(String(16), index=True)
    code: Mapped[str] = mapped_column(String(48))
    message: Mapped[str] = mapped_column(Text)
    strategy_id: Mapped[str | None] = mapped_column(String(64))
    instrument: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
