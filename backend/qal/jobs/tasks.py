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
        run_experiments=payload.get("run_experiments", True))
    session.commit()
    return {"run_id": run.id, **{k: v for k, v in run.results.items() if k.startswith("n_")}}


HANDLERS = {"ingest": handle_ingest, "scan": handle_scan, "ingest_and_scan": handle_ingest_and_scan,
            "validate": handle_validate, "alpha_cycle": handle_alpha_cycle}
__all__ = ["HANDLERS", "latest_dataset"]
