"""Session configuration and pre-registered evaluation criteria."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from qal.research.costs import CostModel

# Fields a person may change while a session runs (each change is versioned and logged).
MUTABLE_FIELDS = ("cost_scenario", "order_type", "limit_offset_bps", "order_ttl_bars", "stop_loss_pct",
                  "stop_order_type", "rebalance_threshold", "delay_bars")


class StrategyIn(BaseModel):
    strategy_id: str = Field(description="H:<hypothesis id> (validated), P:<candidate id> (paper validation program) "
                                         "or DEMO:<strategy type> (unvalidated)")
    dataset_ids: list[str] = Field(default_factory=list, description="DEMO only; hypotheses use their contract")
    params: dict[str, Any] = Field(default_factory=dict, description="DEMO only")
    allocation: float = Field(0.3, gt=0, le=2, description="Fraction of session equity given to the strategy")


class EvaluationCriteria(BaseModel):
    """Fixed before the session starts and hashed. They decide only whether the simulation is consistent with
    the backtest on the same bars; no value of them makes a strategy 'ready to trade'."""

    min_bars: int = Field(60, ge=10)
    min_fills: int = Field(10, ge=1)
    max_return_gap: float = Field(0.02, gt=0, description="|paper - backtest| cumulative net return")
    min_return_correlation: float = Field(0.8, ge=-1, le=1, description="Correlation of per-bar returns")
    max_drawdown_ratio: float = Field(1.5, gt=0, description="Paper max drawdown / backtest max drawdown")
    min_fill_rate: float = Field(0.8, ge=0, le=1, description="Filled / ordered quantity of finished orders")
    max_cost_ratio: float = Field(2.0, gt=0, description="Paper costs (trading + holding) / backtest costs on the same bars")
    min_signal_agreement: float = Field(0.95, ge=0, le=1)


class SessionConfig(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    mode: Literal["replay", "new_data"] = "replay"
    strategies: list[StrategyIn] = Field(min_length=1)
    capital: float = Field(100_000.0, gt=0)
    account_currency: str = Field("USD", min_length=3, max_length=3)
    start: datetime | None = Field(None, description="Replay: first bar traded (earlier bars only feed signals)")
    end: datetime | None = None
    warmup_bars: int = Field(300, ge=20, description="Bars before start used only to compute signals")
    history_bars: int = Field(600, ge=50, description="Bars passed to the strategy at each decision")
    cost_scenario: Literal["base", "adverse", "severe"] = "base"
    cost_overrides: dict[str, CostModel] = Field(default_factory=dict, description="Per dataset id")
    delay_bars: int = Field(0, ge=0, le=20, description="Extra bars between the decision and the first possible fill")
    order_type: Literal["market", "limit"] = "market"
    limit_offset_bps: float = Field(5.0, ge=0, description="Limit orders: price better than the last close by this")
    order_ttl_bars: int = Field(1, ge=1, le=50, description="Bars a strategy order stays active before expiring")
    stop_loss_pct: float | None = Field(None, gt=0, lt=0.5, description="Protective stop distance from the entry")
    stop_order_type: Literal["stop", "stop_limit"] = "stop"
    stop_limit_offset_bps: float = Field(50.0, ge=0)
    rebalance_threshold: float = Field(0.02, ge=0, description="Skip trades smaller than this fraction of equity")
    allow_unvalidated_demo: bool = False
    max_gap_bars: int = Field(5, ge=0)
    max_delay_hours: float = Field(48.0, gt=0)
    max_staleness_hours: float = Field(96.0, gt=0, description="New data: older last bar raises an alert")
    policy_id: str | None = None
    criteria: EvaluationCriteria = Field(default_factory=EvaluationCriteria)
    created_by: str = Field(min_length=2, max_length=64)
    program_id: str | None = Field(None, description="Set by the paper validation program: its history cannot be "
                                                     "reset or deleted and its configuration cannot change")

    @model_validator(mode="after")
    def _check(self) -> SessionConfig:
        ids = [s.strategy_id for s in self.strategies]
        if len(set(ids)) != len(ids):
            raise ValueError("each strategy may appear once per session")
        if self.start and self.end and self.end <= self.start:
            raise ValueError("end must be after start")
        return self
