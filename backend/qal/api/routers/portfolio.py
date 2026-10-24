"""Portfolio & Risk: strategy eligibility, risk policies, portfolio runs, risk decisions and agent proposals."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.api.schemas import JobOut
from qal.db import get_session
from qal.jobs.runner import submit_job
from qal.models import AgentMessage, Experiment, Instrument, RiskDecision, RiskPolicy
from qal.portfolio import construction, sizing
from qal.portfolio.agent import ProposalRequest, propose
from qal.portfolio.risk_manager import (
    PolicyError,
    PolicyIn,
    create_policy,
    default_policy,
    policy_label,
)
from qal.portfolio.service import PortfolioRunIn, RunError, run_portfolio
from qal.portfolio.strategies import EligibilityCriteria, catalog

router = APIRouter(prefix="/portfolio", tags=["portfolio & risk"])


class RunIn(PortfolioRunIn):
    run_async: bool = True


class ProposalIn(ProposalRequest):
    run_async: bool = True


class SizingIn(BaseModel):
    symbol: str
    asset_class: str = "fx"
    capital: float = Field(1_000_000.0, gt=0)
    price: float = Field(gt=0)
    method: str = "vol_target"
    fraction: float = 0.1
    trailing_vol: float | None = Field(None, gt=0, description="Annualised, measured before the decision")
    target_vol: float = 0.1
    stop_distance: float | None = Field(None, gt=0, lt=1)


def _policy_out(p: RiskPolicy) -> dict[str, Any]:
    return {"id": p.id, "name": p.name, "version": p.version, "label": policy_label(p), "limits": p.limits,
            "policy_hash": p.policy_hash, "created_by": p.created_by, "active": p.active, "note": p.note,
            "created_at": p.created_at}


@router.get("/methods")
def methods() -> list[dict[str, str]]:
    return [{"name": m, "description": construction.describe(m)} for m in construction.METHODS]


@router.post("/strategies")
def strategies(criteria: EligibilityCriteria | None = None, include_synthetic: bool = False,
               s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Every validated hypothesis as a strategy (eligible or rejected, with reasons), plus synthetic ones on request."""
    crit = criteria or EligibilityCriteria()
    return [r.as_dict() for r in catalog(s, crit, include_synthetic=include_synthetic)]


@router.get("/policies")
def policies(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    default_policy(s)
    return [_policy_out(p) for p in s.scalars(select(RiskPolicy).order_by(RiskPolicy.created_at.desc()))]


@router.get("/policies/active")
def active_policy(s: Session = Depends(get_session)) -> dict[str, Any]:
    return _policy_out(default_policy(s))


@router.post("/policies", status_code=201)
def new_policy(body: PolicyIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Create a new policy version (people only). Agents get 403 and the attempt is audited."""
    try:
        return _policy_out(create_policy(s, body))
    except PolicyError as exc:
        from qal import audit

        audit.record(s, body.approved_by, "risk_policy_change_refused", "risk_policy", None,
                     actor_kind=body.actor_kind, reason=str(exc))
        s.commit()
        raise HTTPException(403, str(exc)) from exc


def _summary(e: Experiment) -> dict[str, Any]:
    r = e.results or {}
    prim = (r.get("primary") or {}).get("metrics") or {}
    return {"id": e.id, "kind": e.kind, "status": e.status, "created_at": e.created_at, "finished_at": e.finished_at,
            "label": e.config.get("label", ""), "strategies": e.config.get("strategy_ids", []),
            "methods": e.config.get("methods", []), "policy_version": e.config.get("policy_version"),
            "is_synthetic": r.get("is_synthetic"), "sharpe_net": prim.get("sharpe_net"),
            "max_drawdown": prim.get("max_drawdown"), "duration_s": r.get("duration_s"), "error": e.error}


@router.post("/runs", status_code=202)
def create_run(body: RunIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    payload = body.model_dump(mode="json", exclude={"run_async"})
    if body.run_async:
        return {"job": JobOut.model_validate(submit_job(s, "portfolio_run", payload)).model_dump(mode="json")}
    try:
        exp = run_portfolio(s, PortfolioRunIn.model_validate(payload))
    except RunError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"experiment": _summary(exp)}


@router.get("/runs")
def list_runs(limit: int = Query(50, le=500), s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    q = select(Experiment).where(Experiment.kind == "portfolio_backtest").order_by(Experiment.created_at.desc()).limit(limit)
    return [_summary(e) for e in s.scalars(q)]


def _get(s: Session, exp_id: str, kind: str) -> Experiment:
    e = s.get(Experiment, exp_id)
    if e is None or e.kind != kind:
        raise HTTPException(404, "not found")
    return e


@router.get("/runs/{exp_id}")
def get_run(exp_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    e = _get(s, exp_id, "portfolio_backtest")
    return {**_summary(e), "config": e.config, "data_refs": e.data_refs, "code_version": e.code_version,
            "algorithm_version": e.algorithm_version, "results": e.results}


@router.get("/runs/{exp_id}/decisions")
def decisions(exp_id: str, decision: str | None = None, rule: str | None = None, limit: int = Query(500, le=20000),
              s: Session = Depends(get_session)) -> dict[str, Any]:
    _get(s, exp_id, "portfolio_backtest")
    q = select(RiskDecision).where(RiskDecision.run_id == exp_id)
    if decision:
        q = q.where(RiskDecision.decision == decision)
    if rule:
        q = q.where(RiskDecision.rule == rule)
    rows = s.scalars(q.order_by(RiskDecision.as_of.desc(), RiskDecision.level).limit(limit)).all()
    return {"items": [{"as_of": d.as_of, "level": d.level, "scope": d.scope, "rule": d.rule, "observed": d.observed,
                       "limit": d.limit_value, "decision": d.decision, "action": d.action, "reason": d.reason,
                       "policy_version": d.policy_version, "created_at": d.created_at} for d in rows]}


@router.post("/proposals", status_code=202)
def create_proposals(body: ProposalIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    """The portfolio agent proposes combinations; each is computed and checked by the risk manager."""
    req = ProposalRequest.model_validate(body.model_dump(exclude={"run_async"}))
    if req.limits is not None or req.policy_id is not None:
        try:
            propose(s, req)
        except PolicyError as exc:
            s.commit()
            raise HTTPException(403, str(exc)) from exc
    if body.run_async:
        return {"job": JobOut.model_validate(submit_job(s, "portfolio_proposals", req.model_dump(mode="json")))
                .model_dump(mode="json")}
    return {"experiment": _summary(propose(s, req))}


@router.get("/proposals")
def list_proposals(limit: int = Query(20, le=200), s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    q = select(Experiment).where(Experiment.kind == "portfolio_proposals").order_by(Experiment.created_at.desc()).limit(limit)
    return [{**_summary(e), "results": e.results} for e in s.scalars(q)]


@router.get("/proposals/{exp_id}")
def get_proposals(exp_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    e = _get(s, exp_id, "portfolio_proposals")
    msgs = s.scalars(select(AgentMessage).where(AgentMessage.run_id == exp_id).order_by(AgentMessage.seq)).all()
    return {**_summary(e), "results": e.results,
            "messages": [{"seq": m.seq, "sender": m.sender, "receiver": m.receiver, "kind": m.kind,
                          "subject": m.subject_key, "payload": m.payload, "evidence": m.evidence} for m in msgs]}


@router.post("/sizing")
def size_position(body: SizingIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Size one position and round it to the instrument's contract (lots, tick, leverage)."""
    inst = s.scalar(select(Instrument).where(Instrument.symbol == body.symbol).limit(1))
    spec = sizing.contract_for(inst.metadata_json if inst else None, body.asset_class, body.symbol)
    try:
        t = sizing.target_exposure(body.method, capital=body.capital, fraction=body.fraction, vol=body.trailing_vol,
                                   target_vol=body.target_vol)
        q = sizing.to_quantity(t["exposure"], capital=body.capital, price=body.price, spec=spec)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    out = {**t, **q}
    if body.stop_distance:
        out["stop_loss"] = {"loss_at_stop": abs(q["exposure"]) * body.stop_distance * body.capital,
                            "note": "A stop-loss does not guarantee a maximum loss: gaps and slippage can exceed it."}
    return out
