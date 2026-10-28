"""Strategy Review & Readiness: inventory, deterministic decisions with history, manual reviews, amendments and
reports of the paper trading results.

Simulation only. No endpoint can send an order or authorise real trading. Manual reviews and amendments need a
named person; agents and LLMs are refused. Decisions are never edited: a new review is a new record.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from qal.db import get_session
from qal.models import Experiment
from qal.review import (
    criteria,
    decision,
    diagnosis,
    metrics,
    report,
    service,
    uncertainty,
)

router = APIRouter(prefix="/review", tags=["strategy review"])


class RunIn(BaseModel):
    by: str = Field(min_length=2, max_length=64)
    subjects: list[str] | None = None


class ManualIn(BaseModel):
    by: str = Field(min_length=2, max_length=64)
    state: str
    rationale: str = Field(min_length=1, max_length=4000)


@router.get("/meta")
def meta() -> dict[str, Any]:
    return {"versions": {"review": service.REVIEW_VERSION, "engine": decision.ENGINE_VERSION,
                         "metrics": metrics.METRICS_VERSION, "amendments": criteria.AMENDMENT_VERSION},
            "states": list(decision.STATES), "manual_allowed": decision.ALLOWED_MANUAL, "rules": decision.__doc__,
            "uncertainty": uncertainty.__doc__, "diagnosis": diagnosis.__doc__, "criteria": criteria.__doc__,
            "evidence_classes": {"PROSPECTIVE": "bars that closed after the protocol was frozen (mode B)",
                                 "HISTORICAL": "data already known when processed (replay)",
                                 "SYNTHETIC": "synthetic data: tests the procedure only",
                                 "NEW_DATA_WITHOUT_PROTOCOL": "new data without a protocol frozen beforehand"},
            "amendable": list(criteria.AMENDABLE), "live_trading_enabled": False,
            "note": decision.NOT_AUTHORISATION}


@router.get("/summary")
def summary(s: Session = Depends(get_session)) -> dict[str, Any]:
    return service.phase_summary(s)


@router.post("/run")
def run(body: RunIn, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = service.run(s, body.by, subjects=body.subjects)
    s.commit()
    return rows


def _latest_rows(s: Session) -> list[Experiment]:
    out = []
    for ps, prog in service._all_subjects(s):
        sub = service.subject_id("program", prog.id) if prog is not None else service.subject_id("session", ps.id)
        last = service.latest(s, sub)
        if last is not None:
            out.append(last)
    return out


@router.get("/strategies")
def strategies(strategy: str | None = None, market: str | None = None, asset: str | None = None,
               start: str | None = None, end: str | None = None, state: str | None = None,
               evidence_class: str | None = None, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Latest decision per strategy version, with the current state (a manual review when one exists)."""
    rows = []
    for rec in _latest_rows(s):
        r = {**service.summary_row(rec), "current": service.current_state(s, rec.results["subject"])}
        if strategy and strategy.lower() not in (r["strategy"] or "").lower():
            continue
        if market and market not in r["markets"]:
            continue
        if asset and asset not in r["assets"]:
            continue
        if start and (r["last_bar"] or "") < start:
            continue
        if end and (r["first_bar"] or "9999") > end:
            continue
        if state and state not in (r["state"], r["current"].get("state")):
            continue
        if evidence_class and r["evidence_class"] != evidence_class:
            continue
        rows.append(r)
    return rows


@router.get("/inventory")
def inventory(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """The evidence inventory stored with the latest decision of every subject (run a review to refresh it)."""
    return [rec.config["inventory"] for rec in _latest_rows(s)]


def _dec(s: Session, did: str) -> Experiment:
    try:
        return service.get_decision(s, did)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/decisions/{did}")
def get_decision(did: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    rec = _dec(s, did)
    return {"summary": service.summary_row(rec), "report": report.build(s, rec),
            "manual_allowed": decision.ALLOWED_MANUAL[rec.config["decision"]["state"]],
            "is_latest": (service.latest(s, rec.results["subject"]) or rec).id == rec.id}


@router.get("/decisions/{did}/report.json")
def report_json(did: str, s: Session = Depends(get_session)) -> Response:
    import json

    rep = report.build(s, _dec(s, did))
    return Response(json.dumps(rep, indent=2, default=str), media_type="application/json",
                    headers={"content-disposition": f'attachment; filename="strategy_review_{did}.json"'})


@router.get("/decisions/{did}/report.md")
def report_md(did: str, s: Session = Depends(get_session)) -> Response:
    rep = report.build(s, _dec(s, did))
    return Response(report.markdown(rep), media_type="text/markdown",
                    headers={"content-disposition": f'attachment; filename="strategy_review_{did}.md"'})


@router.get("/decisions/{did}/regenerate")
def regenerate(did: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    _dec(s, did)
    return service.regenerate(s, did)


@router.post("/decisions/{did}/manual", status_code=201)
def manual(did: str, body: ManualIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    _dec(s, did)
    try:
        m = service.manual_review(s, did, body.by, body.state, body.rationale)
    except service.ReviewError as exc:
        raise HTTPException(400, str(exc)) from exc
    s.commit()
    return {"id": m.id, **m.config}


@router.get("/history")
def history(subject: str, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    out = []
    for h in service.history(s, subject):
        if h.kind == service.KIND:
            out.append({"kind": "engine", "id": h.id, "at": h.config["decided_at"], "state": h.config["decision"]["state"],
                        "reason": h.config["decision"]["reason"], "by": h.config["triggered_by"],
                        "engine_version": h.algorithm_version, "previous": h.config.get("previous_decision_id"),
                        "integrity_ok": service.verify(h)})
        else:
            out.append({"kind": "manual", "id": h.id, "at": h.config["at"], "state": h.config["state"],
                        "reason": h.config["rationale"], "by": h.config["by"], "decision_id": h.config["decision_id"],
                        "engine_state": h.config["engine_state"], "warning": h.config.get("warning"),
                        "integrity_ok": service.verify(h)})
    return out


@router.post("/protocols/{pid}/amendments", status_code=201)
def amend(pid: str, body: dict[str, Any], s: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        a = criteria.amend(s, pid, criteria.AmendmentIn.model_validate(body))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValidationError, criteria.AmendmentError) as exc:
        raise HTTPException(400, str(exc)) from exc
    s.commit()
    return {"id": a.id, **a.config}


@router.get("/protocols/{pid}/amendments")
def amendments(pid: str, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [{"id": a.id, **a.config, "integrity_ok": a.config_hash == service.stable_hash(a.config)}
            for a in criteria.amendments(s, pid)]
