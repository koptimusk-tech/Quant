from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.anomaly.detectors import DETECTORS
from qal.anomaly.engine import ScanConfig, run_scan
from qal.api.schemas import ExperimentOut, JobOut, ScanIn
from qal.config import get_settings
from qal.data.store import ParquetStore
from qal.db import get_session
from qal.jobs.runner import submit_job
from qal.models import Experiment

router = APIRouter(tags=["research"])


@router.get("/detectors")
def list_detectors() -> list[dict[str, Any]]:
    return [{"name": d.name, "version": d.version, "anomaly_type": d.anomaly_type, "pairwise": d.pairwise,
             "description": d.description, "defaults": d.defaults} for d in DETECTORS.values()]


@router.post("/scans")
def create_scan(body: ScanIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Run an anomaly scan (recorded as an experiment). Identical requests return the cached experiment."""
    payload = body.model_dump(exclude={"run_async", "idempotency_key"})
    if not body.dataset_ids and not body.pairs:
        raise HTTPException(400, "Provide dataset_ids and/or pairs")
    if body.run_async:
        job = submit_job(s, "scan", payload, idempotency_key=body.idempotency_key)
        return {"job": JobOut.model_validate(job).model_dump(mode="json")}
    cfg = ScanConfig(dataset_ids=body.dataset_ids, detectors=body.detectors or ScanConfig().detectors,
                     pairs=[tuple(p) for p in body.pairs], cost_bps=body.cost_bps,
                     min_dollar_volume=body.min_dollar_volume, label=body.label, force=body.force)
    try:
        exp = run_scan(s, cfg, store=ParquetStore(get_settings().data_dir))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"experiment": ExperimentOut.model_validate(exp).model_dump(mode="json")}


@router.get("/experiments", response_model=list[ExperimentOut])
def list_experiments(kind: str | None = None, status: str | None = None, limit: int = Query(50, le=500),
                     s: Session = Depends(get_session)):
    q = select(Experiment).order_by(Experiment.created_at.desc()).limit(limit)
    if kind:
        q = q.where(Experiment.kind == kind)
    if status:
        q = q.where(Experiment.status == status)
    return s.scalars(q).all()


@router.get("/experiments/{experiment_id}", response_model=ExperimentOut)
def get_experiment(experiment_id: str, s: Session = Depends(get_session)):
    exp = s.get(Experiment, experiment_id)
    if exp is None:
        raise HTTPException(404, "experiment not found")
    return exp
