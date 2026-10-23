from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal import audit
from qal.api.schemas import JobOut, ScheduleIn, ScheduleOut, SchedulePatch
from qal.db import get_session
from qal.jobs.runner import cancel_job, submit_job
from qal.models import Job, Schedule

router = APIRouter(tags=["jobs"])


class JobIn(BaseModel):
    kind: str
    payload: dict[str, Any]
    idempotency_key: str | None = None


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(status: str | None = None, limit: int = Query(50, le=500), s: Session = Depends(get_session)):
    q = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if status:
        q = q.where(Job.status == status)
    return s.scalars(q).all()


@router.post("/jobs", response_model=JobOut, status_code=202)
def create_job(body: JobIn, s: Session = Depends(get_session)):
    try:
        return submit_job(s, body.kind, body.payload, idempotency_key=body.idempotency_key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, s: Session = Depends(get_session)):
    job = s.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel(job_id: str, s: Session = Depends(get_session)):
    job = s.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if not cancel_job(s, job):
        raise HTTPException(409, f"job is {job.status}; only queued jobs can be cancelled")
    return job


@router.get("/schedules", response_model=list[ScheduleOut])
def list_schedules(s: Session = Depends(get_session)):
    return s.scalars(select(Schedule).order_by(Schedule.created_at)).all()


@router.post("/schedules", response_model=ScheduleOut, status_code=201)
def create_schedule(body: ScheduleIn, s: Session = Depends(get_session)):
    """Recurring job, e.g. ``ingest_and_scan`` to download new bars automatically and scan them."""
    if body.kind in ("ingest", "ingest_and_scan") and not body.payload.get("items"):
        raise HTTPException(400, "payload.items (list of ingest requests) is required")
    if body.kind == "scan" and not (body.payload.get("instrument_ids") or body.payload.get("dataset_ids")):
        raise HTTPException(400, "payload.instrument_ids (scanned at their latest data) is required")
    sch = Schedule(name=body.name, kind=body.kind, payload=body.payload, interval_seconds=body.interval_seconds,
                   enabled=body.enabled, next_run_at=body.start_at or datetime.now(UTC))
    s.add(sch)
    s.flush()
    audit.record(s, "api", "schedule_created", "schedule", sch.id, kind=body.kind, interval=body.interval_seconds)
    return sch


@router.patch("/schedules/{sid}", response_model=ScheduleOut)
def patch_schedule(sid: str, body: SchedulePatch, s: Session = Depends(get_session)):
    sch = s.get(Schedule, sid)
    if sch is None:
        raise HTTPException(404, "schedule not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(sch, k, v)
    audit.record(s, "api", "schedule_updated", "schedule", sid, **body.model_dump(exclude_none=True, mode="json"))
    return sch


@router.delete("/schedules/{sid}", status_code=204)
def delete_schedule(sid: str, s: Session = Depends(get_session)):
    sch = s.get(Schedule, sid)
    if sch is None:
        raise HTTPException(404, "schedule not found")
    for job in s.scalars(select(Job).where(Job.schedule_id == sid)).all():
        job.schedule_id = None
    s.delete(sch)
    audit.record(s, "api", "schedule_deleted", "schedule", sid)
