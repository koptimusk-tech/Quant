"""Hypothesis lifecycle: create (frozen contract), validate (walk-forward + gates), final test."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from qal import audit
from qal.data.store import ParquetStore
from qal.models import AnomalyDetection, Dataset, Experiment, GateDecision, Hypothesis
from qal.research.contract import HypothesisContract
from qal.research.costs import default_cost_model
from qal.research.policy import POLICY_VERSION, evaluate_gates
from qal.research.states import TransitionError, check_transition
from qal.research.validation import Validator
from qal.versioning import code_version, stable_hash


def transition(session: Session, hyp: Hypothesis, target: str, *, actor: str, actor_kind: str, reason: str) -> None:
    check_transition(hyp.status, target, actor_kind)
    audit.record(session, actor, "hypothesis_transition", "hypothesis", hyp.id, frm=hyp.status, to=target, reason=reason)
    hyp.status = target
    hyp.updated_at = datetime.now(UTC)


def create_hypothesis(session: Session, contract: HypothesisContract, *, actor: str = "api",
                      parent_id: str | None = None) -> Hypothesis:
    for ds_id in contract.dataset_ids:
        if session.get(Dataset, ds_id) is None:
            raise ValueError(f"unknown dataset {ds_id}")
    data = contract.model_dump(mode="json")
    parent = session.get(Hypothesis, parent_id) if parent_id else None
    if parent_id and parent is None:
        raise ValueError("unknown parent hypothesis")
    hyp = Hypothesis(title=contract.title, contract=data, contract_hash=stable_hash(data), created_by=actor,
                     parent_id=parent_id, version=(parent.version + 1) if parent else 1)
    hyp.family_id = parent.family_id if parent else hyp.id or ""
    session.add(hyp)
    session.flush()
    if not parent:
        hyp.family_id = hyp.id
    for aid in contract.source_anomaly_ids:
        a = session.get(AnomalyDetection, aid)
        if a is None:
            raise ValueError(f"unknown anomaly {aid}")
        a.validation_status = "HYPOTHESIS_FORMULATED"
    audit.record(session, actor, "hypothesis_created", "hypothesis", hyp.id, hash=hyp.contract_hash,
                 version=hyp.version, family=hyp.family_id)
    return hyp


def _validator(session: Session, hyp: Hypothesis) -> tuple[Validator, dict[str, str], list[dict[str, Any]]]:
    contract = HypothesisContract.model_validate(hyp.contract)
    frames, costs, names, quality, refs = [], [], [], {}, []
    asset_class = "other"
    for ds_id in contract.dataset_ids:
        ds = session.get(Dataset, ds_id)
        frames.append(ParquetStore.read(ds.path))
        asset_class = ds.instrument.asset_class
        costs.append(contract.costs.get(ds_id) or default_cost_model(ds.instrument.asset_class))
        names.append(ds.instrument.symbol)
        quality[ds.instrument.symbol] = ds.quality_status
        refs.append({"dataset_id": ds.id, "content_hash": ds.content_hash, "symbol": ds.instrument.symbol,
                     "is_synthetic": ds.is_synthetic, "quality_status": ds.quality_status})
    return Validator(contract, frames, asset_class, names, costs), quality, refs


def prior_trials(session: Session, family_id: str) -> int:
    rows = session.scalars(select(Experiment).join(Hypothesis, Experiment.hypothesis_id == Hypothesis.id)
                           .where(Hypothesis.family_id == family_id, Experiment.kind == "validation")).all()
    return sum(int((e.results or {}).get("variants", 0)) for e in rows)


def run_validation(session: Session, hyp: Hypothesis, *, actor: str = "api") -> Experiment:
    transition(session, hyp, "BACKTEST_PENDING", actor=actor, actor_kind="user", reason="validation requested")
    contract = HypothesisContract.model_validate(hyp.contract)
    n_prior = prior_trials(session, hyp.family_id)
    exp = Experiment(kind="validation", hypothesis_id=hyp.id, status="running",
                     algorithm_version=f"walk_forward@2.0.0+{contract.strategy.type}+policy@{POLICY_VERSION}",
                     code_version=code_version(), config={"contract_hash": hyp.contract_hash, "contract": hyp.contract},
                     config_hash=stable_hash({"c": hyp.contract_hash, "prior": n_prior}), data_refs=[],
                     seed=contract.validation.seed, n_tests=contract.strategy.n_variants())
    session.add(exp)
    session.flush()
    t0 = time.perf_counter()
    try:
        validator, quality, refs = _validator(session, hyp)
        exp.data_refs = refs
        report = validator.run(n_prior_trials=n_prior)
        gates = evaluate_gates(contract, report, quality)
        for g in gates:
            session.add(GateDecision(hypothesis_id=hyp.id, experiment_id=exp.id, gate=g.gate,
                                     decision="pass" if g.passed else "block", reasons=g.reasons,
                                     evidence=g.evidence, policy_version=POLICY_VERSION))
        passed = all(g.passed for g in gates)
        report["gates"] = [{"gate": g.gate, "passed": g.passed, "reasons": g.reasons} for g in gates]
        report["decision"] = "VALIDATION_PASSED" if passed else "VALIDATION_FAILED"
        exp.results = report
        exp.status = "succeeded"
        transition(session, hyp, report["decision"], actor="policy_engine", actor_kind="policy",
                   reason="all gates passed" if passed else
                   "blocked by gate(s) " + ",".join(g.gate for g in gates if not g.passed))
    except Exception as exc:
        exp.status = "failed"
        exp.error = f"{type(exc).__name__}: {exc}"
        exp.results = {"variants": contract.strategy.n_variants()}  # failed runs still count as trials
        transition(session, hyp, "HYPOTHESIS_FORMULATED", actor="policy_engine", actor_kind="policy",
                   reason=f"validation failed to run: {exp.error}")
    exp.results = {**(exp.results or {}), "duration_s": round(time.perf_counter() - t0, 3)}
    exp.finished_at = datetime.now(UTC)
    audit.record(session, actor, "validation_finished", "experiment", exp.id, status=exp.status,
                 decision=(exp.results or {}).get("decision"), error=exp.error)
    return exp


def run_final_test(session: Session, hyp: Hypothesis, *, actor: str = "api") -> Experiment:
    """Evaluate the isolated holdout once. Every access is counted; repeated access contaminates it."""
    if hyp.status not in ("VALIDATION_PASSED", "VALIDATION_FAILED"):
        raise TransitionError("run a validation before the final test")
    last = session.scalar(select(Experiment).where(Experiment.hypothesis_id == hyp.id, Experiment.kind == "validation",
                                                   Experiment.status == "succeeded")
                          .order_by(Experiment.created_at.desc()).limit(1))
    if last is None:
        raise TransitionError("no succeeded validation to take the selected parameters from")
    params = last.results["selected_params_last_window"]
    hyp.final_test_accesses += 1
    validator, _, refs = _validator(session, hyp)
    res = validator.final_test(params)
    res["access_number"] = hyp.final_test_accesses
    res["contaminated"] = hyp.final_test_accesses > 1
    res["note"] = ("The holdout has been examined before; it is no longer an independent test."
                   if res["contaminated"] else "First access to the holdout.")
    exp = Experiment(kind="final_test", hypothesis_id=hyp.id, status="succeeded",
                     algorithm_version=last.algorithm_version, code_version=code_version(),
                     config={"params": params, "validation_experiment": last.id}, config_hash=stable_hash(params),
                     data_refs=refs, seed=None, n_tests=1, results=res, finished_at=datetime.now(UTC))
    session.add(exp)
    session.flush()
    audit.record(session, actor, "final_test_accessed", "hypothesis", hyp.id, access=hyp.final_test_accesses,
                 experiment=exp.id)
    return exp
