"""Which anomalies are worth testing now: the research agents' judgement, without running anything.

The Alpha Researcher, Quant Analyst and Adversarial Reviewer run in read-only mode over the current data and
records (no hypothesis is registered, no backtest runs, the latest stored relationship scan is reused). Every
candidate gets one verdict, with the reasons taken from the reviewer's objections and stored results:

* ``worth_testing``: testable, enough events before the holdout, no earlier study refuted it, not tested yet;
* ``already_tested``: an equivalent hypothesis exists; its outcome is shown instead of testing it again;
* ``not_worth``: a blocking objection (too few events, mixed data...) or an earlier out-of-sample study that
  found no evidence;
* ``not_testable``: the data needed is missing.

The ranking score is the researcher's heuristic priority minus a penalty per major objection. It orders the
queue; it is not evidence that the anomaly predicts anything.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.alpha.agents import AdversarialReviewer, AlphaResearcher, QuantAnalyst, build_context
from qal.alpha.messages import Budget, DataGap
from qal.models import Experiment

MAJOR_PENALTY = 0.05
REFUTING_VERDICTS = {"no_evidence": "an earlier event study found no out-of-sample evidence",
                     "in_sample_only": "an earlier event study found an effect in-sample only; it did not hold "
                                       "out of sample"}
LABELS = {"worth_testing": "Worth testing", "already_tested": "Already tested", "not_worth": "Not worth testing",
          "not_testable": "Not testable with current data"}
ORDER = {"worth_testing": 0, "already_tested": 1, "not_worth": 2, "not_testable": 3}


def _last_validation(session: Session, hyp_id: str) -> dict[str, Any] | None:
    e = session.scalar(select(Experiment).where(Experiment.hypothesis_id == hyp_id, Experiment.kind == "validation")
                       .order_by(Experiment.created_at.desc()).limit(1))
    if e is None:
        return None
    m = ((e.results or {}).get("oos") or {}).get("metrics", {})
    return {"experiment_id": e.id, "status": e.status, "decision": (e.results or {}).get("decision"),
            "sharpe_net_oos": m.get("sharpe_net"), "n_trades_oos": m.get("n_trades")}


def worth_testing(session: Session, include_synthetic: bool = False,
                  dataset_id: str | None = None) -> dict[str, Any]:
    ctx = build_context(session, Budget(max_ideas=200, include_synthetic=include_synthetic),
                        run_relationships=False)
    ideas, gaps = AlphaResearcher().propose(ctx)
    analyst, reviewer = QuantAnalyst(), AdversarialReviewer()
    items: list[dict[str, Any]] = []
    for idea in ideas:
        if dataset_id and dataset_id not in idea.dataset_ids:
            continue
        base = {"key": idea.key, "title": idea.title, "route": idea.route, "family": idea.family,
                "dataset_ids": idea.dataset_ids, "origin_observation": idea.origin_observation,
                "detector": (idea.strategy.get("params") or {}).get("detector")
                if idea.strategy["type"] == "event" else None,
                "strategy": idea.strategy["type"], "priority": idea.priority,
                "evidence": [e.model_dump() for e in idea.evidence], "refuted_if": idea.refutation}
        try:
            form = analyst.formalize(ctx, idea)
        except ValueError as exc:
            form = DataGap(key=idea.key, route=idea.route, family=idea.family, reason=f"invalid contract: {exc}",
                           needs=[])
        if isinstance(form, DataGap):
            items.append({**base, "verdict": "not_testable", "score": None, "reasons": [form.reason],
                          "objections": [], "existing": []})
            continue
        objections = reviewer.review(ctx, idea, form, set())
        blocks = [o for o in objections if o.severity == "block" and o.code != "duplicate"]
        major = [o for o in objections if o.severity == "major"]
        existing = [{**h, "last_validation": _last_validation(session, h["id"])}
                    for h in ctx.existing.get(idea.key, [])]
        verdicts = {e.values.get("verdict") for e in idea.evidence if e.kind == "event_study"}
        reasons: list[str] = []
        tested = [h for h in existing if h["last_validation"] or h["status"] in ("VALIDATION_PASSED",
                                                                                 "VALIDATION_FAILED", "REJECTED")]
        if tested:
            verdict = "already_tested"
            h = tested[-1]
            lv = h["last_validation"] or {}
            reasons.append(f"Hypothesis '{h['title']}' is {h['status']}"
                           + (f" (OOS net Sharpe {lv['sharpe_net_oos']:.2f}, {lv['n_trades_oos']} trades)"
                              if lv.get("sharpe_net_oos") is not None else "")
                           + ". Testing it again would only add trials to the same family.")
        elif blocks:
            verdict = "not_worth"
            reasons += [o.message for o in blocks]
        elif verdicts & set(REFUTING_VERDICTS):
            verdict = "not_worth"
            reasons += [REFUTING_VERDICTS[v] for v in sorted(verdicts & set(REFUTING_VERDICTS))]
        else:
            verdict = "worth_testing"
            reasons.append("No blocking objection and no earlier study refuted it"
                           + ("; already registered, waiting for its first experiment" if existing else "")
                           + ".")
            reasons += [f"Caveat: {o.message}" for o in major if o.code != "origin_used_holdout_period"]
        items.append({**base, "verdict": verdict,
                      "score": round(idea.priority - MAJOR_PENALTY * len(major), 4) if verdict == "worth_testing"
                      else None,
                      "reasons": reasons,
                      "objections": [o.model_dump() for o in objections if o.code != "duplicate"],
                      "existing": existing})
    items.sort(key=lambda x: (ORDER[x["verdict"]], -(x["score"] or 0), -x["priority"], x["key"]))
    for x in items:
        x["verdict_label"] = LABELS[x["verdict"]]
    counts = {v: sum(x["verdict"] == v for x in items) for v in LABELS}
    rel = ctx.relationship_scan
    return {
        "counts": counts, "items": items,
        "data_gaps": [g.model_dump() for g in gaps if not dataset_id],
        "relationship_scan": {"id": rel.id, "created_at": rel.created_at} if rel else None,
        "notes": [
            "Read-only: nothing was registered or backtested to build this list.",
            "The score orders the queue (researcher priority minus 0.05 per major objection); it is not evidence "
            "of predictive power or profitability.",
        ] + ([] if rel else ["No relationship scan stored yet: run a research cycle to add cointegrated pairs."]),
    }
