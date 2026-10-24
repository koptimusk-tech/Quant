"""Read models for the Alpha Research views: runs, hypotheses and the evidence behind each decision.

Three things are kept apart on purpose:

* research priority: the researcher's heuristic ordering (why it was tested first), never evidence;
* predictive evidence: what the walk-forward validation and the policy gates found out of sample;
* validated net profitability: only after the single holdout test, with explicit costs (``promotion``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.alpha.agents import idea_key
from qal.models import AgentMessage, Dataset, Experiment, GateDecision, Hypothesis
from qal.research.promotion import eligibility

METRICS = ("total_return_gross", "total_return_net", "total_cost", "sharpe_gross", "sharpe_net", "sortino_net",
           "max_drawdown", "exposure_avg", "n_trades", "hit_rate", "mean_net", "turnover_ann")


def run_summary(e: Experiment) -> dict[str, Any]:
    return {"id": e.id, "status": e.status, "created_at": e.created_at, "finished_at": e.finished_at,
            "error": e.error, "budget": (e.config or {}).get("budget"), **(e.results or {})}


def run_detail(session: Session, run: Experiment) -> dict[str, Any]:
    msgs = session.scalars(select(AgentMessage).where(AgentMessage.run_id == run.id).order_by(AgentMessage.seq)).all()
    return {"run": run_summary(run), "messages": [message_out(m) for m in msgs]}


def message_out(m: AgentMessage) -> dict[str, Any]:
    return {"id": m.id, "run_id": m.run_id, "seq": m.seq, "sender": m.sender, "receiver": m.receiver,
            "kind": m.kind, "subject_key": m.subject_key, "hypothesis_id": m.hypothesis_id, "payload": m.payload,
            "evidence": m.evidence, "created_at": m.created_at}


def _key(session: Session, h: Hypothesis) -> str:
    c = h.contract or {}
    inst = [session.get(Dataset, i).instrument_id if session.get(Dataset, i) else i for i in c.get("dataset_ids", [])]
    return idea_key(c.get("strategy", {}), inst)


def _validation_view(e: Experiment) -> dict[str, Any]:
    r = e.results or {}
    m = (r.get("oos") or {}).get("metrics", {})
    rob = r.get("robustness") or {}
    bench = r.get("benchmark") or {}
    ov = r.get("overfitting") or {}
    return {
        "experiment_id": e.id, "status": e.status, "created_at": e.created_at, "error": e.error,
        "decision": r.get("decision"), "variants": r.get("variants"), "windows": len(r.get("windows") or []),
        "metrics": {k: m.get(k) for k in METRICS},
        "benchmark_sharpe_net": bench.get("sharpe_net"),
        "excess_vs_benchmark_mean_net": (m.get("mean_net") - bench["mean_net"]) if bench and m.get("mean_net")
        is not None else None,
        "sharpe_ci": [(rob.get("bootstrap") or {}).get("sharpe_ci_low"), (rob.get("bootstrap") or {}).get("sharpe_ci_high")],
        "positive_window_frac": rob.get("positive_window_frac"), "placebo_pvalue": rob.get("placebo_pvalue"),
        "dsr": ov.get("dsr"), "pbo": ov.get("pbo"),
        "cost_sensitivity": rob.get("cost_sensitivity"), "regimes": rob.get("regimes"),
        "per_asset_oos_gross_cc": rob.get("per_asset_oos_gross_cc"),
        "gates": r.get("gates"), "leakage_checks_passed": all(c.get("passed") for c in r.get("leakage_checks") or [])
        if r.get("leakage_checks") else None,
    }


def _evidence_labels(h: Hypothesis, last: Experiment | None, elig: dict[str, Any]) -> dict[str, Any]:
    if last is None:
        pred = {"level": "not_tested", "text": "Not tested yet"}
    elif last.status != "succeeded":
        pred = {"level": "error", "text": f"Validation failed to run: {last.error}"}
    else:
        v = _validation_view(last)
        blocked = [g["gate"] for g in v["gates"] or [] if not g["passed"]]
        pred = {"level": "walk_forward_passed" if not blocked else "no_evidence",
                "text": (f"OOS net Sharpe {_f(v['metrics']['sharpe_net'])}, DSR {_f(v['dsr'])}, "
                         f"placebo p {_f(v['placebo_pvalue'])}; ")
                + ("all gates passed" if not blocked else "blocked by gate(s) " + ",".join(blocked))}
    missing = [r["requirement"] for r in elig["requirements"] if not r["met"]]
    net = {"established": bool(elig["eligible"]),
           "text": "Established on the holdout with explicit costs" if elig["eligible"]
           else "Not established: " + ", ".join(missing)}
    return {"predictive_evidence": pred, "validated_net_profitability": net}


def _f(x: Any) -> str:
    return "n/a" if x is None else f"{x:.3g}"


def hypotheses_table(session: Session, limit: int = 200) -> list[dict[str, Any]]:
    hyps = session.scalars(select(Hypothesis).order_by(Hypothesis.created_at.desc()).limit(limit)).all()
    out = []
    for h in hyps:
        c = h.contract or {}
        msgs = session.scalars(select(AgentMessage).where(AgentMessage.hypothesis_id == h.id)).all()
        idea = next((m.payload for m in msgs if m.kind == "idea"), None)
        sev = [m.payload.get("severity") for m in msgs if m.kind == "objection"]
        last = session.scalar(select(Experiment).where(Experiment.hypothesis_id == h.id, Experiment.kind == "validation")
                              .order_by(Experiment.created_at.desc()).limit(1))
        out.append({
            "id": h.id, "title": h.title, "status": h.status, "version": h.version, "created_by": h.created_by,
            "created_at": h.created_at, "origin": c.get("origin", "manual"),
            "origin_observation": c.get("origin_observation", ""), "strategy": c.get("strategy", {}).get("type"),
            "family": idea.get("family") if idea else None, "n_source_anomalies": len(c.get("source_anomaly_ids", [])),
            "objections": {s: sev.count(s) for s in ("block", "major", "minor")},
            "research_priority": idea.get("priority") if idea else None,
            **_evidence_labels(h, last, eligibility(session, h)),
        })
    return out


def hypothesis_detail(session: Session, h: Hypothesis) -> dict[str, Any]:
    key = _key(session, h)
    msgs = session.scalars(select(AgentMessage).where(
        (AgentMessage.hypothesis_id == h.id) | (AgentMessage.subject_key == key[:64]))
        .order_by(AgentMessage.created_at, AgentMessage.seq)).all()
    exps = session.scalars(select(Experiment).where(Experiment.hypothesis_id == h.id)
                           .order_by(Experiment.created_at.desc())).all()
    gates = session.scalars(select(GateDecision).where(GateDecision.hypothesis_id == h.id)
                            .order_by(GateDecision.created_at.desc())).all()
    vals = [e for e in exps if e.kind == "validation"]
    elig = eligibility(session, h)
    decisions = [m.payload for m in msgs if m.kind in ("decision", "experiment_result")]
    return {
        "hypothesis": {"id": h.id, "title": h.title, "status": h.status, "version": h.version,
                       "family_id": h.family_id, "created_by": h.created_by, "created_at": h.created_at,
                       "contract": h.contract, "contract_hash": h.contract_hash,
                       "final_test_accesses": h.final_test_accesses},
        "idea_key": key,
        "messages": [message_out(m) for m in msgs],
        "objections": [m.payload for m in msgs if m.kind == "objection" and m.hypothesis_id == h.id],
        "decisions": decisions,
        "validations": [_validation_view(e) for e in vals],
        "final_tests": [{"experiment_id": e.id, "created_at": e.created_at, "contaminated": (e.results or {})
                         .get("contaminated"), "metrics": {k: ((e.results or {}).get("metrics") or {}).get(k)
                                                           for k in METRICS}} for e in exps if e.kind == "final_test"],
        "gates": [{"gate": g.gate, "decision": g.decision, "reasons": g.reasons, "experiment_id": g.experiment_id,
                   "created_at": g.created_at} for g in gates],
        "eligibility": elig,
        **_evidence_labels(h, vals[0] if vals else None, elig),
    }
