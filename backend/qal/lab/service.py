"""Deterministic planner of the Experiment Lab: create, run, cancel, resume, rank and record selections.

States (``Experiment.status`` of kind ``lab_experiment``): QUEUED -> RUNNING -> COMPLETED | FAILED | CANCELLED |
INVALID.

* Identity: the experiment is keyed by the hash of its spec, the content hashes of its datasets and the
  pipeline version. Submitting the same thing again returns the stored experiment (retries never duplicate a
  run); changing anything creates a new experiment and the old one stays as it was.
* INVALID: a critical integrity check failed (data quality, timestamps, temporal separation, leakage). No result
  of an INVALID experiment is used for selection.
* Cancellation is checked between units of work; a cancelled run keeps the stages it finished.
* Resume: the pipeline is a pure function of the stored spec and data snapshots, so an interrupted run (RUNNING
  with no live worker) is safely re-queued and re-run from the start; attempts are counted.
* Multiple testing: every variant of every earlier lab experiment on the same universe counts as a trial for the
  DSR (the trial count only grows), and the ranking applies Benjamini-Hochberg across all completed lab
  experiments, recomputed every time the ranking is read.
* The lab reads datasets and writes only its own experiment rows: no hypothesis status, paper session, risk
  policy or stored result is changed, and nothing here can place an order.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal import audit
from qal.anomaly.engine import benjamini_hochberg
from qal.data.store import ParquetStore
from qal.lab.pipeline import PIPELINE_VERSION, Cancelled, Pipeline
from qal.lab.selection import SELECTION_VERSION, decide, rank_key
from qal.lab.spec import ExperimentSpec
from qal.models import Dataset, Experiment
from qal.research.costs import default_cost_model
from qal.versioning import code_version, stable_hash

KIND = "lab_experiment"
STATES = ("QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "INVALID")
FINAL = ("COMPLETED", "FAILED", "CANCELLED", "INVALID")
REPEATABLE = ("FAILED", "CANCELLED")  # a new submission of the same spec may run again after these


class LabError(ValueError):
    pass


def _datasets(s: Session, spec: ExperimentSpec) -> list[Dataset]:
    out = []
    for ds_id in spec.contract.dataset_ids:
        ds = s.get(Dataset, ds_id)
        if ds is None:
            raise LabError(f"unknown dataset {ds_id}")
        out.append(ds)
    if len({bool(d.is_synthetic) for d in out}) > 1:
        raise LabError("refusing to mix synthetic and real datasets in one experiment")
    if len({d.frequency for d in out}) > 1:
        raise LabError("all datasets of an experiment must share one frequency")
    return out


def universe_key(datasets: list[Dataset]) -> str:
    return stable_hash(sorted(d.instrument_id for d in datasets) + [datasets[0].frequency])[:24]


def create(s: Session, spec: ExperimentSpec, *, actor: str = "api",
           generator_key: str | None = None) -> tuple[Experiment, bool]:
    """Register an experiment (QUEUED). Returns (experiment, created). Same spec + same data -> the stored one."""
    dss = _datasets(s, spec)
    spec_json = spec.model_dump(mode="json")
    refs = [{"dataset_id": d.id, "content_hash": d.content_hash, "symbol": d.instrument.symbol,
             "frequency": d.frequency, "is_synthetic": d.is_synthetic, "quality_status": d.quality_status,
             "rows": d.rows} for d in dss]
    key = stable_hash({"spec": spec_json, "data": [r["content_hash"] for r in refs], "pipeline": PIPELINE_VERSION})
    prev = s.scalars(select(Experiment).where(Experiment.kind == KIND, Experiment.config_hash == key)
                     .order_by(Experiment.created_at.desc())).first()
    if prev is not None and prev.status not in REPEATABLE:
        audit.record(s, actor, "lab_experiment_duplicate", "experiment", prev.id)
        return prev, False
    exp = Experiment(kind=KIND, status="QUEUED", algorithm_version=f"{PIPELINE_VERSION}+{spec.spec_version}",
                     code_version=code_version(), config_hash=key, data_refs=refs, seed=spec.contract.validation.seed,
                     n_tests=spec.contract.strategy.n_variants(),
                     config={"spec": spec_json, "spec_hash": stable_hash(spec_json), "universe_key": universe_key(dss),
                             "family": spec.family, "title": spec.contract.title, "generated_by": spec.generated_by,
                             "previous_attempt": prev.id if prev is not None else None,
                             "generator_key": generator_key},
                     results={"attempts": 0})
    s.add(exp)
    s.flush()
    audit.record(s, actor, "lab_experiment_created", "experiment", exp.id, hash=key, family=spec.family)
    return exp, True


def prior_trials(s: Session, exp: Experiment) -> int:
    """Variants of every other lab experiment on the same universe that got past the integrity checks."""
    rows = s.scalars(select(Experiment).where(Experiment.kind == KIND, Experiment.id != exp.id,
                                              Experiment.created_at <= exp.created_at)).all()
    uk = exp.config["universe_key"]
    return sum(int(e.n_tests or 0) for e in rows if (e.config or {}).get("universe_key") == uk
               and e.status in ("COMPLETED", "FAILED", "CANCELLED", "RUNNING"))


def holdout_accesses(s: Session, exp: Experiment) -> int:
    hs = exp.config["spec"]["contract"]["validation"]["holdout_start"]
    rows = s.scalars(select(Experiment).where(Experiment.kind == KIND, Experiment.id != exp.id,
                                              Experiment.status == "COMPLETED")).all()
    return sum(1 for e in rows if (e.config or {}).get("universe_key") == exp.config["universe_key"]
               and e.config["spec"]["contract"]["validation"]["holdout_start"] == hs
               and (e.results or {}).get("pipeline", {}).get("C_temporal", {}).get("holdout") is not None)


def run(s: Session, exp: Experiment, *, store: ParquetStore, actor: str = "api",
        should_stop: Callable[[], bool] = lambda: False, checkpoint: Callable[[], None] | None = None) -> Experiment:
    if exp.status in FINAL:
        return exp  # never re-run a finished experiment in place: submit a new one
    spec = ExperimentSpec.model_validate(exp.config["spec"])
    attempts = int((exp.results or {}).get("attempts", 0)) + 1
    exp.status, exp.results = "RUNNING", {"attempts": attempts, "started_at": datetime.now(UTC).isoformat()}
    audit.record(s, actor, "lab_experiment_started", "experiment", exp.id, attempt=attempts)
    if checkpoint:
        checkpoint()
    t0 = time.perf_counter()

    def check() -> None:
        if should_stop():
            raise Cancelled("cancelled")
        if time.perf_counter() - t0 > spec.limits.max_seconds:
            raise Cancelled("time budget exhausted")

    def progress(stage: str) -> None:
        exp.results = {**exp.results, "stage": stage}
        if checkpoint:
            checkpoint()

    try:
        dss = _datasets(s, spec)
        for d, ref in zip(dss, exp.data_refs, strict=True):
            if d.content_hash != ref["content_hash"]:
                raise LabError(f"dataset {d.id} changed since the experiment was created")
        frames = [store.read(d.path) for d in dss]
        if any(len(f) > spec.limits.max_bars for f in frames):
            raise LabError(f"a dataset has more than {spec.limits.max_bars} bars (resource limit)")
        costs = [spec.contract.costs.get(d.id) or default_cost_model(d.instrument.asset_class) for d in dss]
        quality = {d.instrument.symbol: d.quality_status for d in dss}
        pipe = Pipeline(spec, frames, [d.instrument.symbol for d in dss], dss[0].instrument.asset_class, costs, quality,
                        check=check, progress=progress)
        n_prior = prior_trials(s, exp)
        res = pipe.run(n_prior_trials=n_prior, holdout_accesses_before=holdout_accesses(s, exp))
        res["n_prior_trials"] = n_prior
        if res.get("invalid"):
            exp.status = "INVALID"
            exp.error = "critical integrity check failed: " + ", ".join(res["invalid"])
        else:
            exp.status = "COMPLETED"
            res["decision_at_completion"] = decide(res, exp.config["spec"]["contract"],
                                                   adjusted_p=res["E_statistics"].get("pvalue_one_sided"))
            res["decision_at_completion"]["note_on_p"] = ("At completion only this experiment's own p-value was "
                                                          "known; the ranking applies the lab-wide correction.")
        exp.results = {"attempts": attempts, "pipeline": res}
    except Cancelled as exc:
        exp.status = "CANCELLED"
        exp.error = str(exc)
        exp.results = {"attempts": attempts, "stopped": str(exc)}
    except Exception as exc:  # recorded, never hidden
        exp.status = "FAILED"
        exp.error = f"{type(exc).__name__}: {exc}"
        exp.results = {"attempts": attempts}
    exp.results = {**exp.results, "duration_s": round(time.perf_counter() - t0, 3)}
    exp.finished_at = datetime.now(UTC)
    audit.record(s, actor, "lab_experiment_finished", "experiment", exp.id, status=exp.status, error=exp.error)
    return exp


def cancel(s: Session, exp: Experiment, *, actor: str = "api") -> Experiment:
    if exp.status == "QUEUED":
        exp.status, exp.error, exp.finished_at = "CANCELLED", "cancelled before it started", datetime.now(UTC)
        audit.record(s, actor, "lab_experiment_cancelled", "experiment", exp.id)
    elif exp.status != "RUNNING":
        raise LabError(f"experiment is {exp.status}; only QUEUED or RUNNING experiments can be cancelled")
    return exp


def resume_interrupted(s: Session, *, actor: str = "system") -> list[str]:
    """RUNNING experiments with no live worker (after a crash or restart) go back to QUEUED. Safe because the
    pipeline is deterministic and writes only at the end; attempts keep counting."""
    ids = []
    for e in s.scalars(select(Experiment).where(Experiment.kind == KIND, Experiment.status == "RUNNING")).all():
        e.status = "QUEUED"
        ids.append(e.id)
        audit.record(s, actor, "lab_experiment_requeued", "experiment", e.id)
    return ids


# -- background execution -------------------------------------------------------------------------------
JOB_KIND = "lab_experiment"


def submit(s: Session, exp: Experiment, *, actor: str = "api") -> Any:
    """Queue the experiment on the job runner. One job per experiment attempt: retrying the submission returns
    the same job, and the job runs the pipeline at most once (a finished experiment is never re-run in place)."""
    from qal.jobs.runner import submit_job

    attempt = int((exp.results or {}).get("attempts", 0))
    return submit_job(s, JOB_KIND, {"experiment_id": exp.id}, idempotency_key=f"lab:{exp.id}:{attempt}", actor=actor)


def active_job(s: Session, exp: Experiment) -> Any:
    from qal.models import Job

    jobs = s.scalars(select(Job).where(Job.kind == JOB_KIND, Job.status.in_(("queued", "running", "cancelling")))
                     ).all()
    return next((j for j in jobs if (j.payload or {}).get("experiment_id") == exp.id), None)


def request_cancel(s: Session, exp: Experiment, *, actor: str = "api") -> Experiment:
    """QUEUED -> CANCELLED at once; RUNNING -> its job stops between units of work and the run ends CANCELLED."""
    from qal.jobs.runner import cancel_job

    job = active_job(s, exp)
    if exp.status == "RUNNING":
        if job is None:
            raise LabError("this run is not on the job runner (synchronous run); it cannot be stopped from here")
        cancel_job(s, job)
        audit.record(s, actor, "lab_experiment_cancel_requested", "experiment", exp.id)
        return exp
    if job is not None and job.status == "queued":
        cancel_job(s, job)
    return cancel(s, exp, actor=actor)


def enqueue_orphans(s: Session, *, actor: str = "system") -> list[str]:
    """After a restart: re-queue interrupted runs and give every QUEUED experiment without a live job a job."""
    resume_interrupted(s, actor=actor)
    out = []
    for e in s.scalars(select(Experiment).where(Experiment.kind == KIND, Experiment.status == "QUEUED")).all():
        if active_job(s, e) is None:
            submit(s, e, actor=actor)
            out.append(e.id)
    return out


def lineage(s: Session, exp: Experiment) -> list[dict[str, Any]]:
    """Version history: every lab experiment of the same family, strategy and universe, oldest first, with what
    changed in its spec. Nothing is overwritten: a new parameter, cost or code version is a new row."""
    rows = s.scalars(select(Experiment).where(Experiment.kind == KIND).order_by(Experiment.created_at)).all()
    st = exp.config["spec"]["contract"]["strategy"]["type"]
    same = [e for e in rows if e.config.get("universe_key") == exp.config.get("universe_key")
            and e.config.get("family") == exp.config.get("family")
            and e.config["spec"]["contract"]["strategy"]["type"] == st]
    out, prev = [], None
    for e in same:
        flat = _flatten(e.config["spec"])
        changed = sorted(k for k in set(flat) | set(prev or {}) if prev is not None and flat.get(k) != prev.get(k))
        d = ((e.results or {}).get("pipeline") or {}).get("decision_at_completion") or {}
        out.append({"id": e.id, "created_at": str(e.created_at), "status": e.status, "spec_hash": e.config.get(
            "spec_hash"), "code_version": e.code_version, "algorithm_version": e.algorithm_version,
            "decision_at_completion": d.get("decision"), "changed_from_previous": changed[:40],
            "previous_attempt": e.config.get("previous_attempt"), "current": e.id == exp.id})
        prev = flat
    return out


def _flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(d, dict):
        out: dict[str, Any] = {}
        for k, v in d.items():
            out.update(_flatten(v, f"{prefix}{k}."))
        return out
    return {prefix[:-1]: d if not isinstance(d, list) else str(d)}


# -- ranking -----------------------------------------------------------------------------------------------
def ranking(s: Session, *, include_synthetic: bool = True) -> dict[str, Any]:
    rows = s.scalars(select(Experiment).where(Experiment.kind == KIND, Experiment.status == "COMPLETED")
                     .order_by(Experiment.created_at)).all()
    if not include_synthetic:
        rows = [e for e in rows if not any(r.get("is_synthetic") for r in e.data_refs)]
    p = np.array([((e.results or {}).get("pipeline", {}).get("E_statistics", {}).get("pvalue_one_sided") or 1.0)
                  for e in rows], dtype=float)
    adj = benjamini_hochberg(p) if len(p) else p
    out = []
    for e, a in zip(rows, adj, strict=True):
        res = e.results["pipeline"]
        d = decide(res, e.config["spec"]["contract"], adjusted_p=float(a))
        st = res["E_statistics"]
        out.append({"experiment_id": e.id, "title": e.config.get("title"), "family": e.config.get("family"),
                    "strategy": e.config["spec"]["contract"]["strategy"]["type"],
                    "universe": [r["symbol"] for r in e.data_refs],
                    "is_synthetic": any(r.get("is_synthetic") for r in e.data_refs),
                    "decision": d["decision"], "score": d["score"], "reasons": d["reasons"], "warnings": d["warnings"],
                    "components": d["components"], "adjusted_p": float(a), "raw_p": st.get("pvalue_one_sided"),
                    "oos_sharpe_net": st["oos"].get("sharpe_net"), "oos_mean_net": st["oos"].get("mean_net"),
                    "oos_bars": st["oos"].get("bars"), "n_trades": st["oos"].get("n_trades"), "dsr": st.get("dsr"),
                    "n_trials": st.get("n_trials"), "variants": e.n_tests,
                    "baselines_beaten": [b["baseline"] for b in res["B_baselines"] if b.get("beaten")],
                    "created_at": str(e.created_at)})
    out.sort(key=rank_key)
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return {"version": SELECTION_VERSION, "n_experiments": len(out), "n_trials_total": int(sum(e.n_tests for e in rows)),
            "multiple_testing": f"Benjamini-Hochberg over the out-of-sample p-values of {len(out)} completed "
                                "lab experiments", "rows": out,
            "note": "Rank orders the evidence; it is not a forecast. Open an experiment for every metric behind it."}


def record_selection(s: Session, *, actor: str, note: str = "") -> Experiment:
    """Freeze the current ranking as an audited, versioned record (kind ``lab_selection``)."""
    r = ranking(s)
    snap = {"ranking": r, "note": note}
    exp = Experiment(kind="lab_selection", status="succeeded", algorithm_version=SELECTION_VERSION,
                     code_version=code_version(), config={"recorded_by": actor, "note": note},
                     config_hash=stable_hash(snap), data_refs=[{"experiment_id": x["experiment_id"]} for x in r["rows"]],
                     n_tests=r["n_trials_total"], results=r, finished_at=datetime.now(UTC))
    s.add(exp)
    s.flush()
    audit.record(s, actor, "lab_selection_recorded", "experiment", exp.id, n=r["n_experiments"])
    return exp


# -- portfolio contribution ----------------------------------------------------------------------------
def portfolio_contribution(s: Session, ids: list[str]) -> dict[str, Any]:
    """Does each candidate add something to the others? Daily OOS series, correlations (all and in the worst
    days), common factor exposure, drawdown and costs of the equal-weight combination vs each candidate and cash."""
    from qal.portfolio.risk import correlations, covariance, factor_exposure, marginal_analysis

    exps = [s.get(Experiment, i) for i in ids]
    if any(e is None or e.kind != KIND or e.status != "COMPLETED" for e in exps):
        raise LabError("every id must be a COMPLETED lab experiment")
    if len(exps) < 2:
        raise LabError("select at least two experiments")
    cols = {}
    for e in exps:
        ser = e.results["pipeline"]["oos_series"]
        r = pd.Series(ser["net"], index=pd.to_datetime(ser["ts"], utc=True).normalize())
        cols[e.id] = (1 + r).groupby(level=0).prod() - 1
    R = pd.DataFrame(cols).dropna(how="all").fillna(0.0)
    common = R[(R != 0).any(axis=1)]
    if len(common) < 60:
        return {"not_applicable": f"only {len(common)} days with out-of-sample returns in common (minimum 60)"}
    C = covariance(R, "ledoit_wolf")
    w = np.ones(len(cols)) / len(cols)
    combo = R @ w

    def stats_(r: pd.Series) -> dict[str, Any]:
        eq = (1 + r).cumprod()
        sd = r.std(ddof=1)
        return {"mean_daily": float(r.mean()), "sharpe_ann": float(r.mean() / sd * np.sqrt(252)) if sd > 0 else None,
                "max_drawdown": float((eq / eq.cummax() - 1).min()), "total": float(eq.iloc[-1] - 1)}

    corr = correlations(R)
    # Loss diversification: on each candidate's worst 5% of days, how much did the combination lose?
    tail = {}
    for i in ids:
        cut = R[i].quantile(0.05)
        worst = R.index[R[i] <= cut]
        tail[i] = {"days": int(len(worst)), "candidate_mean": float(R.loc[worst, i].mean()),
                   "combined_mean": float(combo.loc[worst].mean()),
                   "others_also_lost_frac": float((R.loc[worst, [j for j in ids if j != i]] < 0).all(axis=1).mean())}
    costs = {e.id: {k: e.results["pipeline"]["E_statistics"]["oos"].get(k)
                    for k in ("turnover_ann", "total_cost", "total_return_gross", "total_return_net", "exposure_avg")}
             for e in exps}
    contrib = {i: float((w[k] * R[i]).sum() / combo.sum()) if combo.sum() != 0 else None for k, i in enumerate(ids)}
    return {"days": int(len(R)), "strategies": ids, "weights": "equal", "loss_diversification": tail,
            "costs_and_turnover": costs, "return_contribution_share": contrib,
            "individual": {i: stats_(R[i]) for i in ids}, "combined": stats_(combo),
            "baseline_cash": {"mean_daily": 0.0, "sharpe_ann": None, "max_drawdown": 0.0, "total": 0.0},
            "correlation": corr, "marginal": marginal_analysis(R, C, w), "factor_exposure": factor_exposure(R),
            "note": "Correlations of out-of-sample returns, also on the worst days; two strategies with different names "
                    "or parameters are not diversified if these numbers say otherwise. Nothing here changes a paper "
                    "session's allocation."}


__all__ = ["FINAL", "JOB_KIND", "KIND", "STATES", "LabError", "active_job", "cancel", "create", "enqueue_orphans",
           "lineage", "portfolio_contribution", "prior_trials", "ranking", "record_selection", "request_cancel",
           "resume_interrupted", "run", "submit"]
