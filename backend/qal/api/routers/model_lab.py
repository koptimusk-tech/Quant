"""Model Lab: anomaly models, documented features, and stored model comparisons."""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.anomaly.detectors import DETECTORS
from qal.api.schemas import JobOut
from qal.config import get_settings
from qal.data.store import ParquetStore
from qal.db import get_session
from qal.features.catalog import DEFAULT_FEATURES, FEATURES
from qal.jobs.runner import submit_job
from qal.models import Experiment
from qal.research import model_lab

router = APIRouter(prefix="/models", tags=["model lab"])


class ModelIn(BaseModel):
    detector: str
    params: dict[str, Any] = Field(default_factory=dict)
    label: str = ""


class ComparisonIn(BaseModel):
    dataset_ids: list[str] = Field(default_factory=list)
    models: list[ModelIn] = Field(default_factory=lambda: [ModelIn(**m) for m in model_lab.DEFAULT_MODELS])
    seeds: list[int] = Field(default_factory=lambda: [0, 1, 2])
    window_variants: list[float] = Field(default_factory=lambda: [2 / 3, 4 / 3])
    predictive: bool = True
    horizons: list[int] = Field(default_factory=lambda: [1, 5, 20])
    max_seconds: float = 1800
    benchmark: bool = False
    force: bool = False
    label: str = ""
    run_async: bool = True


@router.get("/detectors")
def detectors() -> list[dict[str, Any]]:
    """Single-series detectors the Model Lab can compare, with their parameters and defaults."""
    return [{"name": d.name, "version": d.version, "anomaly_type": d.anomaly_type, "description": d.description,
             "defaults": d.defaults, "stochastic": bool(getattr(d, "stochastic", False)),
             "multivariate": "features" in d.defaults}
            for d in DETECTORS.values() if not d.pairwise]


@router.get("/features")
def features() -> dict[str, Any]:
    """Feature catalog: formula, source, window, availability, missing values, assumptions and biases."""
    return {"default": list(DEFAULT_FEATURES), "features": [f.as_dict() for f in FEATURES.values()]}


def _summary(e: Experiment) -> dict[str, Any]:
    r = e.results or {}
    return {"id": e.id, "status": e.status, "created_at": e.created_at, "finished_at": e.finished_at,
            "label": e.config.get("label", ""), "models": [m["key"] for m in e.config.get("models", [])],
            "datasets": [f"{d.get('symbol')} {d.get('frequency')}" for d in e.data_refs],
            "is_synthetic": r.get("is_synthetic"), "benchmark": bool(e.config.get("benchmark")),
            "duration_s": r.get("duration_s"), "budget_exhausted": bool(r.get("budget_exhausted")),
            "error": e.error}


@router.post("/experiments", status_code=202)
def create(body: ComparisonIn, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Queue a model comparison (or run it now with run_async=false). Configuration errors return 400."""
    payload = body.model_dump(exclude={"run_async"})
    try:
        cfg = model_lab.as_config(payload)
        cfg.validate()
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if body.run_async:
        job = submit_job(s, "model_experiment", payload)
        return {"job": JobOut.model_validate(job).model_dump(mode="json")}
    try:
        exp = model_lab.run_comparison(s, cfg, store=ParquetStore(get_settings().data_dir))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"experiment": _summary(exp)}


@router.get("/experiments")
def list_experiments(limit: int = Query(50, le=500), s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    q = (select(Experiment).where(Experiment.kind == "model_comparison")
         .order_by(Experiment.created_at.desc()).limit(limit))
    return [_summary(e) for e in s.scalars(q)]


def _get(s: Session, exp_id: str) -> Experiment:
    e = s.get(Experiment, exp_id)
    if e is None or e.kind != "model_comparison":
        raise HTTPException(404, "model comparison not found")
    return e


@router.get("/experiments/{exp_id}")
def get_experiment(exp_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    e = _get(s, exp_id)
    return {**_summary(e), "config": e.config, "data_refs": e.data_refs, "code_version": e.code_version,
            "algorithm_version": e.algorithm_version, "results": e.results}


EXPORT_COLUMNS = ["symbol", "frequency", "model", "detector", "status", "n_scored", "n_flagged", "n_episodes",
                  "flag_rate", "nominal_rate", "n_bh_significant", "rate_cv_across_years", "high_vol_rate",
                  "low_vol_rate", "seed_jaccard_mean", "window_jaccard_min", "runtime_s", "predictive_verdict",
                  "oos_n", "oos_mean_excess_bps", "oos_pvalue", "oos_run_wide_adjusted_pvalue", "reason"]


@router.get("/experiments/{exp_id}/export.csv")
def export_csv(exp_id: str, s: Session = Depends(get_session)) -> Response:
    """One row per dataset x model (the full record stays in the JSON)."""
    e = _get(s, exp_id)
    buf = io.StringIO()
    w = csv.DictWriter(buf, EXPORT_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for d in (e.results or {}).get("datasets", []):
        for r in d.get("rows", []):
            stab = r.get("stability") or {}
            win = [v["jaccard"] for v in stab.get("window_variants", []) if v.get("jaccard") is not None]
            pred = r.get("predictive") or {}
            oos = pred.get("out_of_sample") or {}
            reg = r.get("per_regime") or {}
            w.writerow({**r, "symbol": d["symbol"], "frequency": d["frequency"],
                        "high_vol_rate": (reg.get("high_vol") or {}).get("rate"),
                        "low_vol_rate": (reg.get("low_vol") or {}).get("rate"),
                        "seed_jaccard_mean": (stab.get("seeds") or {}).get("jaccard_mean"),
                        "window_jaccard_min": min(win) if win else None,
                        "predictive_verdict": pred.get("verdict"), "oos_n": oos.get("n"),
                        "oos_mean_excess_bps": oos.get("mean_excess_bps"), "oos_pvalue": oos.get("pvalue_one_sided"),
                        "oos_run_wide_adjusted_pvalue": pred.get("oos_run_wide_adjusted_pvalue"),
                        "reason": r.get("reason") or r.get("error")})
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="model_comparison_{exp_id}.csv"'})
