"""Typed messages exchanged by the research agents.

Agents never pass statistics they computed themselves: numbers travel only as ``Evidence`` that points to a
stored record (anomaly, event study, relationship test, dataset, experiment) and copies the values that
record holds, so every figure can be traced back and re-read.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Route = Literal["anomaly", "prior", "factor", "relationship"]  # prior: a documented effect, no lab anomaly
Severity = Literal["block", "major", "minor"]


class Evidence(BaseModel):
    kind: Literal["anomalies", "event_study", "relationship_test", "dataset", "experiment", "factor_spec",
                  "hypothesis"]
    ref: str = Field(description="Id of the stored record (or dataset id / factor name)")
    summary: str
    values: dict[str, Any] = Field(default_factory=dict, description="Copied from the stored record, not computed")


class Idea(BaseModel):
    """Alpha Researcher -> Quant Analyst."""

    key: str = Field(description="Stable identity: strategy, instruments and mechanism")
    route: Route
    family: str = Field(description="mean_reversion, shock_continuation, momentum, divergence, volatility, volume, "
                                    "factor")
    title: str
    dataset_ids: list[str]
    origin_observation: str
    premise: str
    mechanism: str
    strategy: dict[str, Any]
    horizon_bars: int
    expected_direction: str
    entry_rule: str
    exit_rule: str
    features: list[str]
    refutation: list[str] = Field(min_length=1, description="Evidence that would refute it")
    risks: list[str] = Field(min_length=1)
    source_anomaly_ids: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    priority: float = Field(0.0, description="Heuristic ordering for the experiment budget, not evidence")


class DataGap(BaseModel):
    """An idea that cannot be tested with the data in the lab, and what would make it testable."""

    key: str
    route: Route
    family: str
    reason: str
    needs: list[str]


class Formalization(BaseModel):
    """Quant Analyst -> Adversarial Reviewer: the contract plus what the test assumes."""

    key: str
    contract: dict[str, Any]
    variables: list[str]
    signal_rule: str
    assumptions: list[str]
    data_requirements: list[str]


class Objection(BaseModel):
    """Adversarial Reviewer -> Research Coordinator."""

    code: str
    severity: Severity
    message: str
    evidence: list[Evidence] = Field(default_factory=list)


class Decision(BaseModel):
    """Research Coordinator's record of what happened to an idea, with the reason."""

    key: str
    action: Literal["registered", "duplicate", "rejected_by_review", "not_testable", "deferred_budget",
                    "experiment_run", "experiment_skipped", "experiment_failed"]
    reason: str
    hypothesis_id: str | None = None
    experiment_id: str | None = None


class Budget(BaseModel):
    """Limits for one research cycle. No language model is called, so the token budget is zero."""

    max_ideas: int = Field(40, ge=1, le=200)
    max_hypotheses: int = Field(12, ge=0, le=50)
    max_experiments: int = Field(6, ge=0, le=30)
    max_variants: int = Field(120, ge=1, le=1000, description="Total parameter variants backtested in the cycle")
    max_seconds: float = Field(900, gt=0, le=7200)
    llm_tokens: int = Field(0, ge=0, le=0, description="Agents are deterministic; no LLM tokens are spent")
    include_synthetic: bool = Field(False, description="Synthetic datasets are excluded unless asked for")
