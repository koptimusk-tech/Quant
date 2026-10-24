"""Promotion of a validated hypothesis to PAPER_TRADING_ELIGIBLE.

Eligibility only marks a candidate for paper trading; nothing trades and no account is touched. It needs,
all at once:

* VALIDATION_PASSED (walk-forward + gates A-D, decided by the policy engine);
* exactly one access to the isolated holdout, meeting the pre-registered primary metric and drawdown
  limit (a holdout looked at more than once is no longer independent and cannot promote);
* explicit cost assumptions in the contract for every dataset (asset-class defaults are estimates);
* risk limits stated by the approver;
* an explicit approval by a person. Agents and the API without a named approver are refused.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.models import Experiment, GateDecision, Hypothesis
from qal.research.contract import HypothesisContract
from qal.research.service import transition
from qal.research.states import TransitionError


class RiskLimits(BaseModel):
    max_gross_exposure: float = Field(gt=0, le=1, description="Fraction of paper capital")
    max_drawdown_stop: float = Field(gt=0, le=0.5, description="Stop the paper strategy beyond this drawdown")
    max_daily_loss: float = Field(gt=0, le=0.2)


class PaperApproval(BaseModel):
    approved_by: str = Field(min_length=2, max_length=64)
    actor_kind: str = Field("human", description="Only 'human' can approve")
    risk_limits: RiskLimits
    confirm: bool = Field(description="Must be true: the approver read the validation and holdout reports")
    note: str = ""


def eligibility(session: Session, hyp: Hypothesis) -> dict[str, Any]:
    """Every requirement with its evidence; ``eligible`` only when all of them hold."""
    c = HypothesisContract.model_validate(hyp.contract)
    finals = session.scalars(select(Experiment).where(Experiment.hypothesis_id == hyp.id,
                                                      Experiment.kind == "final_test")
                             .order_by(Experiment.created_at)).all()
    gates = session.scalars(select(GateDecision).where(GateDecision.hypothesis_id == hyp.id)
                            .order_by(GateDecision.created_at.desc())).all()
    req: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        req.append({"requirement": name, "met": bool(ok), "detail": detail})

    add("validation_passed", hyp.status == "VALIDATION_PASSED", f"status {hyp.status}")
    add("gates_passed", bool(gates) and all(g.decision == "pass" for g in gates[:4]),
        "latest gates A-D: " + ", ".join(f"{g.gate}={g.decision}" for g in gates[:4]) if gates else "no gate decisions")
    if not finals:
        add("holdout_tested_once", False, "the isolated holdout has not been evaluated")
    else:
        first = finals[0].results or {}
        m = first.get("metrics", {})
        add("holdout_tested_once", len(finals) == 1 and hyp.final_test_accesses == 1,
            f"{hyp.final_test_accesses} access(es) to the holdout")
        primary = m.get(c.success.metric)
        add("holdout_primary_metric", primary is not None and primary >= c.success.min_value,
            f"holdout {c.success.metric} {primary} vs pre-registered minimum {c.success.min_value}")
        dd = m.get("max_drawdown")
        add("holdout_drawdown", dd is not None and abs(dd) <= c.success.max_drawdown,
            f"holdout max drawdown {dd} vs limit {c.success.max_drawdown}")
    add("explicit_costs", all(ds in c.costs for ds in c.dataset_ids),
        "cost assumptions set for every dataset" if all(ds in c.costs for ds in c.dataset_ids)
        else "contract uses asset-class default costs (estimates): set explicit costs in a new version")
    return {"eligible": all(r["met"] for r in req), "requirements": req}


def promote_paper_eligible(session: Session, hyp: Hypothesis, approval: PaperApproval) -> dict[str, Any]:
    if approval.actor_kind != "human":
        raise TransitionError("only a person can approve paper-trading eligibility; agents cannot")
    if not approval.confirm:
        raise TransitionError("approval must be confirmed explicitly")
    status = eligibility(session, hyp)
    if not status["eligible"]:
        missing = [r["requirement"] for r in status["requirements"] if not r["met"]]
        raise TransitionError("not eligible: " + ", ".join(missing))
    transition(session, hyp, "PAPER_TRADING_ELIGIBLE", actor="policy_engine", actor_kind="policy",
               reason=f"approved by {approval.approved_by}; risk limits {approval.risk_limits.model_dump()}")
    return {**status, "approved_by": approval.approved_by, "risk_limits": approval.risk_limits.model_dump(),
            "note": "Eligible for paper trading only. No orders are sent and live execution stays disabled."}
