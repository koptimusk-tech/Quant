"""Formal candidate registry of the paper trading validation program.

A candidate is a frozen snapshot of one Experiment Lab experiment: hypothesis, assets, timeframe, code version,
the parameters selected in the last walk-forward window, features, data (content hashes), historical,
out-of-sample and robustness results, baselines, costs, risk limits and the admission (who, when, why, which
checks passed). The snapshot is hashed and stored as an ``Experiment`` of kind ``paper_candidate``; nothing ever
edits it. ``verify`` recomputes the hash, and a candidate whose record no longer matches cannot trade.

Versions: candidates share a ``name``; a different snapshot under the same name is the next version. Results are
always kept per version (per candidate id) and never pooled across versions.

Admission rules (``eligibility``):

* always required: the lab experiment COMPLETED, its datasets unchanged since it ran, no dataset with failed
  quality, a named person (never an agent) and a written justification;
* evidence requirements: lab decision OOS_SUPPORTED or PAPER_ELIGIBLE and a succeeded robustness run of that
  experiment in state ROBUST_UNDER_TESTED_ASSUMPTIONS. A candidate that misses them is refused, unless the person
  records an explicit *exploratory* admission; it is then labelled exploratory everywhere and its results are
  never presented as validation evidence. A good backtest alone never admits a candidate.

A refused admission is stored too (status ``refused``) so the registry shows every attempt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal import audit
from qal.lab import selection
from qal.lab import service as lab
from qal.models import Dataset, Experiment
from qal.research.costs import default_cost_model
from qal.versioning import code_version, stable_hash

KIND = "paper_candidate"
REGISTRY_VERSION = "paper-registry@1.0.0"
VALIDATED, EXPLORATORY = "PAPER_CANDIDATE", "PAPER_CANDIDATE_EXPLORATORY"
CANDIDATE_STATUSES = (VALIDATED, EXPLORATORY)
ROBUST = "ROBUST_UNDER_TESTED_ASSUMPTIONS"


class RegistryError(ValueError):
    pass


def is_agent(actor: str) -> bool:
    a = actor.strip().lower()
    return a.startswith(("agent", "llm")) or a in ("system", "api", "auto")


class RiskLimits(BaseModel):
    """Limits of the candidate in paper trading. They can only tighten the active risk policy, never relax it."""

    drawdown_pause: float = Field(0.10, gt=0, lt=1, description="Drawdown that pauses the program for review")
    drawdown_stop: float = Field(0.20, gt=0, lt=1, description="Drawdown that stops it (reject criterion)")
    max_gross_exposure: float = Field(1.0, gt=0, le=4, description="Gross exposure / equity that pauses it")
    daily_loss_pause: float = Field(0.05, gt=0, lt=1, description="One-bar loss that pauses it")

    @model_validator(mode="after")
    def _check(self) -> RiskLimits:
        if self.drawdown_pause >= self.drawdown_stop:
            raise ValueError("drawdown_pause must be smaller than drawdown_stop")
        return self


class AdmissionIn(BaseModel):
    name: str = Field(min_length=3, max_length=96, description="Candidate line; versions share it")
    lab_experiment_id: str
    robustness_run_id: str | None = Field(None, description="The latest succeeded run of the experiment by default")
    admitted_by: str = Field(min_length=2, max_length=64)
    justification: str = Field(min_length=30, max_length=4000)
    exploratory: bool = Field(False, description="Explicit decision to admit a candidate that misses the evidence "
                                                 "requirements; it stays labelled exploratory")
    allocation: float = Field(0.3, gt=0, le=1)
    risk_limits: RiskLimits = Field(default_factory=RiskLimits)


def _check(name: str, passed: bool, detail: str, *, blocking: bool) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail, "blocking": blocking}


def latest_robustness(s: Session, lab_id: str) -> Experiment | None:
    rows = s.scalars(select(Experiment).where(Experiment.kind == "robustness_run", Experiment.status == "succeeded")
                     .order_by(Experiment.created_at.desc())).all()
    return next((r for r in rows if (r.config or {}).get("experiment_id") == lab_id), None)


def eligibility(s: Session, exp: Experiment | None, rob: Experiment | None, actor: str = "",
                justification: str = "") -> dict[str, Any]:
    """Every admission check with its evidence. ``always`` checks block any admission; ``evidence`` checks block
    a validated admission and are recorded as unmet for an exploratory one."""
    always, evidence = [], []
    if exp is None or exp.kind != lab.KIND:
        return {"always": [_check("lab_experiment", False, "unknown lab experiment", blocking=True)], "evidence": [],
                "decision": None, "adjusted_p": None}
    always.append(_check("lab_completed", exp.status == "COMPLETED", f"lab experiment is {exp.status}", blocking=True))
    changed, bad_quality = [], []
    for ref in exp.data_refs:
        d = s.get(Dataset, ref["dataset_id"])
        if d is None or d.content_hash != ref["content_hash"]:
            changed.append(ref.get("symbol", ref["dataset_id"]))
        elif d.quality_status == "fail":
            bad_quality.append(d.instrument.symbol)
    always.append(_check("data_unchanged", not changed, f"changed or missing: {changed}" if changed else
                         "every dataset has the content hash the experiment used", blocking=True))
    always.append(_check("data_quality", not bad_quality, f"failed quality: {bad_quality}" if bad_quality else
                         "no dataset with failed quality", blocking=True))
    if actor:
        always.append(_check("named_person", not is_agent(actor), f"admitted by {actor!r}" + (
            ": agents and automated processes cannot admit candidates" if is_agent(actor) else ""), blocking=True))
    if justification is not None and actor:
        always.append(_check("justification", len(justification.strip()) >= 30, "written justification recorded",
                             blocking=True))
    decision = adj_p = None
    if exp.status == "COMPLETED":
        from qal.robustness.service import lab_decision

        d, adj_p = lab_decision(s, exp)
        decision = d["decision"] if d else None
    ok_decision = decision is not None and selection.TIER[decision] >= selection.TIER["OOS_SUPPORTED"]
    evidence.append(_check("lab_decision", ok_decision, f"lab decision {decision} (needs OOS_SUPPORTED or "
                           "PAPER_ELIGIBLE)", blocking=True))
    if rob is None:
        evidence.append(_check("robustness", False, "no succeeded robustness run of this experiment", blocking=True))
    else:
        st = (rob.results or {}).get("state")
        same = (rob.config or {}).get("experiment_id") == exp.id
        evidence.append(_check("robustness", same and st == ROBUST, f"robustness run {rob.id}: {st}"
                               + ("" if same else " (belongs to another experiment)"), blocking=True))
    return {"always": always, "evidence": evidence, "decision": decision, "adjusted_p": adj_p}


def _snapshot(s: Session, exp: Experiment, rob: Experiment | None, body: AdmissionIn, elig: dict[str, Any],
              kind: str) -> dict[str, Any]:
    spec = exp.config["spec"]
    c = spec["contract"]
    p = exp.results["pipeline"]
    dss = [s.get(Dataset, r["dataset_id"]) for r in exp.data_refs]
    assets = [{"dataset_id": d.id, "symbol": d.instrument.symbol, "content_hash": d.content_hash,
               "provider": d.provider_id, "asset_class": d.instrument.asset_class, "frequency": d.frequency,
               "is_synthetic": bool(d.is_synthetic), "start": str(d.start_ts), "end": str(d.end_ts)} for d in dss]
    costs = {}
    for d in dss:
        if d.id in c.get("costs", {}):
            costs[d.id] = {**c["costs"][d.id], "source": "contract (configured estimate, not observed)"}
        else:
            costs[d.id] = {**default_cost_model(d.instrument.asset_class).model_dump(),
                           "source": "asset-class default (estimate, not observed)"}
    params = {**c["strategy"].get("params", {}), **p["C_temporal"]["selected_params_last_window"]}
    rob_part = None
    if rob is not None:
        rr = rob.results or {}
        rob_part = {"run_id": rob.id, "version": rob.algorithm_version, "state": rr.get("state"),
                    "state_reasons": rr.get("state_reasons"),
                    "dimensions": {k: v.get("status") for k, v in (rr.get("dimensions") or {}).items()}}
    synthetic = any(a["is_synthetic"] for a in assets)
    return {
        "registry_version": REGISTRY_VERSION, "name": body.name, "status": kind,
        "hypothesis": {k: c.get(k) for k in ("title", "premise", "mechanism", "entry_rule", "exit_rule",
                                             "invalidation", "risks", "horizon_bars", "expected_direction")},
        "family": spec["family"], "features": c.get("features", []),
        "strategy": {"type": c["strategy"]["type"], "params": params, "param_grid": c["strategy"].get("param_grid", {}),
                     "selection": "parameters selected in the last walk-forward window; nothing is refitted in paper"},
        "assets": assets, "timeframe": assets[0]["frequency"], "is_synthetic": synthetic,
        "allowed_modes": ["C"] if synthetic else ["A", "B"],
        "code_version": {"lab_experiment": exp.code_version, "lab_pipeline": exp.algorithm_version,
                         "registry": code_version()},
        "lab": {"experiment_id": exp.id, "config_hash": exp.config_hash, "decision": elig["decision"],
                "adjusted_p": elig["adjusted_p"]},
        "results": {"oos": p["E_statistics"]["oos"], "sharpe_ci95": p["E_statistics"].get("sharpe_ci95"),
                    "mean_net_ci95": p["E_statistics"].get("mean_net_ci95"), "dsr": p["E_statistics"].get("dsr"),
                    "pbo": p["E_statistics"].get("pbo"), "n_trials": p["E_statistics"].get("n_trials"),
                    "oos_bars": p["C_temporal"].get("oos_bars"), "research_bars": p["C_temporal"].get("research_bars"),
                    "holdout_start": p["C_temporal"].get("holdout_start"),
                    "holdout_evaluated": p["C_temporal"].get("holdout") is not None,
                    "holdout": {k: v for k, v in (p["C_temporal"].get("holdout") or {}).items() if k != "equity"} or None},
        "robustness": rob_part,
        "baselines": [{k: b.get(k) for k in ("baseline", "how", "metrics", "diff_mean_net", "diff_ci95", "beaten",
                                            "placebo_pvalue")} for b in p["B_baselines"]],
        "costs": costs, "risk_limits": body.risk_limits.model_dump(), "allocation": body.allocation,
        "admission": {"kind": "exploratory" if kind == EXPLORATORY else "validated", "by": body.admitted_by,
                      "at": datetime.now(UTC).isoformat(), "justification": body.justification,
                      "checks": elig["always"] + elig["evidence"]},
    }


def _identity(snap: dict[str, Any]) -> str:
    """What makes two admissions the same candidate version: everything but the admission metadata."""
    return stable_hash({k: v for k, v in snap.items() if k not in ("admission", "version", "code_version")}
                       | {"code": snap["code_version"]["lab_experiment"]})


def admit(s: Session, body: AdmissionIn) -> Experiment:
    exp = s.get(Experiment, body.lab_experiment_id)
    rob = s.get(Experiment, body.robustness_run_id) if body.robustness_run_id else (
        latest_robustness(s, body.lab_experiment_id) if exp is not None else None)
    if body.robustness_run_id and (rob is None or rob.kind != "robustness_run"):
        raise RegistryError("robustness run not found")
    elig = eligibility(s, exp, rob, body.admitted_by, body.justification)
    blocking = [c for c in elig["always"] if not c["passed"]]
    unmet = [c for c in elig["evidence"] if not c["passed"]]
    if blocking or exp is None or exp.status != "COMPLETED":
        refused = blocking or [_check("lab_completed", False, "not completed", blocking=True)]
        _store_refusal(s, body, elig, [c["detail"] for c in refused])
        raise RegistryError("admission refused: " + "; ".join(c["detail"] for c in refused))
    if unmet and not body.exploratory:
        _store_refusal(s, body, elig, [c["detail"] for c in unmet])
        raise RegistryError("admission refused, evidence requirements not met: " + "; ".join(c["detail"] for c in unmet)
                            + ". A person may record an explicit exploratory admission instead; it is never "
                              "presented as validated.")
    kind = EXPLORATORY if unmet else VALIDATED
    snap = _snapshot(s, exp, rob, body, elig, kind)
    ident = _identity(snap)
    for prev in versions(s, body.name):
        if prev.results.get("identity") == ident:
            audit.record(s, body.admitted_by, "paper_candidate_duplicate", "experiment", prev.id)
            return prev
    snap["version"] = len(versions(s, body.name)) + 1
    rec = Experiment(kind=KIND, status="admitted", algorithm_version=REGISTRY_VERSION, code_version=code_version(),
                     config=snap, config_hash=stable_hash(snap), data_refs=list(exp.data_refs),
                     results={"identity": ident, "name": body.name})
    s.add(rec)
    s.flush()
    audit.record(s, body.admitted_by, "paper_candidate_admitted", "experiment", rec.id, kind=kind,
                 version=snap["version"], lab_experiment=exp.id, unmet=[c["check"] for c in unmet])
    return rec


def _store_refusal(s: Session, body: AdmissionIn, elig: dict[str, Any], reasons: list[str]) -> None:
    cfg = {"registry_version": REGISTRY_VERSION, "name": body.name, "lab_experiment_id": body.lab_experiment_id,
           "robustness_run_id": body.robustness_run_id, "requested_by": body.admitted_by,
           "requested_exploratory": body.exploratory, "justification": body.justification,
           "checks": elig["always"] + elig["evidence"], "reasons": reasons, "at": datetime.now(UTC).isoformat()}
    s.add(Experiment(kind=KIND, status="refused", algorithm_version=REGISTRY_VERSION, code_version=code_version(),
                     config=cfg, config_hash=stable_hash(cfg), results={"name": body.name}))
    s.flush()
    audit.record(s, body.admitted_by, "paper_candidate_refused", "experiment", None, reasons=reasons)


def versions(s: Session, name: str) -> list[Experiment]:
    rows = s.scalars(select(Experiment).where(Experiment.kind == KIND, Experiment.status == "admitted")
                     .order_by(Experiment.created_at)).all()
    return [e for e in rows if (e.results or {}).get("name") == name]


def verify(rec: Experiment) -> bool:
    """True when the stored snapshot still matches the hash computed when it was admitted."""
    return rec.kind == KIND and rec.status == "admitted" and stable_hash(rec.config) == rec.config_hash


def get(s: Session, cid: str) -> Experiment:
    rec = s.get(Experiment, cid)
    if rec is None or rec.kind != KIND:
        raise LookupError("candidate not found")
    return rec


def admission_status(rec: Experiment) -> str:
    """Status the risk gate sees: the admission kind, or UNKNOWN when the record was altered."""
    return rec.config["status"] if verify(rec) else "UNKNOWN"


def summary(rec: Experiment) -> dict[str, Any]:
    c = rec.config
    if rec.status != "admitted":
        return {"id": rec.id, "status": rec.status, "name": c.get("name"), "created_at": str(rec.created_at),
                "lab_experiment_id": c.get("lab_experiment_id"), "reasons": c.get("reasons"),
                "requested_by": c.get("requested_by"), "checks": c.get("checks")}
    return {"id": rec.id, "status": rec.status, "name": c["name"], "version": c["version"],
            "admission_kind": c["admission"]["kind"], "admitted_by": c["admission"]["by"],
            "admitted_at": c["admission"]["at"], "created_at": str(rec.created_at), "family": c["family"],
            "strategy": c["strategy"]["type"], "params": c["strategy"]["params"],
            "universe": [a["symbol"] for a in c["assets"]], "timeframe": c["timeframe"],
            "is_synthetic": c["is_synthetic"], "allowed_modes": c["allowed_modes"],
            "lab_experiment_id": c["lab"]["experiment_id"], "lab_decision": c["lab"]["decision"],
            "robustness_state": (c["robustness"] or {}).get("state"), "integrity_ok": verify(rec),
            "record_hash": rec.config_hash}
