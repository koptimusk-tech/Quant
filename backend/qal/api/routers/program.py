"""Paper Trading Validation: candidate registry, protocols, programs, pauses, reviews and reports.

Simulation only. Every result carries its mode label (historical replay, prospective paper, synthetic simulation);
no endpoint can send an order, change a risk policy or authorise real trading. Admissions, protocols, resumes,
stops and reviews need a named person; agents are refused.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.api.schemas import JobOut
from qal.db import get_session
from qal.jobs.runner import submit_job
from qal.lab import service as lab
from qal.models import Experiment, PaperEvent
from qal.program import classify, protocol, registry, report, service

router = APIRouter(prefix="/program", tags=["paper trading validation"])


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(400, str(exc))


@router.get("/meta")
def meta() -> dict[str, Any]:
    return {"versions": {"registry": registry.REGISTRY_VERSION, "protocol": protocol.PROTOCOL_VERSION,
                         "program": service.PROGRAM_VERSION},
            "modes": protocol.MODES, "mode_notes": protocol.MODE_NOTES, "states": list(classify.STATES),
            "review_allowed": classify.ALLOWED, "rules": classify.__doc__, "pause_causes": protocol.PAUSE_CAUSES,
            "primary_metric": protocol.PRIMARY_METRIC, "secondary_metrics": list(protocol.SECONDARY_METRICS),
            "comparison_method": protocol.COMPARISON_METHOD, "missing_data": protocol.MISSING_DATA,
            "horizon_rule": protocol.__doc__, "admission_rules": registry.__doc__,
            "protocol_defaults": protocol.ProtocolIn.model_json_schema()["properties"],
            "risk_limit_defaults": registry.RiskLimits().model_dump(),
            "live_trading_enabled": False,
            "note": "A classification describes the simulation under its protocol; it is not a probability of future "
                    "profit and it authorises nothing."}


@router.get("/eligibility")
def eligibility(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Every completed lab experiment with the admission checks it passes or fails today."""
    out = []
    for e in s.scalars(select(Experiment).where(Experiment.kind == lab.KIND, Experiment.status == "COMPLETED")
                       .order_by(Experiment.created_at.desc())).all():
        rob = registry.latest_robustness(s, e.id)
        el = registry.eligibility(s, e, rob)
        out.append({"lab_experiment_id": e.id, "title": e.config.get("title"), "family": e.config.get("family"),
                    "is_synthetic": any(r.get("is_synthetic") for r in e.data_refs),
                    "universe": [r.get("symbol") for r in e.data_refs], "decision": el["decision"],
                    "robustness_run_id": rob.id if rob else None,
                    "robustness_state": (rob.results or {}).get("state") if rob else None,
                    "checks": el["always"] + el["evidence"],
                    "meets_evidence_requirements": all(c["passed"] for c in el["always"] + el["evidence"])})
    return out


@router.post("/candidates", status_code=201)
def admit(body: dict[str, Any], s: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        rec = registry.admit(s, registry.AdmissionIn.model_validate(body))
    except (ValidationError, registry.RegistryError) as exc:
        s.commit()  # a refused admission is stored too
        raise _bad(exc) from exc
    return {**registry.summary(rec), "snapshot": rec.config}


@router.get("/candidates")
def candidates(include_refused: bool = True, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = s.scalars(select(Experiment).where(Experiment.kind == registry.KIND)
                     .order_by(Experiment.created_at.desc())).all()
    return [registry.summary(r) for r in rows if include_refused or r.status == "admitted"]


def _cand(s: Session, cid: str) -> Experiment:
    try:
        return registry.get(s, cid)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/candidates/{cid}")
def candidate(cid: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    rec = _cand(s, cid)
    return {**registry.summary(rec), "snapshot": rec.config, "config_hash": rec.config_hash}


@router.get("/candidates/{cid}/versions")
def candidate_versions(cid: str, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Every version of the candidate's line, each with its own programs (results are never pooled across them)."""
    rec = _cand(s, cid)
    name = (rec.results or {}).get("name")
    progs = s.scalars(select(Experiment).where(Experiment.kind == service.KIND)).all()
    return [{**registry.summary(v), "programs": [service.summary_row(p) for p in progs
                                                 if p.config["candidate_id"] == v.id]}
            for v in registry.versions(s, name)]


@router.post("/protocols/preview")
def preview(body: dict[str, Any], s: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        _, cfg = protocol.build(s, protocol.ProtocolIn.model_validate(body))
    except (ValidationError, protocol.ProtocolError, LookupError) as exc:
        raise _bad(exc) from exc
    return cfg


@router.post("/protocols", status_code=201)
def create_protocol(body: dict[str, Any], s: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        e = protocol.create(s, protocol.ProtocolIn.model_validate(body))
    except (ValidationError, protocol.ProtocolError, LookupError) as exc:
        raise _bad(exc) from exc
    return {"id": e.id, "config_hash": e.config_hash, **e.config}


@router.get("/protocols")
def protocols(candidate_id: str | None = None, s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = s.scalars(select(Experiment).where(Experiment.kind == protocol.KIND)
                     .order_by(Experiment.created_at.desc())).all()
    used = {p.config["protocol_id"]: p.id for p in s.scalars(select(Experiment).where(Experiment.kind == service.KIND))}
    return [{"id": e.id, "candidate_id": e.results["candidate_id"], "candidate_name": e.config["candidate_name"],
             "candidate_version": e.config["candidate_version"], "mode": e.config["mode"],
             "mode_label": e.config["mode_label"], "frozen_at": e.config["frozen_at"],
             "created_by": e.config["created_by"], "horizon_bars": e.config["horizon"]["horizon_bars"],
             "program_id": used.get(e.id), "integrity_ok": protocol.verify(e), "config_hash": e.config_hash}
            for e in rows if candidate_id is None or e.results["candidate_id"] == candidate_id]


@router.get("/protocols/{pid}")
def get_protocol(pid: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        e = protocol.get(s, pid)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"id": e.id, "config_hash": e.config_hash, "integrity_ok": protocol.verify(e), **e.config}


class StartIn(BaseModel):
    protocol_id: str
    started_by: str = Field(min_length=2, max_length=64)


@router.post("/programs", status_code=201)
def start(body: StartIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        prog = service.start(s, body.protocol_id, body.started_by)
    except (service.ProgramError, LookupError) as exc:
        raise _bad(exc) from exc
    return service.summary_row(prog)


@router.get("/programs")
def programs(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = s.scalars(select(Experiment).where(Experiment.kind == service.KIND)
                     .order_by(Experiment.created_at.desc())).all()
    return [service.summary_row(p) for p in rows]


def _prog(s: Session, pid: str) -> Experiment:
    try:
        return service.get(s, pid)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/programs/{pid}")
def program(pid: str, events: int = Query(300, le=5000), s: Session = Depends(get_session)) -> dict[str, Any]:
    prog = _prog(s, pid)
    ev = service.evaluate(s, prog)
    ps, cand, proto = service.parts(s, prog)
    evs = s.scalars(select(PaperEvent).where(PaperEvent.session_id == ps.id).order_by(PaperEvent.seq.desc())
                    .limit(events)).all()
    r = prog.results
    return {"summary": service.summary_row(prog), "config": prog.config, "config_hash": prog.config_hash,
            "candidate": registry.summary(s.get(Experiment, prog.config["candidate_id"])), "candidate_snapshot": cand,
            "protocol": proto, "session": {"id": ps.id, "status": ps.status, "pause_reason": ps.pause_reason,
                                           "kill_switch": ps.kill_switch, "bars_processed": ps.bars_processed,
                                           "cursor": str(ps.cursor_ts) if ps.cursor_ts else None,
                                           "summary": ps.summary},
            "tracking": ev["tracking"], "comparison": ev["comparison"], "rule": ev["rule"],
            "classification": service.classification(prog), "review_allowed": list(classify.ALLOWED[ev["rule"]["state"]]),
            "pauses": r.get("pauses", []),
            "reviews": r.get("reviews", []), "timeline": r.get("timeline", []), "errors": r.get("errors", []),
            "feed_log": r.get("feed_log", [])[-500:], "missing_bars": r.get("missing_bars", 0),
            "stopped": r.get("stopped"), "finished": r.get("finished"),
            "events": [{"seq": e.seq, "sim_ts": str(e.sim_ts) if e.sim_ts else None, "level": e.level,
                        "category": e.category, "code": e.code, "message": e.message} for e in evs]}


class RunIn(BaseModel):
    max_bars: int = Field(500, ge=1, le=100_000)
    run_async: bool = True


@router.post("/programs/{pid}/run")
def run(pid: str, body: RunIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    prog = _prog(s, pid)
    if body.run_async:
        job = submit_job(s, "paper_program_run", {"program_id": prog.id, "max_bars": body.max_bars})
        return {"job": JobOut.model_validate(job).model_dump(mode="json"), "result": None}
    try:
        return {"job": None, "result": service.run(s, prog.id, body.max_bars)}
    except service.ProgramError as exc:
        raise _bad(exc) from exc


class ActIn(BaseModel):
    by: str = Field(min_length=2, max_length=64)
    reason: str = ""


class ResumeIn(ActIn):
    resolution: str = ""
    confirm: bool = False
    skip_bad_bar: bool = False


class StopIn(ActIn):
    confirm: bool = False


class KillIn(StopIn):
    active: bool = True


class ReviewIn(BaseModel):
    by: str = Field(min_length=2, max_length=64)
    decision: str
    rationale: str


def _act(s: Session, f: Any) -> dict[str, Any]:
    try:
        prog = f()
    except (service.ProgramError, LookupError) as exc:
        raise _bad(exc) from exc
    return service.summary_row(prog)


@router.post("/programs/{pid}/pause")
def pause(pid: str, body: ActIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    return _act(s, lambda: service.pause(s, pid, body.by, body.reason or "paused by a person"))


@router.post("/programs/{pid}/resume")
def resume(pid: str, body: ResumeIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    return _act(s, lambda: service.resume(s, pid, body.by, body.reason, body.resolution, body.confirm,
                                          body.skip_bad_bar))


@router.post("/programs/{pid}/stop")
def stop(pid: str, body: StopIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    return _act(s, lambda: service.stop(s, pid, body.by, body.reason, body.confirm))


@router.post("/programs/{pid}/kill-switch")
def kill(pid: str, body: KillIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    return _act(s, lambda: service.kill_switch(s, pid, body.by, body.active, body.confirm, body.reason))


@router.post("/programs/{pid}/review")
def review(pid: str, body: ReviewIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    return _act(s, lambda: service.review(s, pid, body.by, body.decision, body.rationale))


@router.get("/programs/{pid}/report")
def get_report(pid: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    return report.build(s, _prog(s, pid))


@router.get("/programs/{pid}/export.json")
def export_json(pid: str, s: Session = Depends(get_session)) -> Response:
    """The final report with the candidate snapshot, the protocol and the program configuration."""
    rep = report.build(s, _prog(s, pid))
    s.commit()
    return Response(json.dumps(rep, default=str, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="paper_validation_{pid}.json"'})
