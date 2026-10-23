from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.api.schemas import ExperimentOut, JobOut
from qal.db import get_session
from qal.jobs.runner import submit_job
from qal.models import Experiment, GateDecision, Hypothesis
from qal.research.contract import HypothesisContract
from qal.research.costs import DEFAULT_COSTS
from qal.research.service import create_hypothesis, run_final_test, run_validation, transition
from qal.research.states import STATES, TransitionError
from qal.research.strategies import STRATEGIES

router = APIRouter(tags=["research"])


class HypothesisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    family_id: str
    version: int
    parent_id: str | None
    title: str
    status: str
    contract: dict[str, Any]
    contract_hash: str
    created_by: str
    final_test_accesses: int
    created_at: datetime
    updated_at: datetime


class GateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    experiment_id: str | None
    gate: str
    decision: str
    reasons: list[str]
    evidence: dict[str, Any]
    policy_version: str
    created_at: datetime


class CreateIn(BaseModel):
    contract: dict[str, Any]
    parent_id: str | None = None


class TransitionIn(BaseModel):
    target: str
    reason: str


class RunIn(BaseModel):
    run_async: bool = False


@router.get("/strategies")
def list_strategies() -> list[dict[str, Any]]:
    return [{"name": s.name, "description": s.description, "n_assets": s.n_assets, "defaults": s.defaults}
            for s in STRATEGIES.values()]


@router.get("/research/meta")
def research_meta() -> dict[str, Any]:
    """Contract JSON schema, default cost assumptions per asset class and the research states."""
    return {"contract_schema": HypothesisContract.model_json_schema(), "states": list(STATES),
            "default_costs": {k: v.model_dump() for k, v in DEFAULT_COSTS.items()}}


@router.post("/hypotheses", response_model=HypothesisOut, status_code=201)
def create(body: CreateIn, s: Session = Depends(get_session)):
    """Register a hypothesis. The contract is validated and frozen (hashed)."""
    try:
        contract = HypothesisContract.model_validate(body.contract)
        return create_hypothesis(s, contract, parent_id=body.parent_id)
    except ValidationError as exc:
        raise HTTPException(422, detail=[{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/hypotheses", response_model=list[HypothesisOut])
def list_h(status: str | None = None, limit: int = Query(100, le=500), s: Session = Depends(get_session)):
    q = select(Hypothesis).order_by(Hypothesis.created_at.desc()).limit(limit)
    if status:
        q = q.where(Hypothesis.status == status)
    return s.scalars(q).all()


def _get(s: Session, hid: str) -> Hypothesis:
    h = s.get(Hypothesis, hid)
    if h is None:
        raise HTTPException(404, "hypothesis not found")
    return h


@router.get("/hypotheses/{hid}")
def get_h(hid: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    h = _get(s, hid)
    exps = s.scalars(select(Experiment).where(Experiment.hypothesis_id == hid).order_by(Experiment.created_at.desc())).all()
    gates = s.scalars(select(GateDecision).where(GateDecision.hypothesis_id == hid).order_by(GateDecision.created_at.desc())).all()
    return {
        "hypothesis": HypothesisOut.model_validate(h).model_dump(mode="json"),
        "experiments": [{"id": e.id, "kind": e.kind, "status": e.status, "created_at": e.created_at, "error": e.error,
                         "decision": (e.results or {}).get("decision"),
                         "oos_sharpe_net": ((e.results or {}).get("oos") or {}).get("metrics", {}).get("sharpe_net"),
                         "contaminated": (e.results or {}).get("contaminated")} for e in exps],
        "gates": [GateOut.model_validate(g).model_dump(mode="json") for g in gates],
        "versions": [HypothesisOut.model_validate(v).model_dump(mode="json") for v in
                     s.scalars(select(Hypothesis).where(Hypothesis.family_id == h.family_id).order_by(Hypothesis.version)).all()],
    }


@router.patch("/hypotheses/{hid}")
def edit_h(hid: str) -> None:
    raise HTTPException(409, "Contracts are frozen. Create a new version with parent_id; its trials are counted "
                             "together with this one.")


@router.post("/hypotheses/{hid}/transition", response_model=HypothesisOut)
def manual_transition(hid: str, body: TransitionIn, s: Session = Depends(get_session)):
    """Manual transitions (reject, suspend, resume). Promotions are reserved for the policy engine."""
    h = _get(s, hid)
    try:
        transition(s, h, body.target, actor="api", actor_kind="user", reason=body.reason)
    except TransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return h


@router.post("/hypotheses/{hid}/validate")
def validate(hid: str, body: RunIn | None = None, s: Session = Depends(get_session)) -> dict[str, Any]:
    h = _get(s, hid)
    if body and body.run_async:
        job = submit_job(s, "validate", {"hypothesis_id": hid})
        return {"job": JobOut.model_validate(job).model_dump(mode="json")}
    try:
        exp = run_validation(s, h)
    except TransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"experiment": ExperimentOut.model_validate(exp).model_dump(mode="json"), "status": h.status}


@router.post("/hypotheses/{hid}/final-test")
def final_test(hid: str, confirm: bool = Query(False, description="Must be true: each access is recorded"),
               s: Session = Depends(get_session)) -> dict[str, Any]:
    if not confirm:
        raise HTTPException(400, "Pass confirm=true. The holdout should be examined once; repeated access is recorded "
                                 "and marks it as contaminated.")
    h = _get(s, hid)
    try:
        exp = run_final_test(s, h)
    except TransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"experiment": ExperimentOut.model_validate(exp).model_dump(mode="json")}
