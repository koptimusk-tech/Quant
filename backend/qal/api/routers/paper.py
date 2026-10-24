"""Paper Trading (simulation only): sessions, orders, positions, P&L, risk blocks, events, evaluation, export."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.api.schemas import JobOut
from qal.db import get_session
from qal.jobs.runner import submit_job
from qal.models import (
    PaperEquity,
    PaperEvent,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PaperSession,
    PaperSignal,
    RiskDecision,
)
from qal.paper import engine, evaluation, service
from qal.paper.config import SessionConfig
from qal.paper.orders import OrderIn
from qal.paper.reconcile import reconcile

router = APIRouter(prefix="/paper", tags=["paper trading (simulation)"])


class Actor(BaseModel):
    by: str = Field(min_length=2, max_length=64, description="Person taking the action")


class StepIn(Actor):
    bars: int = Field(1, ge=1, le=500)
    skip_bad_bar: bool = False


class RunIn(Actor):
    max_bars: int = Field(5000, ge=1, le=100_000)


class KillIn(Actor):
    active: bool = True
    confirm: bool = False
    reason: str = ""


class ConfirmIn(Actor):
    confirm: bool = False


class ConfigChangeIn(Actor):
    changes: dict[str, Any]
    reason: str = Field(min_length=3)


class ManualOrderIn(OrderIn):
    by: str = Field(min_length=2, max_length=64)


def _ps(s: Session, sid: str) -> PaperSession:
    try:
        return service.get(s, sid)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


def _guard(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except service.PaperError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


def _out(ps: PaperSession) -> dict[str, Any]:
    cfg = {k: v for k, v in ps.config.items() if k != "resolved"}
    return {"id": ps.id, "name": ps.name, "status": ps.status, "mode": ps.mode, "is_synthetic": ps.is_synthetic,
            "is_demo": ps.is_demo, "kill_switch": ps.kill_switch, "pause_reason": ps.pause_reason,
            "cursor_ts": ps.cursor_ts, "clock_ts": ps.clock_ts, "bars_processed": ps.bars_processed,
            "initial_capital": ps.initial_capital, "account_currency": ps.account_currency,
            "summary": {k: v for k, v in (ps.summary or {}).items() if k not in ("stopped_out", "event_seq")},
            "strategies": [p["id"] for p in ps.config["resolved"]["plans"]],
            "plans": ps.config["resolved"]["plans"], "notes": ps.config["resolved"].get("notes", []),
            "config": cfg, "config_version": ps.config_version, "config_hash": ps.config_hash,
            "config_history": ps.config_history, "criteria": ps.criteria, "criteria_hash": ps.criteria_hash,
            "code_version": ps.code_version, "data_refs": ps.data_refs, "policy_version": ps.policy_version,
            "experiment_id": ps.experiment_id, "verdict": (ps.evaluation or {}).get("verdict"),
            "created_by": ps.created_by, "created_at": ps.created_at, "updated_at": ps.updated_at,
            "labels": [service.SIM_NOTE] + ([service.REPLAY_NOTE] if ps.mode == "replay" else [])
            + (["SYNTHETIC DATA: not evidence about any market."] if ps.is_synthetic else [])
            + (["UNVALIDATED DEMO strategies included."] if ps.is_demo else [])}


@router.get("/strategies")
def strategies(s: Session = Depends(get_session)) -> dict[str, Any]:
    """Validated strategies (eligible or not, with reasons) and the unvalidated demo rules."""
    return service.eligible_strategies(s)


@router.post("/sessions", status_code=201)
def create(body: SessionConfig, s: Session = Depends(get_session)) -> dict[str, Any]:
    return _out(_guard(service.create_session, s, body))


@router.get("/sessions")
def list_sessions(limit: int = Query(50, le=500), s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    q = select(PaperSession).where(PaperSession.status != "DELETED").order_by(PaperSession.created_at.desc()).limit(limit)
    return [_out(p) for p in s.scalars(q)]


@router.get("/sessions/{sid}")
def get_one(sid: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    ps = _ps(s, sid)
    out = _out(ps)
    last = {}
    for sg in s.scalars(select(PaperSignal).where(PaperSignal.session_id == sid)
                        .order_by(PaperSignal.bar_ts.desc()).limit(50)).all():
        last.setdefault(sg.strategy_id, {"bar_ts": sg.bar_ts, "decided_at": sg.decided_at, "targets": sg.targets})
    out["last_signals"] = last
    out["alerts"] = s.scalar(select(PaperEvent.id).where(PaperEvent.session_id == sid,
                                                         PaperEvent.level.in_(("error", "critical"))).limit(1)) is not None
    return out


@router.post("/sessions/{sid}/start")
def start(sid: str, body: Actor, s: Session = Depends(get_session)) -> dict[str, Any]:
    return _out(_guard(service.start, s, _ps(s, sid), body.by))


@router.post("/sessions/{sid}/pause")
def pause(sid: str, body: Actor, s: Session = Depends(get_session)) -> dict[str, Any]:
    return _out(_guard(service.pause, s, _ps(s, sid), body.by))


@router.post("/sessions/{sid}/step")
def step(sid: str, body: StepIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    ps = _ps(s, sid)
    if body.skip_bad_bar:
        if ps.status != "PAUSED":
            raise HTTPException(400, "skip_bad_bar applies to a session paused by a data failure")
        service.start(s, ps, body.by)
    if ps.status != "RUNNING":
        raise HTTPException(400, f"session is {ps.status}: start it first")
    res = engine.run(s, ps, body.bars, skip_bad_bar=body.skip_bad_bar)
    return {"result": res, "session": _out(ps)}


@router.post("/sessions/{sid}/run", status_code=202)
def run(sid: str, body: RunIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    ps = _ps(s, sid)
    if ps.status != "RUNNING":
        raise HTTPException(400, f"session is {ps.status}: start it first")
    job = submit_job(s, "paper_run", {"session_id": sid, "max_bars": body.max_bars})
    return {"job": JobOut.model_validate(job).model_dump(mode="json")}


@router.post("/sessions/{sid}/kill-switch")
def kill(sid: str, body: KillIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    return _out(_guard(service.kill_switch, s, _ps(s, sid), active=body.active, by=body.by, confirm=body.confirm,
                       reason=body.reason))


@router.post("/sessions/{sid}/reset")
def reset(sid: str, body: ConfirmIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    return _out(_guard(service.reset, s, _ps(s, sid), body.by, body.confirm))


@router.post("/sessions/{sid}/delete")
def delete(sid: str, body: ConfirmIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    _guard(service.delete_session, s, _ps(s, sid), body.by, body.confirm)
    return {"deleted": sid}


@router.patch("/sessions/{sid}/config")
def change_config(sid: str, body: ConfigChangeIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    return _out(_guard(service.change_config, s, _ps(s, sid), body.changes, body.by, body.reason))


@router.post("/sessions/{sid}/orders", status_code=201)
def manual_order(sid: str, body: ManualOrderIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    o = _guard(service.manual_order, s, _ps(s, sid), OrderIn.model_validate(body.model_dump(exclude={"by"})), body.by)
    if o is None:
        raise HTTPException(409, "an identical order was already created at this bar")
    return _order(o)


@router.post("/sessions/{sid}/orders/{oid}/cancel")
def cancel_order(sid: str, oid: str, body: Actor, s: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        return _order(_guard(service.cancel_order, s, _ps(s, sid), oid, body.by))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _order(o: PaperOrder) -> dict[str, Any]:
    return {c: getattr(o, c) for c in ("id", "client_order_id", "strategy_id", "instrument", "side", "quantity", "lots",
                                       "order_type", "limit_price", "stop_price", "tif_bars", "delay_bars", "purpose",
                                       "status", "triggered", "bars_active", "created_ts", "reference_price",
                                       "filled_quantity", "avg_fill_price", "last_fill_ts", "commission",
                                       "slippage_cost", "reject_reason", "cancel_reason")} | {
        "risk_checks": (o.details or {}).get("risk_checks", [])}


def _filters(q, model, strategy: str | None, instrument: str | None, start: datetime | None, end: datetime | None, tcol):
    if strategy:
        q = q.where(model.strategy_id == strategy)
    if instrument:
        q = q.where(model.instrument == instrument)
    if start:
        q = q.where(tcol >= start)
    if end:
        q = q.where(tcol <= end)
    return q


@router.get("/sessions/{sid}/orders")
def list_orders(sid: str, status: str | None = None, strategy: str | None = None, instrument: str | None = None,
                start: datetime | None = None, end: datetime | None = None, limit: int = Query(500, le=20000),
                s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    _ps(s, sid)
    q = _filters(select(PaperOrder).where(PaperOrder.session_id == sid), PaperOrder, strategy, instrument, start, end,
                 PaperOrder.created_ts)
    if status:
        q = q.where(PaperOrder.status.in_(status.split(",")))
    return [_order(o) for o in s.scalars(q.order_by(PaperOrder.created_ts.desc()).limit(limit))]


@router.get("/sessions/{sid}/fills")
def list_fills(sid: str, strategy: str | None = None, instrument: str | None = None, start: datetime | None = None,
               end: datetime | None = None, limit: int = Query(500, le=20000),
               s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    _ps(s, sid)
    q = _filters(select(PaperFill).where(PaperFill.session_id == sid), PaperFill, strategy, instrument, start, end,
                 PaperFill.bar_ts)
    cols = ("id", "order_id", "seq", "strategy_id", "instrument", "bar_ts", "side", "quantity", "price",
            "reference_price", "fx_rate", "commission", "spread_cost", "slippage_cost", "rule")
    return [{c: getattr(f, c) for c in cols} for f in s.scalars(q.order_by(PaperFill.bar_ts.desc()).limit(limit))]


@router.get("/sessions/{sid}/positions")
def list_positions(sid: str, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    _ps(s, sid)
    cols = ("strategy_id", "instrument", "quantity", "avg_price", "last_price", "fx_rate", "realized_pnl",
            "unrealized_pnl", "fees", "holding_costs", "updated_ts")
    rows = s.scalars(select(PaperPosition).where(PaperPosition.session_id == sid)
                     .order_by(PaperPosition.strategy_id, PaperPosition.instrument)).all()
    return [{c: getattr(p, c) for c in cols} | {"notional": p.quantity * (p.last_price or 0) * p.fx_rate} for p in rows]


@router.get("/sessions/{sid}/equity")
def equity(sid: str, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    _ps(s, sid)
    cols = ("bar_ts", "cash", "equity", "realized_pnl", "unrealized_pnl", "fees", "holding_costs", "gross_exposure",
            "net_exposure", "margin_used", "drawdown")
    rows = s.scalars(select(PaperEquity).where(PaperEquity.session_id == sid).order_by(PaperEquity.bar_ts)).all()
    return [{c: getattr(e, c) for c in cols} for e in rows]


@router.get("/sessions/{sid}/events")
def events(sid: str, level: str | None = None, category: str | None = None, strategy: str | None = None,
           instrument: str | None = None, start: datetime | None = None, end: datetime | None = None,
           limit: int = Query(500, le=20000), s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    _ps(s, sid)
    q = _filters(select(PaperEvent).where(PaperEvent.session_id == sid), PaperEvent, strategy, instrument, start, end,
                 PaperEvent.sim_ts)
    if level:
        q = q.where(PaperEvent.level.in_(level.split(",")))
    if category:
        q = q.where(PaperEvent.category.in_(category.split(",")))
    cols = ("seq", "sim_ts", "level", "category", "code", "message", "strategy_id", "instrument", "details",
            "created_at")
    return [{c: getattr(e, c) for c in cols} for e in s.scalars(q.order_by(PaperEvent.seq.desc()).limit(limit))]


@router.get("/sessions/{sid}/risk")
def risk_decisions(sid: str, decision: str | None = None, limit: int = Query(500, le=20000),
                   s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    ps = _ps(s, sid)
    q = select(RiskDecision).where(RiskDecision.run_id == ps.experiment_id)
    if decision:
        q = q.where(RiskDecision.decision == decision)
    rows = s.scalars(q.order_by(RiskDecision.created_at.desc()).limit(limit)).all()
    return [{"as_of": d.as_of, "scope": d.scope, "rule": d.rule, "observed": d.observed, "limit": d.limit_value,
             "decision": d.decision, "action": d.action, "reason": d.reason, "policy_version": d.policy_version}
            for d in rows]


@router.post("/sessions/{sid}/reconcile")
def run_reconcile(sid: str, body: Actor, s: Session = Depends(get_session)) -> dict[str, Any]:
    ps = _ps(s, sid)
    res = reconcile(s, ps)
    if res["ok"] and (ps.summary or {}).get("reconciliation_failed"):
        ps.summary = {**ps.summary, "reconciliation_failed": False}
        engine.emit(s, ps, "warn", "reconcile", "reconciliation_cleared", f"reconciliation passed (run by {body.by})")
    elif not res["ok"]:
        engine.emit(s, ps, "critical", "reconcile", "reconciliation_failed", f"reconciliation failed (run by {body.by})",
                    mismatches=res["mismatches"][:20])
    return res


@router.post("/sessions/{sid}/evaluation")
def evaluate(sid: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    ps = _ps(s, sid)
    ev = evaluation.evaluate(s, ps)
    service.store_evaluation(s, ps, ev)
    return ev


@router.get("/sessions/{sid}/evaluation")
def get_evaluation(sid: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    ps = _ps(s, sid)
    if not ps.evaluation:
        raise HTTPException(404, "not evaluated yet")
    return ps.evaluation


@router.get("/sessions/{sid}/export", response_class=PlainTextResponse)
def export(sid: str, table: str = "orders", s: Session = Depends(get_session)) -> PlainTextResponse:
    ps = _ps(s, sid)
    text = _guard(service.export_csv, s, ps, table)
    return PlainTextResponse(text, media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="paper_{sid[:8]}_{table}.csv"'})


@router.get("/compare")
def compare(ids: str, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    out = []
    for sid in [x for x in ids.split(",") if x][:10]:
        ps = _ps(s, sid)
        m = (ps.evaluation or {}).get("metrics", {})
        out.append({"id": ps.id, "name": ps.name, "status": ps.status, "mode": ps.mode, "is_synthetic": ps.is_synthetic,
                    "is_demo": ps.is_demo, "bars": ps.bars_processed, "summary": _out(ps)["summary"],
                    "cost_scenario": ps.config.get("cost_scenario"), "order_type": ps.config.get("order_type"),
                    "stop_loss_pct": ps.config.get("stop_loss_pct"), "verdict": (ps.evaluation or {}).get("verdict"),
                    "fill_rate": m.get("fill_rate"), "return_gap": m.get("return_gap"),
                    "paper_max_drawdown": m.get("paper_max_drawdown")})
    return out
