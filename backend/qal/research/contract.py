"""Structured, falsifiable hypothesis contract.

Everything that decides whether the hypothesis passes (metric, thresholds, costs, validation design,
explored parameter grid, holdout) is fixed here *before* any backtest runs. The stored contract is
hashed; it cannot be edited once an experiment exists (a new version must be created, and trials of
all versions are counted together for the Deflated Sharpe Ratio).
"""

from __future__ import annotations

import re
from datetime import datetime
from itertools import product
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from qal.research.costs import CostModel
from qal.research.strategies import STRATEGIES

VAGUE = re.compile(
    r"\b(parece que|seems? (to|like)|va a subir|va a bajar|will go up|will go down|to the moon|"
    r"siempre sube|always goes up|obvio|obviously|garantizad[oa]|guaranteed)\b",
    re.IGNORECASE,
)


class StrategySpec(BaseModel):
    type: str = Field(description=f"One of {list(STRATEGIES)}")
    params: dict[str, Any] = Field(default_factory=dict, description="Fixed parameters")
    param_grid: dict[str, list[Any]] = Field(default_factory=dict,
                                             description="Variants explored during selection (all are counted as trials)")

    @model_validator(mode="after")
    def _check(self) -> StrategySpec:
        if self.type not in STRATEGIES:
            raise ValueError(f"strategy.type must be one of {list(STRATEGIES)}")
        cls = STRATEGIES[self.type]
        unknown = (set(self.params) | set(self.param_grid)) - set(cls.defaults)
        if unknown:
            raise ValueError(f"unknown parameters for {self.type}: {sorted(unknown)}")
        for k, v in self.param_grid.items():
            if not v:
                raise ValueError(f"param_grid.{k} is empty")
        if self.n_variants() > 200:
            raise ValueError("param_grid explores more than 200 variants; narrow it down")
        cls(**self.params)  # validate fixed params
        return self

    def n_variants(self) -> int:
        n = 1
        for v in self.param_grid.values():
            n *= len(v)
        return n

    def variants(self) -> list[dict[str, Any]]:
        keys = list(self.param_grid)
        if not keys:
            return [dict(self.params)]
        return [{**self.params, **dict(zip(keys, combo, strict=True))} for combo in product(*self.param_grid.values())]


class SuccessCriteria(BaseModel):
    metric: Literal["sharpe_net", "mean_net", "profit_factor", "hit_rate"] = "sharpe_net"
    min_value: float = Field(0.5, description="Out-of-sample threshold for the primary metric")
    min_trades: int = Field(30, ge=5)
    max_drawdown: float = Field(0.35, gt=0, le=1, description="Max OOS drawdown as a positive fraction")
    cost_multiplier_survive: float = Field(2.0, ge=1, description="Net mean return must stay > 0 at this cost multiple")
    min_positive_window_frac: float = Field(0.5, ge=0, le=1)
    min_robust_param_frac: float = Field(0.5, ge=0, le=1, description="Share of grid variants with positive net mean")
    max_top5_trades_share: float = Field(0.6, gt=0, description="Max share of net PnL from the 5 best trades")
    dsr_min: float = Field(0.9, ge=0, le=1, description="Minimum Deflated Sharpe probability")
    max_pbo: float = Field(0.5, ge=0, le=1)
    placebo_max_pvalue: float = Field(0.1, gt=0, le=1)
    require_ci_lower_positive: bool = False
    max_capacity_breach_frac: float = Field(0.05, ge=0, le=1)


class ValidationDesign(BaseModel):
    method: Literal["walk_forward"] = "walk_forward"
    train_bars: int = Field(500, ge=50)
    test_bars: int = Field(125, ge=10)
    embargo_bars: int = Field(5, ge=0)
    holdout_start: datetime = Field(description="Data from this instant on is the isolated final test")
    selection_metric: Literal["sharpe_net", "mean_net"] = "sharpe_net"
    placebo_runs: int = Field(200, ge=20, le=2000)
    bootstrap_runs: int = Field(1000, ge=100, le=10000)
    seed: int = 12345


class HypothesisContract(BaseModel):
    title: str = Field(min_length=5, max_length=200)
    premise: str = Field(min_length=40, description="Economic or behavioural premise")
    mechanism: str = Field(min_length=40, description="Proposed economic mechanism")
    source_anomaly_ids: list[str] = Field(default_factory=list)
    dataset_ids: list[str] = Field(min_length=1, description="Universe: datasets the strategy trades")
    features: list[str] = Field(min_length=1)
    strategy: StrategySpec
    entry_rule: str = Field(min_length=10)
    exit_rule: str = Field(min_length=10)
    horizon_bars: int = Field(ge=1, description="Prediction horizon in bars")
    benchmark: Literal["buy_and_hold", "cash"] = "cash"
    costs: dict[str, CostModel] = Field(default_factory=dict,
                                        description="Cost assumptions per dataset id; asset-class defaults otherwise")
    risks: list[str] = Field(min_length=1)
    invalidation: list[str] = Field(min_length=1, description="Observations that would falsify the hypothesis")
    success: SuccessCriteria = Field(default_factory=SuccessCriteria)
    validation: ValidationDesign
    capital: float = Field(1_000_000.0, gt=0)

    @field_validator("premise", "mechanism", "entry_rule", "exit_rule")
    @classmethod
    def _not_vague(cls, v: str) -> str:
        if VAGUE.search(v):
            raise ValueError("vague or non-falsifiable wording; state a measurable condition instead")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> HypothesisContract:
        n_assets = STRATEGIES[self.strategy.type].n_assets
        if len(self.dataset_ids) != n_assets:
            raise ValueError(f"{self.strategy.type} needs exactly {n_assets} dataset(s)")
        # Purging: the embargo must cover the label horizon so train labels cannot overlap test bars.
        if self.validation.embargo_bars < self.horizon_bars:
            raise ValueError("validation.embargo_bars must be >= horizon_bars (purging of overlapping labels)")
        return self
