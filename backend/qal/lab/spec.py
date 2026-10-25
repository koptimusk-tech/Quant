"""Versioned experiment contract of the Experiment Lab.

An experiment = a frozen hypothesis contract (``qal.research.contract``: mechanism, universe, rules, costs,
validation design, parameter grid, success thresholds) plus what the lab adds: the strategy family, the
baselines it must beat, an optional ablation (the strategy without the feature under test), the robustness
checks and the resource limits. Everything is hashed: the hash is the experiment's identity, so the same spec
never runs twice by accident and any change (a parameter, a cost, the code or data version) is a new experiment
next to the old one, never an overwrite.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from qal.research.contract import HypothesisContract
from qal.research.strategies import STRATEGIES

SPEC_VERSION = "lab-exp@1"

FAMILIES: dict[str, dict[str, Any]] = {
    "momentum": {"strategies": ["ts_momentum"], "label": "Momentum and trend following"},
    "mean_reversion": {"strategies": ["mean_reversion"], "label": "Mean reversion"},
    "factor": {"strategies": ["cross_sectional_factor"], "label": "Price factors (momentum, low volatility)"},
    "anomaly": {"strategies": ["event"], "label": "Statistical anomalies (trade after detector events)"},
    "pairs": {"strategies": ["pair_mean_reversion"], "label": "Pairs and cointegration"},
    "regime": {"strategies": ["ts_momentum"], "label": "Regime-conditional rules (volatility regime filter)"},
}

BASELINES = ("no_trade", "buy_and_hold", "permuted_signal", "simple_reference", "ablation")
# Baselines that make sense per family (documented choice): buy and hold is a fair comparison only for strategies
# that take directional risk on one instrument; market-neutral spreads and long/short factors are compared with
# cash, a permuted signal and their own unoptimised default.
DEFAULT_BASELINES: dict[str, list[str]] = {
    "momentum": ["no_trade", "buy_and_hold", "permuted_signal", "simple_reference"],
    "mean_reversion": ["no_trade", "buy_and_hold", "permuted_signal", "simple_reference"],
    "anomaly": ["no_trade", "buy_and_hold", "permuted_signal", "simple_reference"],
    "regime": ["no_trade", "buy_and_hold", "permuted_signal", "simple_reference", "ablation"],
    "factor": ["no_trade", "permuted_signal", "simple_reference"],
    "pairs": ["no_trade", "permuted_signal", "simple_reference"],
}
NOT_VALID_BASELINES = {"factor": {"buy_and_hold": "a dollar-neutral long/short book does not take market risk"},
                       "pairs": {"buy_and_hold": "a hedged spread does not take market risk"}}


class Robustness(BaseModel):
    train_factors: list[float] = Field(default_factory=lambda: [0.67, 1.5],
                                       description="Walk-forward re-run with shorter / longer training windows")
    delays: list[int] = Field(default_factory=lambda: [1, 2], description="Extra bars between decision and execution")
    cost_multipliers: list[float] = Field(default_factory=lambda: [0.0, 0.5, 1.0, 2.0, 3.0])
    leave_one_out: bool = Field(True, description="Universe subsets: drop one instrument at a time (3+ instruments)")

    @model_validator(mode="after")
    def _check(self) -> Robustness:
        if any(not 0.25 <= f <= 4 for f in self.train_factors) or len(self.train_factors) > 4:
            raise ValueError("train_factors: up to 4 values in [0.25, 4]")
        if any(d < 1 or d > 20 for d in self.delays) or len(self.delays) > 4:
            raise ValueError("delays: up to 4 values between 1 and 20 bars")
        if 1.0 not in self.cost_multipliers or any(m < 0 or m > 10 for m in self.cost_multipliers):
            raise ValueError("cost_multipliers must include 1.0 and stay within [0, 10]")
        return self


class Limits(BaseModel):
    max_seconds: float = Field(900.0, ge=10, le=6 * 3600)
    max_variants: int = Field(60, ge=1, le=200)
    max_bars: int = Field(200_000, ge=100)


class ExperimentSpec(BaseModel):
    spec_version: Literal["lab-exp@1"] = SPEC_VERSION
    family: Literal["momentum", "mean_reversion", "factor", "anomaly", "pairs", "regime"]
    contract: HypothesisContract
    baselines: list[str] | None = Field(None, description="Default per family when omitted")
    ablation: dict[str, Any] | None = Field(None, description="Parameters that switch off the feature under test")
    robustness: Robustness = Field(default_factory=Robustness)
    limits: Limits = Field(default_factory=Limits)
    evaluate_holdout: bool = Field(False, description="Evaluate the isolated final period once, after selection")
    generated_by: str = Field("manual", max_length=64, description="manual, generator:<family> or an agent name")

    @model_validator(mode="after")
    def _check(self) -> ExperimentSpec:
        st = self.contract.strategy.type
        if st not in FAMILIES[self.family]["strategies"]:
            raise ValueError(f"family {self.family} uses {FAMILIES[self.family]['strategies']}, not {st}")
        if self.family == "regime":
            regimes = {self.contract.strategy.params.get("regime", "all"),
                       *self.contract.strategy.param_grid.get("regime", [])}
            if regimes <= {"all"}:
                raise ValueError("a regime experiment must condition on a regime (regime high_vol or low_vol)")
        if self.contract.strategy.n_variants() > self.limits.max_variants:
            raise ValueError(f"{self.contract.strategy.n_variants()} variants above the limit "
                             f"{self.limits.max_variants}")
        bl = self.resolved_baselines()
        unknown = [b for b in bl if b not in BASELINES]
        if unknown:
            raise ValueError(f"unknown baselines {unknown}; use {list(BASELINES)}")
        bad = {b: why for b, why in NOT_VALID_BASELINES.get(self.family, {}).items() if b in bl}
        if bad:
            raise ValueError(f"baseline not valid for {self.family}: {bad}")
        if "ablation" in bl:
            if not self.ablation:
                raise ValueError("the ablation baseline needs `ablation` parameters (the feature switched off)")
            STRATEGIES[st](**{**self.contract.strategy.params, **self.ablation})
        return self

    def resolved_baselines(self) -> list[str]:
        if self.baselines is not None:
            return list(self.baselines)
        out = list(DEFAULT_BASELINES[self.family])
        if "ablation" in out and not self.ablation:
            out.remove("ablation")
        return out


__all__ = ["BASELINES", "DEFAULT_BASELINES", "FAMILIES", "SPEC_VERSION", "ExperimentSpec", "Limits", "Robustness"]
