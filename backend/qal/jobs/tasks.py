"""Job handlers. Payloads are plain JSON so jobs can be persisted, retried and scheduled."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from qal.anomaly.engine import ScanConfig, run_scan
from qal.config import get_settings
from qal.data.ingest import IngestRequest, ingest, latest_dataset
from qal.data.store import ParquetStore


def _dt(v: Any) -> datetime | None:
    return datetime.fromisoformat(v) if isinstance(v, str) and v else v


def ingest_request_from(payload: dict[str, Any]) -> IngestRequest:
    p = dict(payload)
    return IngestRequest(
        adapter=p["adapter"], symbol=p["symbol"], asset_class=p["asset_class"], frequency=p.get("frequency", "1d"),
        market=p.get("market", ""), currency=p.get("currency"), start=_dt(p.get("start")), end=_dt(p.get("end")),
        incremental=p.get("incremental", True), options=p.get("options", {}),
        adapter_config=p.get("adapter_config", {}),
    )


def handle_ingest(session: Session, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """payload: {"items": [ingest request, ...]} or a single ingest request."""
    items = payload.get("items") or [payload]
    results, errors = [], []
    for item in items:
        try:
            results.append(ingest(session, ingest_request_from(item), actor=actor))
            session.commit()
        except Exception as exc:
            session.rollback()
            errors.append({"symbol": item.get("symbol"), "adapter": item.get("adapter"), "error": f"{type(exc).__name__}: {exc}"})
            # Persist provider error state recorded during the failed attempt.
    if errors and not results:
        raise RuntimeError(f"All ingestions failed: {errors}")
    return {"ingested": results, "errors": errors}


def resolve_latest(session: Session, instrument_ids: list[str]) -> list[str]:
    from sqlalchemy import select

    from qal.models import Dataset

    out = []
    for iid in instrument_ids:
        ds = session.scalar(select(Dataset).where(Dataset.instrument_id == iid, Dataset.is_latest.is_(True))
                            .order_by(Dataset.ingested_at.desc()).limit(1))
        if ds is None:
            raise ValueError(f"instrument {iid} has no dataset")
        out.append(ds.id)
    return out


def handle_scan(session: Session, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    cfg = ScanConfig(
        dataset_ids=list(payload.get("dataset_ids", [])) + resolve_latest(session, payload.get("instrument_ids", [])),
        detectors=payload.get("detectors") or ScanConfig().detectors,
        pairs=[tuple(p) for p in payload.get("pairs", [])]
        + [tuple(resolve_latest(session, list(p))) for p in payload.get("instrument_pairs", [])],
        cost_bps=payload.get("cost_bps", {}),
        min_dollar_volume=payload.get("min_dollar_volume", ScanConfig().min_dollar_volume),
        label=payload.get("label", ""),
        force=payload.get("force", False),
    )
    exp = run_scan(session, cfg, store=ParquetStore(get_settings().data_dir), actor=actor)
    if exp.status == "failed":
        session.commit()  # keep the failed experiment in the registry
        raise RuntimeError(exp.error or "scan failed")
    return {"experiment_id": exp.id, "n_anomalies": (exp.results or {}).get("n_anomalies")}


def handle_ingest_and_scan(session: Session, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """Download (incrementally) every item, then scan the latest datasets. Used by schedules."""
    ing = handle_ingest(session, {"items": payload["items"]}, actor)
    ds_ids = [r["dataset_id"] for r in ing["ingested"]]
    by_symbol = {r["symbol"]: r["dataset_id"] for r in ing["ingested"]}
    pairs = [(by_symbol[a], by_symbol[b]) for a, b in payload.get("pairs", []) if a in by_symbol and b in by_symbol]
    by_instrument = {r["instrument_id"]: r["dataset_id"] for r in ing["ingested"]}
    pairs += [(by_instrument[a], by_instrument[b]) for a, b in payload.get("instrument_pairs", [])
              if a in by_instrument and b in by_instrument]
    scan = handle_scan(session, {"dataset_ids": ds_ids, "detectors": payload.get("detectors"), "pairs": pairs,
                                 "label": payload.get("label", "scheduled")}, actor)
    return {**ing, **scan}


def handle_validate(session: Session, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    from qal.models import Hypothesis
    from qal.research.service import run_validation

    hyp = session.get(Hypothesis, payload["hypothesis_id"])
    if hyp is None:
        raise ValueError("unknown hypothesis")
    exp = run_validation(session, hyp, actor=actor)
    session.commit()
    if exp.status == "failed":
        raise RuntimeError(exp.error or "validation failed")
    return {"experiment_id": exp.id, "decision": exp.results.get("decision"), "status": hyp.status}


def handle_alpha_cycle(session: Session, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """payload: {"budget": {...}, "costs": {symbol: cost model}, "run_experiments": bool}."""
    from qal.alpha.agents import ResearchCoordinator
    from qal.alpha.messages import Budget
    from qal.research.costs import CostModel

    run = ResearchCoordinator().run_cycle(
        session, Budget(**payload.get("budget", {})), actor=actor,
        costs={k: CostModel(**v) for k, v in (payload.get("costs") or {}).items()},
        run_experiments=payload.get("run_experiments", True), checkpoint=session.commit)
    session.commit()
    return {"run_id": run.id, **{k: v for k, v in run.results.items() if k.startswith("n_")}}


def handle_model_experiment(session: Session, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """payload: a Model Lab comparison (see ``qal.research.model_lab.as_config``). Cancellable while running."""
    from qal.jobs.runner import stop_requested
    from qal.research.model_lab import as_config, run_comparison

    job_id = payload.get("_job_id")
    exp = run_comparison(session, as_config(payload), store=ParquetStore(get_settings().data_dir), actor=actor,
                         should_stop=(lambda: stop_requested(job_id)) if job_id else (lambda: False),
                         checkpoint=session.commit)
    session.commit()
    if exp.status == "failed":
        raise RuntimeError(exp.error or "model comparison failed")
    return {"experiment_id": exp.id, "status": exp.status}


def handle_portfolio_run(session: Session, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """payload: a portfolio run configuration (``qal.portfolio.service.PortfolioRunIn``)."""
    from qal.portfolio.service import PortfolioRunIn, run_portfolio

    exp = run_portfolio(session, PortfolioRunIn.model_validate(payload), actor=actor)
    session.commit()
    if exp.status == "failed":
        raise RuntimeError(exp.error or "portfolio run failed")
    return {"experiment_id": exp.id, "status": exp.status}


def handle_portfolio_proposals(session: Session, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    from qal.portfolio.agent import ProposalRequest, propose

    exp = propose(session, ProposalRequest.model_validate(payload))
    session.commit()
    return {"experiment_id": exp.id, "status": exp.status}


def handle_paper_run(session: Session, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """payload: {"session_id", "max_bars"}. Processes bars of a running paper session (simulation only).

    A schedule with this kind moves a new-data session forward after each ingestion."""
    from qal.paper import engine
    from qal.paper.service import get

    ps = get(session, payload["session_id"])
    out = engine.run(session, ps, int(payload.get("max_bars", 5000)))
    session.commit()
    return out


def handle_lab_experiment(session: Session, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """payload: {"experiment_id"}. Runs one Experiment Lab experiment; cancellable between units of work.

    Errors inside the pipeline end the experiment FAILED (recorded) rather than retrying the job: the pipeline is
    deterministic, so a retry would fail the same way. A finished experiment is returned as it is."""
    from qal.jobs.runner import stop_requested
    from qal.lab import service
    from qal.models import Experiment

    exp = session.get(Experiment, payload["experiment_id"])
    if exp is None or exp.kind != service.KIND:
        raise ValueError("unknown lab experiment")
    job_id = payload.get("_job_id")
    service.run(session, exp, store=ParquetStore(get_settings().data_dir), actor=actor,
                should_stop=(lambda: stop_requested(job_id)) if job_id else (lambda: False),
                checkpoint=session.commit)
    session.commit()
    return {"experiment_id": exp.id, "status": exp.status, "error": exp.error}


def handle_robustness_run(session: Session, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """payload: a ``RobustnessConfig``. Evaluates one completed lab experiment; cancellable between stages.

    Like lab experiments, a failure is recorded on the run (status failed) instead of retrying a deterministic job."""
    from qal.jobs.runner import stop_requested
    from qal.robustness import service

    job_id = payload.get("_job_id")
    cfg = service.RobustnessConfig.model_validate({k: v for k, v in payload.items() if not k.startswith("_")})
    exp = service.run(session, cfg, store=ParquetStore(get_settings().data_dir), actor=actor,
                      should_stop=(lambda: stop_requested(job_id)) if job_id else (lambda: False),
                      checkpoint=session.commit)
    session.commit()
    return {"experiment_id": exp.id, "status": exp.status, "state": (exp.results or {}).get("state"),
            "error": exp.error}


HANDLERS = {"ingest": handle_ingest, "scan": handle_scan, "ingest_and_scan": handle_ingest_and_scan,
            "validate": handle_validate, "alpha_cycle": handle_alpha_cycle,
            "model_experiment": handle_model_experiment, "portfolio_run": handle_portfolio_run,
            "portfolio_proposals": handle_portfolio_proposals, "paper_run": handle_paper_run,
            "lab_experiment": handle_lab_experiment, "robustness_run": handle_robustness_run}
__all__ = ["HANDLERS", "latest_dataset"]
