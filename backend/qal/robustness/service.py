"""Robustness runs: configuration, execution, persistence and the candidate audit.

A run evaluates one COMPLETED Experiment Lab experiment and is stored as an ``Experiment`` of kind
``robustness_run``. Its identity is the hash of the configuration, the lab experiment, the content of every dataset
it reads and the engine version: the same request returns the stored run, and any change (a target asset, a capital
level, new data, a new version) is a new run next to the old one. Nothing is overwritten, no lab result, hypothesis,
paper session or risk policy is changed, and nothing here can place an order.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal import audit
from qal.data.store import ParquetStore
from qal.lab import selection
from qal.lab import service as lab
from qal.models import Dataset, Experiment
from qal.robustness import engine, report
from qal.versioning import code_version, stable_hash

KIND = "robustness_run"


class RobustnessError(ValueError):
    pass


class RobustnessConfig(BaseModel):
    experiment_id: str
    target_dataset_ids: list[str] | None = Field(None, description="Datasets for the cross-asset matrix; all "
                                                                   "compatible ones when omitted")
    max_targets: int = Field(12, ge=0, le=40)
    capital_levels: list[float] = Field(default_factory=lambda: [1e5, 3e5, 1e6, 3e6, 1e7, 3e7, 1e8])
    impact_coefficient: float = Field(1.0, gt=0, le=10, description="Y of the square-root impact model (assumption)")
    min_capacity: float = Field(1e6, gt=0)
    peer_experiment_ids: list[str] | None = Field(None, description="Lab experiments to combine with; by default "
                                                                    "the other completed, non-rejected ones")
    max_seconds: float = Field(1800, ge=10, le=6 * 3600)

    @field_validator("capital_levels")
    @classmethod
    def _levels(cls, v: list[float]) -> list[float]:
        if not v or len(v) > 8 or any(x <= 0 for x in v):
            raise ValueError("capital_levels: 1 to 8 positive values")
        return sorted(v)


def lab_decision(s: Session, exp: Experiment) -> tuple[dict[str, Any] | None, float | None]:
    """The candidate's decision with the current lab-wide Benjamini-Hochberg p (as the lab ranking shows it)."""
    rk = lab.ranking(s)
    row = next((r for r in rk["rows"] if r["experiment_id"] == exp.id), None)
    if row is None:
        return None, None
    d = selection.decide(exp.results["pipeline"], exp.config["spec"]["contract"], adjusted_p=row["adjusted_p"])
    return d, row["adjusted_p"]


def default_peers(s: Session, exp: Experiment, limit: int = 8) -> list[Experiment]:
    synth = any(r.get("is_synthetic") for r in exp.data_refs)
    rows = [r for r in lab.ranking(s)["rows"] if r["experiment_id"] != exp.id and r["decision"] != "REJECTED"
            and r["is_synthetic"] == synth]
    return [s.get(Experiment, r["experiment_id"]) for r in rows[:limit]]


def _identity(s: Session, cfg: RobustnessConfig, exp: Experiment, ctx_targets: list[Dataset]) -> str:
    return stable_hash({"config": cfg.model_dump(mode="json"), "lab": exp.config_hash,
                        "targets": sorted(d.content_hash for d in ctx_targets), "version": engine.ROBUSTNESS_VERSION})


def run(s: Session, cfg: RobustnessConfig, *, store: ParquetStore, actor: str = "api",
        should_stop: Callable[[], bool] = lambda: False, checkpoint: Callable[[], None] | None = None) -> Experiment:
    exp = s.get(Experiment, cfg.experiment_id)
    if exp is None or exp.kind != lab.KIND:
        raise RobustnessError("unknown lab experiment")
    if exp.status != "COMPLETED":
        raise RobustnessError(f"the lab experiment is {exp.status}: only COMPLETED experiments can be evaluated")
    t0 = time.perf_counter()

    def check() -> None:
        if should_stop():
            raise engine.Cancelled("cancelled")
        if time.perf_counter() - t0 > cfg.max_seconds:
            raise engine.Cancelled("time budget exhausted")

    ctx = None
    try:
        ctx = engine.build_context(s, exp, store)
        targets = engine.compatible_targets(s, ctx, cfg.target_dataset_ids, cfg.max_targets)
    except ValueError as exc:
        targets = []
        build_error = str(exc)
    else:
        build_error = None
    key = _identity(s, cfg, exp, targets)
    prev = s.scalars(select(Experiment).where(Experiment.kind == KIND, Experiment.config_hash == key)
                     .order_by(Experiment.created_at.desc())).first()
    if prev is not None and prev.status == "succeeded":
        audit.record(s, actor, "robustness_run_duplicate", "experiment", prev.id)
        return prev
    decision, adj_p = lab_decision(s, exp)
    run_exp = Experiment(kind=KIND, status="running", algorithm_version=engine.ROBUSTNESS_VERSION,
                         code_version=code_version(), config_hash=key, seed=exp.seed,
                         data_refs=list(exp.data_refs) + [{"dataset_id": d.id, "content_hash": d.content_hash,
                                                           "symbol": d.instrument.symbol, "role": "cross_asset_target"}
                                                          for d in targets],
                         config={**cfg.model_dump(mode="json"), "lab_title": exp.config.get("title"),
                                 "family": exp.config.get("family"), "strategy": exp.config["spec"]["contract"]["strategy"]["type"]},
                         results={})
    s.add(run_exp)
    s.flush()
    audit.record(s, actor, "robustness_run_started", "experiment", run_exp.id, lab_experiment=exp.id)
    if checkpoint:
        checkpoint()
    if build_error is not None or ctx is None:
        return _finish(s, run_exp, actor, t0, state="NOT_EVALUATED", reasons=[build_error or "could not rebuild"],
                       status="succeeded")
    try:
        res: dict[str, Any] = {"lab": {"experiment_id": exp.id, "title": exp.config.get("title"),
                                       "decision": decision["decision"] if decision else None, "adjusted_p": adj_p,
                                       "exploratory": not decision or selection.DECISIONS.index(decision["decision"])
                                       < selection.DECISIONS.index("OOS_SUPPORTED"),
                                       "universe": ctx.names, "asset_class": ctx.asset_class,
                                       "timeframe": ctx.datasets[0].frequency,
                                       "is_synthetic": any(d.is_synthetic for d in ctx.datasets)}}
        st = exp.results["pipeline"]["E_statistics"]
        res["lab_statistics"] = {k: st.get(k) for k in ("sharpe_ci95", "mean_net_ci95", "dsr", "n_trials", "pbo",
                                                         "pvalue_one_sided")}
        check()
        res["temporal"] = engine.temporal(ctx)
        check()
        res["cross_asset"] = engine.cross_asset(s, ctx, store, ids=cfg.target_dataset_ids, limit=cfg.max_targets,
                                                check=check)
        res["parameters"] = engine.parameters(ctx, check)
        check()
        res["execution"] = engine.execution(ctx)
        res["capacity"] = engine.capacity(ctx, cfg.capital_levels, cfg.impact_coefficient, cfg.min_capacity)
        check()
        res["stress"] = engine.stress(ctx, check)
        peers = [s.get(Experiment, i) for i in cfg.peer_experiment_ids] if cfg.peer_experiment_ids is not None \
            else default_peers(s, exp)
        peers = [p for p in peers if p is not None and p.kind == lab.KIND and p.status == "COMPLETED" and p.id != exp.id]
        res["diversification"] = engine.diversification(s, ctx, peers)
        dims = report.dimensions(res, exp.config["spec"]["contract"], decision, cfg.min_capacity)
        state, reasons = report.overall(dims)
        res["dimensions"] = dims
        res["criteria"] = report.CRITERIA
        res["limitations"] = _limitations(res)
        run_exp.results = res
        return _finish(s, run_exp, actor, t0, state=state, reasons=reasons, status="succeeded")
    except engine.Cancelled as exc:
        run_exp.error = str(exc)
        return _finish(s, run_exp, actor, t0, state="NOT_EVALUATED", reasons=[str(exc)], status="cancelled")
    except Exception as exc:  # recorded, never hidden
        run_exp.error = f"{type(exc).__name__}: {exc}"
        return _finish(s, run_exp, actor, t0, state="NOT_EVALUATED", reasons=[run_exp.error], status="failed")


def _finish(s: Session, e: Experiment, actor: str, t0: float, *, state: str, reasons: list[str], status: str) -> Experiment:
    e.results = {**(e.results or {}), "state": state, "state_reasons": reasons,
                 "duration_s": round(time.perf_counter() - t0, 3),
                 "note": "States describe the tests run under their stated assumptions; they are not a forecast and "
                         "they promote nothing."}
    e.status = status
    e.finished_at = datetime.now(UTC)
    audit.record(s, actor, "robustness_run_finished", "experiment", e.id, status=status, state=state)
    return e


def _limitations(res: dict[str, Any]) -> list[str]:
    out = []
    if res["lab"]["is_synthetic"]:
        out.append("Synthetic data: these results test the procedure, not markets.")
    if res["lab"]["exploratory"]:
        out.append(f"Exploratory: the lab decision is {res['lab']['decision']}, below OOS_SUPPORTED.")
    if res["cross_asset"].get("not_applicable"):
        out.append(res["cross_asset"]["not_applicable"])
    if res["capacity"]["status"] == "UNKNOWN":
        out.append("Capacity unknown: " + res["capacity"]["reason"])
    elif res["capacity"]["status"] == "APPROXIMATE":
        out.append("Capacity is approximate: observed bar volume with an assumed impact model.")
    out += [f"Not modelled: {x}" for x in res["execution"]["not_modelled"]]
    if any("default" in c for c in res["execution"]["cost_sources"]):
        out.append("Costs of at least one asset are asset-class defaults (estimates), not observed costs.")
    return out


def candidates(s: Session) -> list[dict[str, Any]]:
    """Audit of the lab candidates: decision, evidence level and their latest robustness state."""
    runs: dict[str, Experiment] = {}
    for r in s.scalars(select(Experiment).where(Experiment.kind == KIND).order_by(Experiment.created_at)).all():
        runs[r.config["experiment_id"]] = r
    out = []
    for row in lab.ranking(s)["rows"]:
        rr = runs.get(row["experiment_id"])
        exp = s.get(Experiment, row["experiment_id"])
        ds = [d for d in (s.get(Dataset, r.get("dataset_id")) for r in exp.data_refs) if d is not None]
        out.append({"asset_class": ds[0].instrument.asset_class if ds else None,
                    "markets": sorted({d.instrument.market or d.instrument.asset_class for d in ds}),
                    "timeframe": exp.data_refs[0].get("frequency") if exp.data_refs else None,
                    "period": [min(str(d.start_ts)[:10] for d in ds), max(str(d.end_ts)[:10] for d in ds)] if ds else None,
                    **{k: row[k] for k in ("experiment_id", "title", "family", "strategy", "universe", "is_synthetic",
                                            "decision", "adjusted_p", "oos_sharpe_net", "oos_bars", "n_trades")},
                    "exploratory": selection.DECISIONS.index(row["decision"]) < selection.DECISIONS.index("OOS_SUPPORTED"),
                    "robustness_run_id": rr.id if rr else None,
                    "robustness_state": (rr.results or {}).get("state") if rr else "NOT_EVALUATED"})
    return out


def summary_row(e: Experiment) -> dict[str, Any]:
    r = e.results or {}
    return {"id": e.id, "status": e.status, "created_at": e.created_at, "finished_at": e.finished_at,
            "experiment_id": e.config.get("experiment_id"), "title": e.config.get("lab_title"),
            "family": e.config.get("family"), "strategy": e.config.get("strategy"), "state": r.get("state"),
            "state_reasons": r.get("state_reasons", []), "duration_s": r.get("duration_s"), "error": e.error,
            "dimensions": {k: v["status"] for k, v in (r.get("dimensions") or {}).items()},
            **{k: (r.get("lab") or {}).get(k) for k in ("exploratory", "is_synthetic", "universe", "asset_class",
                                                          "timeframe", "decision")}}


__all__ = ["KIND", "RobustnessConfig", "RobustnessError", "candidates", "default_peers", "lab_decision", "run",
           "summary_row"]
