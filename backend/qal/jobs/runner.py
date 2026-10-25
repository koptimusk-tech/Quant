"""Persistent job queue and interval scheduler running in a background thread.

* Jobs live in the database (status, attempts, result, error), so state survives restarts.
* Claiming uses a conditional UPDATE, so two workers never run the same job.
* Failed jobs are retried with exponential backoff up to ``max_attempts``.
* ``idempotency_key`` makes submissions safe to repeat (same key -> same job).
* Schedules enqueue one job per due slot, keyed by schedule id + slot time.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from qal import audit
from qal.config import get_settings
from qal.db import session_scope
from qal.jobs.tasks import HANDLERS
from qal.models import Job, Schedule

log = logging.getLogger(__name__)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def submit_job(session: Session, kind: str, payload: dict[str, Any], *, idempotency_key: str | None = None,
               schedule_id: str | None = None, actor: str = "api") -> Job:
    if kind not in HANDLERS:
        raise ValueError(f"Unknown job kind {kind!r}; available: {list(HANDLERS)}")
    if idempotency_key:
        existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing is not None:
            return existing
    job = Job(kind=kind, payload=payload, idempotency_key=idempotency_key, schedule_id=schedule_id,
              max_attempts=get_settings().job_max_attempts)
    session.add(job)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
    audit.record(session, actor, "job_submitted", "job", job.id, kind=kind)
    return job


def cancel_job(session: Session, job: Job) -> bool:
    """A queued job is cancelled at once. A running job is marked ``cancelling``: handlers that support it
    (``model_experiment``) stop between units of work and keep their partial results; others run to the end."""
    if job.status == "queued":
        job.status = "cancelled"
        job.finished_at = datetime.now(UTC)
    elif job.status == "running":
        job.status = "cancelling"
    else:
        return False
    audit.record(session, "api", "job_cancel_requested" if job.status == "cancelling" else "job_cancelled",
                 "job", job.id)
    return True


def stop_requested(job_id: str) -> bool:
    with session_scope() as s:
        return s.scalar(select(Job.status).where(Job.id == job_id)) == "cancelling"


def _claim_next(now: datetime) -> str | None:
    with session_scope() as s:
        cand = s.scalars(select(Job.id).where(Job.status == "queued", Job.run_after <= now)
                         .order_by(Job.created_at).limit(5)).all()
        for job_id in cand:
            res = s.execute(update(Job).where(Job.id == job_id, Job.status == "queued")
                            .values(status="running", started_at=now, attempts=Job.attempts + 1))
            if res.rowcount == 1:
                return job_id
    return None


def run_job(job_id: str, actor: str = "worker") -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        kind, payload = job.kind, {**job.payload, "_job_id": job_id}
    try:
        with session_scope() as s:
            result = HANDLERS[kind](s, payload, actor)
        with session_scope() as s:
            job = s.get(Job, job_id)
            status = "cancelled" if job.status == "cancelling" else "succeeded"
            job.status, job.result, job.error, job.finished_at = status, result, None, datetime.now(UTC)
            audit.record(s, actor, f"job_{status}", "job", job_id)
    except Exception as exc:
        log.exception("job %s failed", job_id)
        with session_scope() as s:
            job = s.get(Job, job_id)
            job.error = f"{type(exc).__name__}: {exc}"
            if job.status == "cancelling":
                job.status, job.finished_at = "cancelled", datetime.now(UTC)
            elif job.attempts < job.max_attempts:
                job.status = "queued"
                job.run_after = datetime.now(UTC) + timedelta(seconds=30 * 2 ** (job.attempts - 1))
            else:
                job.status = "failed"
                job.finished_at = datetime.now(UTC)
            audit.record(s, actor, "job_failed", "job", job_id, error=job.error, attempts=job.attempts,
                         will_retry=job.status == "queued")


def enqueue_due_schedules(now: datetime) -> int:
    n = 0
    with session_scope() as s:
        for sch in s.scalars(select(Schedule).where(Schedule.enabled.is_(True))).all():
            slot = _aware(sch.next_run_at)
            if slot > now:
                continue
            submit_job(s, sch.kind, sch.payload, idempotency_key=f"schedule:{sch.id}:{slot.isoformat()}",
                       schedule_id=sch.id, actor="scheduler")
            sch.last_run_at = now
            # Skip missed slots instead of replaying a backlog after downtime.
            step = timedelta(seconds=sch.interval_seconds)
            nxt = slot + step
            while nxt <= now:
                nxt += step
            sch.next_run_at = nxt
            n += 1
    return n


def process_once(now: datetime | None = None) -> int:
    """Enqueue due schedules and run all due jobs. Returns the number of jobs run."""
    now = now or datetime.now(UTC)
    enqueue_due_schedules(now)
    ran = 0
    while (job_id := _claim_next(now)) is not None:
        run_job(job_id)
        ran += 1
    return ran


class Worker:
    def __init__(self, poll_seconds: float) -> None:
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # Jobs left "running" by a crash are re-queued (handlers are idempotent).
        with session_scope() as s:
            s.execute(update(Job).where(Job.status == "running").values(status="queued"))
            s.execute(update(Job).where(Job.status == "cancelling").values(status="cancelled",
                                                                            finished_at=datetime.now(UTC)))
        with session_scope() as s:
            from qal.lab.service import enqueue_orphans

            enqueue_orphans(s)  # Experiment Lab runs interrupted by the crash go back to the queue
        self._thread = threading.Thread(target=self._loop, name="qal-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                process_once()
            except Exception:  # keep the loop alive
                log.exception("worker iteration failed")
            self._stop.wait(self.poll_seconds)
