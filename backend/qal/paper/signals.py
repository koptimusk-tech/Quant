"""Strategy signal engine: target weights decided at the close of a bar, from data available at that moment.

Signals are kept apart from execution: this module returns target weights per instrument and never creates
orders. A validated hypothesis trades with the parameters selected in its last validation window (the same
ones the promotion step records); nothing here is fitted on the paper period.

``DEMO:<strategy>`` runs a rule from the strategy library with explicit parameters. Demo strategies are not
validated; a session may include them only when ``allow_unvalidated_demo`` is set, and every output says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.models import Experiment, Hypothesis
from qal.portfolio.strategies import ELIGIBLE_STATUSES
from qal.research.backtest import align
from qal.research.contract import HypothesisContract
from qal.research.costs import CostModel, default_cost_model
from qal.research.strategies import STRATEGIES, Strategy

DEMO_NOTE = "UNVALIDATED DEMO: exercises the simulator only; not a validated strategy and not evidence of an edge."


@dataclass
class StrategyPlan:
    id: str
    kind: str  # hypothesis, demo
    type: str
    params: dict[str, Any]
    dataset_ids: list[str]
    allocation: float
    status: str  # hypothesis status at session creation, or DEMO
    hypothesis_id: str | None = None
    costs: dict[str, dict[str, Any]] = field(default_factory=dict)  # dataset id -> CostModel dump
    validation: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def strategy(self) -> Strategy:
        return STRATEGIES[self.type](**self.params)


def _last_validation(session: Session, hyp_id: str) -> Experiment | None:
    return session.scalar(select(Experiment).where(Experiment.hypothesis_id == hyp_id, Experiment.kind == "validation",
                                                   Experiment.status == "succeeded")
                          .order_by(Experiment.created_at.desc()).limit(1))


def resolve(session: Session, strategy_id: str, *, dataset_ids: list[str] | None, params: dict[str, Any] | None,
            allocation: float, allow_demo: bool) -> tuple[StrategyPlan | None, list[str]]:
    """Build the plan of one strategy, or the reasons it cannot be paper traded."""
    if strategy_id.startswith("H:"):
        hyp = session.get(Hypothesis, strategy_id[2:])
        if hyp is None:
            return None, [f"{strategy_id}: hypothesis not found"]
        problems = []
        if hyp.status not in ELIGIBLE_STATUSES:
            problems.append(f"{strategy_id}: status {hyp.status} is not one of {list(ELIGIBLE_STATUSES)}")
        exp = _last_validation(session, hyp.id)
        if exp is None or "selected_params_last_window" not in (exp.results or {}):
            problems.append(f"{strategy_id}: no succeeded validation with selected parameters")
        if problems:
            return None, problems
        c = HypothesisContract.model_validate(hyp.contract)
        costs = {d: c.costs[d].model_dump() for d in c.dataset_ids if d in c.costs}
        metrics = ((exp.results or {}).get("oos") or {}).get("metrics") or {}
        return StrategyPlan(strategy_id, "hypothesis", c.strategy.type, dict(exp.results["selected_params_last_window"]),
                            list(c.dataset_ids), allocation, hyp.status, hyp.id, costs,
                            {"experiment_id": exp.id, "oos_sharpe_net": metrics.get("sharpe_net"),
                             "oos_max_drawdown": metrics.get("max_drawdown"), "oos_bars": metrics.get("bars"),
                             "oos_volatility_ann": metrics.get("volatility_ann")}), []
    if strategy_id.startswith("DEMO:"):
        typ = strategy_id[5:]
        if not allow_demo:
            return None, [f"{strategy_id}: unvalidated demo strategies need allow_unvalidated_demo=true"]
        if typ not in STRATEGIES:
            return None, [f"{strategy_id}: unknown strategy type; use one of {list(STRATEGIES)}"]
        if not dataset_ids:
            return None, [f"{strategy_id}: dataset_ids are required"]
        try:
            STRATEGIES[typ](**(params or {}))
        except ValueError as exc:
            return None, [f"{strategy_id}: {exc}"]
        n = STRATEGIES[typ].n_assets
        if (n and len(dataset_ids) != n) or (n == 0 and len(dataset_ids) < 3):
            return None, [f"{strategy_id}: needs {n or '3 or more'} dataset(s)"]
        return StrategyPlan(strategy_id, "demo", typ, dict(params or {}), list(dataset_ids), allocation, "DEMO",
                            notes=[DEMO_NOTE]), []
    return None, [f"{strategy_id}: use H:<hypothesis id> or DEMO:<strategy type>"]


def cost_model(plan: StrategyPlan, dataset_id: str, asset_class: str) -> CostModel:
    c = plan.costs.get(dataset_id)
    return CostModel(**c) if c else default_cost_model(asset_class)


def decide(plan: StrategyPlan, frames: list[pd.DataFrame], history_bars: int) -> dict[int, float] | None:
    """Target weight per leg from bars available now (the caller passes only those). None if a leg has no data."""
    if any(f.empty for f in frames):
        return None
    al = align([f.tail(history_bars * 2) for f in frames])
    al = [f.tail(history_bars).reset_index(drop=True) for f in al]
    if len(al[0]) == 0:
        return None
    w = plan.strategy().weights(al)
    row = w.iloc[-1].fillna(0.0).to_numpy(dtype=float)
    gross = abs(row).sum()
    if gross > 1:
        row = row / gross
    return {j: float(row[j]) for j in range(len(frames))}
