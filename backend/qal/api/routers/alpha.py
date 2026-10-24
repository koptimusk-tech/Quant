"""Alpha Research: agent research cycles, the hypotheses they produced and the evidence behind each decision."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.alpha.factors import factor_status
from qal.alpha.messages import Budget
from qal.alpha.report import (
    hypotheses_table,
    hypothesis_detail,
    run_detail,
    run_summary,
)
from qal.api.schemas import JobOut
from qal.db import get_session
from qal.jobs.runner import submit_job
from qal.models import Experiment, Hypothesis
from qal.research.costs import CostModel
from qal.research.promotion import PaperApproval, eligibility, promote_paper_eligible
from qal.research.states import TransitionError

router = APIRouter(prefix="/alpha", tags=["alpha"])


class CycleIn(BaseModel):
    budget: Budget = Field(default_factory=Budget)
    costs: dict[str, CostModel] = Field(default_factory=dict, description="Real account costs by symbol")
    run_experiments: bool = True


@router.get("/factors")
def factors(include_synthetic: bool = False, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Factor modules with their definition and whether the lab can test them (and on which universe)."""
    return factor_status(s, include_synthetic)


@router.post("/runs", status_code=202)
def start_run(body: CycleIn | None = None, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Queue one bounded research cycle. Agents propose and review; the validation engine computes results."""
    body = body or CycleIn()
    job = submit_job(s, "alpha_cycle", {"budget": body.budget.model_dump(),
                                        "costs": {k: v.model_dump() for k, v in body.costs.items()},
                                        "run_experiments": body.run_experiments})
    return {"job": JobOut.model_validate(job).model_dump(mode="json")}


@router.get("/runs")
def list_runs(limit: int = Query(20, le=100), s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = s.scalars(select(Experiment).where(Experiment.kind == "alpha_research_run")
                     .order_by(Experiment.created_at.desc()).limit(limit)).all()
    return [run_summary(e) for e in rows]


@router.get("/runs/{run_id}")
def get_run(run_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    e = s.get(Experiment, run_id)
    if e is None or e.kind != "alpha_research_run":
        raise HTTPException(404, "research run not found")
    return run_detail(s, e)


@router.get("/relationships/{scan_id}")
def get_relationships(scan_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    e = s.get(Experiment, scan_id)
    if e is None or e.kind != "relationship_scan":
        raise HTTPException(404, "relationship scan not found")
    return {"id": e.id, "created_at": e.created_at, "config": e.config, **(e.results or {})}


@router.get("/hypotheses")
def list_hypotheses(limit: int = Query(200, le=500), s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return hypotheses_table(s, limit)


def _hyp(s: Session, hid: str) -> Hypothesis:
    h = s.get(Hypothesis, hid)
    if h is None:
        raise HTTPException(404, "hypothesis not found")
    return h


@router.get("/hypotheses/{hid}")
def get_hypothesis(hid: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    return hypothesis_detail(s, _hyp(s, hid))


@router.post("/hypotheses/{hid}/experiments", status_code=202)
def launch_experiment(hid: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Queue a validation run of the frozen contract (every run adds its variants to the family's trials)."""
    h = _hyp(s, hid)
    if h.status not in ("HYPOTHESIS_FORMULATED", "VALIDATION_FAILED", "VALIDATION_PASSED"):
        raise HTTPException(409, f"cannot start an experiment from {h.status}")
    job = submit_job(s, "validate", {"hypothesis_id": hid})
    return {"job": JobOut.model_validate(job).model_dump(mode="json")}


@router.get("/hypotheses/{hid}/paper-eligibility")
def get_eligibility(hid: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    return eligibility(s, _hyp(s, hid))


@router.post("/hypotheses/{hid}/paper-eligibility")
def approve_paper(hid: str, body: PaperApproval, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Mark as PAPER_TRADING_ELIGIBLE after explicit human approval. No orders are sent; live stays disabled."""
    try:
        return promote_paper_eligible(s, _hyp(s, hid), body)
    except TransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
