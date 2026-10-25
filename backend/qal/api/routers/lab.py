"""Experiment Lab: generate, run, compare and rank strategy experiments under one validation pipeline.

Simulation and research only: nothing here changes a hypothesis status, a paper session or a risk policy, and
nothing can place an order. A decision label (PAPER_ELIGIBLE included) is evidence for a human review, never an
automatic promotion.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.api.schemas import JobOut
from qal.config import get_settings
from qal.data.store import ParquetStore
from qal.db import get_session
from qal.lab import generator, selection, service
from qal.lab.pipeline import MIN_INDEPENDENT_TRADES, MIN_OOS_BARS, PIPELINE_VERSION
from qal.lab.spec import BASELINES, DEFAULT_BASELINES, FAMILIES, NOT_VALID_BASELINES, SPEC_VERSION, ExperimentSpec
from qal.models import Experiment

router = APIRouter(prefix="/lab", tags=["experiment lab"])


class SubmitIn(BaseModel):
    spec: dict[str, Any]
    run_async: bool = True
    generator_key: str | None = None


class GenerateIn(BaseModel):
    families: list[str] | None = None
    limit: int = Field(20, ge=1, le=100)


class SelectionIn(BaseModel):
    actor: str = Field("user", min_length=1, max_length=64)
    note: str = Field("", max_length=2000)


class PortfolioIn(BaseModel):
    experiment_ids: list[str] = Field(min_length=2, max_length=12)


@router.get("/meta")
def meta() -> dict[str, Any]:
    """Families, baselines, versions, decision labels, score weights and the minimum sample used by the lab."""
    return {"spec_version": SPEC_VERSION, "pipeline_version": PIPELINE_VERSION,
            "selection_version": selection.SELECTION_VERSION, "generator_version": generator.GENERATOR_VERSION,
            "families": FAMILIES, "baselines": list(BASELINES), "default_baselines": DEFAULT_BASELINES,
            "not_valid_baselines": NOT_VALID_BASELINES, "decisions": list(selection.DECISIONS),
            "weights": selection.WEIGHTS, "states": list(service.STATES),
            "min_oos_bars": MIN_OOS_BARS, "min_independent_trades": MIN_INDEPENDENT_TRADES,
            "not_a_guarantee": selection.NOT_A_GUARANTEE, "live_trading_enabled": False}


@router.post("/generate")
def generate(body: GenerateIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Propose experiment specs from the loaded data (deterministic rules; nothing is run or stored)."""
    unknown = sorted(set(body.families or []) - set(FAMILIES))
    if unknown:
        raise HTTPException(400, f"unknown families {unknown}")
    return generator.generate(s, families=body.families, limit=body.limit)


def _summary(e: Experiment) -> dict[str, Any]:
    r = e.results or {}
    p = r.get("pipeline") or {}
    d = p.get("decision_at_completion") or {}
    st = (p.get("E_statistics") or {}).get("oos") or {}
    return {"id": e.id, "status": e.status, "title": e.config.get("title"), "family": e.config.get("family"),
            "strategy": e.config["spec"]["contract"]["strategy"]["type"],
            "universe": [x.get("symbol") for x in e.data_refs],
            "is_synthetic": any(x.get("is_synthetic") for x in e.data_refs), "variants": e.n_tests,
            "generated_by": e.config.get("generated_by"), "attempts": r.get("attempts", 0), "stage": r.get("stage"),
            "created_at": e.created_at, "finished_at": e.finished_at, "duration_s": r.get("duration_s"),
            "error": e.error, "decision_at_completion": d.get("decision"), "score_at_completion": d.get("score"),
            "warnings": d.get("warnings", []), "oos_sharpe_net": st.get("sharpe_net"), "oos_bars": st.get("bars"),
            "invalid_checks": p.get("invalid"), "spec_hash": e.config.get("spec_hash")}


def _get(s: Session, exp_id: str) -> Experiment:
    e = s.get(Experiment, exp_id)
    if e is None or e.kind != service.KIND:
        raise HTTPException(404, "lab experiment not found")
    return e


@router.post("/experiments", status_code=202)
def submit(body: SubmitIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Create (or import) an experiment from a spec. The same spec on the same data returns the stored experiment;
    with run_async the job runner executes it, otherwise it runs now. Invalid specs return 400."""
    try:
        spec = ExperimentSpec.model_validate(body.spec)
        exp, created = service.create(s, spec, generator_key=body.generator_key)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    job = None
    if exp.status == "QUEUED":
        if body.run_async:
            job = JobOut.model_validate(service.active_job(s, exp) or service.submit(s, exp)).model_dump(mode="json")
        else:
            service.run(s, exp, store=ParquetStore(get_settings().data_dir))
    return {"experiment": _summary(exp), "created": created, "job": job}


@router.get("/experiments")
def list_experiments(status: str | None = None, limit: int = Query(200, le=1000),
                     s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    q = select(Experiment).where(Experiment.kind == service.KIND)
    if status:
        q = q.where(Experiment.status == status)
    return [_summary(e) for e in s.scalars(q.order_by(Experiment.created_at.desc()).limit(limit))]


@router.get("/experiments/{exp_id}")
def get_experiment(exp_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    e = _get(s, exp_id)
    job = service.active_job(s, e)
    return {**_summary(e), "config": e.config, "data_refs": e.data_refs, "code_version": e.code_version,
            "algorithm_version": e.algorithm_version, "config_hash": e.config_hash, "seed": e.seed,
            "results": e.results, "job": JobOut.model_validate(job).model_dump(mode="json") if job else None,
            "lineage": service.lineage(s, e)}


@router.post("/experiments/{exp_id}/cancel")
def cancel(exp_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        e = service.request_cancel(s, _get(s, exp_id))
    except service.LabError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _summary(e)


@router.get("/experiments/{exp_id}/export.json")
def export_json(exp_id: str, s: Session = Depends(get_session)) -> Response:
    """The full stored record: spec, data snapshots, versions, every stage and the decision with its reasons."""
    body = get_experiment(exp_id, s)
    return Response(json.dumps(body, default=str, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="lab_experiment_{exp_id}.json"'})


@router.get("/ranking")
def ranking(include_synthetic: bool = True, s: Session = Depends(get_session)) -> dict[str, Any]:
    return service.ranking(s, include_synthetic=include_synthetic)


RANKING_COLUMNS = ["rank", "experiment_id", "title", "family", "strategy", "universe", "is_synthetic", "decision",
                   "score", "raw_p", "adjusted_p", "oos_sharpe_net", "oos_mean_net", "oos_bars", "n_trades", "dsr",
                   "n_trials", "variants", "baselines_beaten", "reasons", "warnings"]


@router.get("/ranking/export.csv")
def export_ranking(include_synthetic: bool = True, s: Session = Depends(get_session)) -> Response:
    buf = io.StringIO()
    w = csv.DictWriter(buf, RANKING_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in service.ranking(s, include_synthetic=include_synthetic)["rows"]:
        w.writerow({**r, **{k: " | ".join(map(str, r[k])) for k in ("universe", "baselines_beaten", "reasons",
                                                                      "warnings")}})
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="lab_ranking.csv"'})


@router.post("/selections", status_code=201)
def record_selection(body: SelectionIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Freeze the current ranking as an audited record. It does not promote, approve or allocate anything."""
    e = service.record_selection(s, actor=body.actor, note=body.note)
    return {"id": e.id, "created_at": e.created_at, "n_experiments": e.results["n_experiments"]}


@router.get("/selections")
def list_selections(limit: int = Query(50, le=500), s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    q = (select(Experiment).where(Experiment.kind == "lab_selection").order_by(Experiment.created_at.desc())
         .limit(limit))
    return [{"id": e.id, "created_at": e.created_at, "recorded_by": e.config.get("recorded_by"),
             "note": e.config.get("note"), "version": e.algorithm_version, "ranking": e.results} for e in s.scalars(q)]


@router.post("/portfolio")
def portfolio(body: PortfolioIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        return service.portfolio_contribution(s, body.experiment_ids)
    except service.LabError as exc:
        raise HTTPException(400, str(exc)) from exc
