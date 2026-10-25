"""Robustness Lab: cross-market, temporal, parameter, cost, capacity, stress and diversification tests of completed
Experiment Lab candidates.

Research only: a robustness state describes the tests run under their stated assumptions. It is not a forecast,
it does not replace the statistical validation and it promotes, approves or allocates nothing. Nothing here can
place an order.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.api.schemas import JobOut
from qal.config import get_settings
from qal.data.store import ParquetStore
from qal.db import get_session
from qal.jobs.runner import submit_job
from qal.models import Experiment
from qal.robustness import engine, report, service

router = APIRouter(prefix="/robustness", tags=["robustness lab"])


@router.get("/meta")
def meta() -> dict[str, Any]:
    """States, dimensions and their criteria, execution scenarios and defaults used by the Robustness Lab."""
    return {"version": engine.ROBUSTNESS_VERSION, "states": list(report.STATES), "dimensions": list(report.DIMENSIONS),
            "essential": list(report.ESSENTIAL), "core_fragility": list(report.CORE_FRAGILITY),
            "criteria": report.CRITERIA, "rules": report.__doc__, "execution_scenarios": engine.EXECUTION_SCENARIOS,
            "perturbation": list(engine.PERTURB), "near_duplicate_correlation": engine.CORRELATED,
            "defaults": service.RobustnessConfig(experiment_id="-").model_dump(exclude={"experiment_id"}),
            "not_a_guarantee": "Robustness under tested assumptions is not a probability of future success and does "
                               "not authorise promotion.", "live_trading_enabled": False}


@router.get("/candidates")
def candidates(s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Audit of the lab candidates: decision, exploratory flag and latest robustness state."""
    return service.candidates(s)


class RunIn(service.RobustnessConfig):
    run_async: bool = True


@router.post("/runs", status_code=202)
def create_run(body: dict[str, Any], s: Session = Depends(get_session)) -> dict[str, Any]:
    """Queue a robustness run (or run it now with run_async=false). The same configuration on the same data returns
    the stored run. Unknown or unfinished lab experiments return 400."""
    try:
        cfg = RunIn.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    exp = s.get(Experiment, cfg.experiment_id)
    if exp is None or exp.kind != "lab_experiment" or exp.status != "COMPLETED":
        raise HTTPException(400, "only COMPLETED lab experiments can be evaluated")
    payload = cfg.model_dump(mode="json", exclude={"run_async"})
    if cfg.run_async:
        job = submit_job(s, service.KIND, payload)
        return {"run": None, "job": JobOut.model_validate(job).model_dump(mode="json")}
    try:
        e = service.run(s, service.RobustnessConfig.model_validate(payload), store=ParquetStore(get_settings().data_dir))
    except service.RobustnessError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"run": service.summary_row(e), "job": None}


def _get(s: Session, run_id: str) -> Experiment:
    e = s.get(Experiment, run_id)
    if e is None or e.kind != service.KIND:
        raise HTTPException(404, "robustness run not found")
    return e


@router.get("/runs")
def list_runs(experiment_id: str | None = None, state: str | None = None, limit: int = Query(200, le=1000),
              s: Session = Depends(get_session)) -> list[dict[str, Any]]:
    q = select(Experiment).where(Experiment.kind == service.KIND).order_by(Experiment.created_at.desc()).limit(limit)
    rows = [service.summary_row(e) for e in s.scalars(q)]
    return [r for r in rows if (experiment_id is None or r["experiment_id"] == experiment_id)
            and (state is None or r["state"] == state)]


@router.get("/runs/{run_id}")
def get_run(run_id: str, s: Session = Depends(get_session)) -> dict[str, Any]:
    e = _get(s, run_id)
    return {**service.summary_row(e), "config": e.config, "data_refs": e.data_refs, "code_version": e.code_version,
            "algorithm_version": e.algorithm_version, "config_hash": e.config_hash, "results": e.results}


@router.get("/runs/{run_id}/export.json")
def export_json(run_id: str, s: Session = Depends(get_session)) -> Response:
    return Response(json.dumps(get_run(run_id, s), default=str, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="robustness_{run_id}.json"'})


MATRIX_COLUMNS = ["category", "symbol", "asset_class", "market", "timeframe", "period", "provider", "quality_status",
                  "is_synthetic", "status", "cost_source", "validation", "oos_from", "oos_to", "bars", "n_trades",
                  "sample", "mean_net", "sharpe_gross", "sharpe_net", "total_return_gross", "total_return_net",
                  "max_drawdown", "turnover_ann", "corr_with_design", "near_duplicate_of_design",
                  "transfer_sharpe_net", "reason"]


@router.get("/runs/{run_id}/matrix.csv")
def export_matrix(run_id: str, s: Session = Depends(get_session)) -> Response:
    """The market and asset matrix of a run, one row per asset."""
    e = _get(s, run_id)
    buf = io.StringIO()
    w = csv.DictWriter(buf, MATRIX_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in ((e.results or {}).get("cross_asset") or {}).get("rows", []):
        w.writerow({**r, "period": " to ".join(r.get("period") or []),
                    "transfer_sharpe_net": (r.get("transfer_fixed_params") or {}).get("sharpe_net")})
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="robustness_matrix_{run_id}.csv"'})
