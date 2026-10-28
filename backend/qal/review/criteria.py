"""Evaluation against the criteria fixed before the program started, and registered protocol amendments.

The original protocol is always the basis of the decision. Its acceptance and reject criteria are evaluated with
the Phase 12 rules (``program.classify.criteria``). The review adds *readiness checks* (duration, observations,
independent trades, risk limits, costs, invalidating incidents, baseline, deviations, stability). They come from
the same protocol values; the only one not in the protocol, stability, can only hold a conclusion back, never
make one more favourable.

A criterion is never changed retrospectively. When a person has a justified reason to change one, an *amendment*
is registered as a new record that keeps the original protocol, says who, when and why, and is flagged
``post_hoc`` when results had already been seen. The review shows how the criteria would read under the amendment
and what changes; the decision stays on the original protocol.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal import audit
from qal.models import Experiment
from qal.program import classify, protocol, registry
from qal.review import uncertainty
from qal.versioning import code_version, stable_hash

AMENDMENT_KIND = "paper_protocol_amendment"
AMENDMENT_VERSION = "protocol-amendment@1.0.0"
AMENDABLE = ("min_trades", "tracking_tolerance_bps", "min_return_correlation", "min_signal_agreement",
             "min_fill_rate", "max_cost_ratio", "reject_cost_ratio", "max_drawdown_ratio", "max_missing_frac",
             "baseline")


class AmendmentError(ValueError):
    pass


class AmendmentIn(BaseModel):
    by: str = Field(min_length=2, max_length=64)
    changes: dict[str, Any] = Field(min_length=1)
    justification: str = Field(min_length=40, max_length=4000)
    impact_expected: str = Field(min_length=10, max_length=2000, description="What the person expects it to change")


def readiness(prog_res: dict[str, Any], proto: dict[str, Any], trk: dict[str, Any], cmp: dict[str, Any] | None,
              cand: dict[str, Any], unc: dict[str, Any], integ: dict[str, Any]) -> list[dict[str, Any]]:
    hz = proto["horizon"]["horizon_bars"]
    n, trades = trk.get("bars", 0), trk.get("trades", 0)
    ind = (unc.get("independent_trades") or {}).get("independent", 0)
    pauses = prog_res.get("pauses", [])
    risk = [p for p in pauses if p["cause"] in ("drawdown_stop", "risk_limit_breach", "exposure", "drawdown")]
    ratio = ((cmp or {}).get("costs") or {}).get("ratio")
    ex = unc.get("excess_ci95")
    dci = (trk.get("diff_vs_backtest") or {}).get("mean_ci95_bps")
    tol = proto["tracking_tolerance_bps"]
    halves = unc.get("halves")
    neff = unc.get("effective_n") or 0

    def r(name: str, met: bool | None, detail: str, source: str = "protocol") -> dict[str, Any]:
        return {"check": name, "met": bool(met) if met is not None else None, "evaluable": met is not None,
                "detail": detail, "source": source}

    return [
        r("duration_complete", n >= hz or prog_res.get("status") == "FINISHED",
          f"{n} of {hz} planned bars; program {prog_res.get('status')}"),
        r("minimum_observations", n >= proto["min_observations_floor"],
          f"{n} bars, floor {proto['min_observations_floor']}"),
        r("independent_trades", ind >= proto["min_trades"], f"{ind} independent of {trades} trades, minimum "
          f"{proto['min_trades']}"),
        r("risk_limits_respected", not any(p["cause"] == "drawdown_stop" for p in pauses) and
          not any(not p.get("resolved_at") for p in risk), f"{len(risk)} risk pause(s): "
          + ", ".join(sorted({p['cause'] for p in risk})) if risk else "no risk pause"),
        r("costs_within_assumptions", (ratio <= proto["max_cost_ratio"]) if ratio is not None else None,
          f"paper/backtest costs {ratio} (max {proto['max_cost_ratio']})"),
        r("no_invalidating_incident", integ["status"] != "BLOCKED" and not any(not p.get("resolved_at")
                                                                              for p in pauses),
          f"integrity {integ['status']}" + (f" ({', '.join(integ['blocking'])})" if integ["blocking"] else "")
          + f"; {sum(1 for p in pauses if not p.get('resolved_at'))} open pause(s)"),
        r("beats_baseline", (ex[0] > 0) if ex else None, f"excess over {proto['baseline']} 95% CI {uncertainty.fmt_ci(ex)}"),
        r("no_material_deviation_vs_backtest", (dci[0] >= -tol and dci[1] <= tol) if dci else None,
          f"difference with the same-bar backtest {uncertainty.fmt_ci(dci)} bps, tolerance ±{tol}"),
        r("stable_enough", (halves["same_sign"] and neff >= 30) if halves else None,
          (f"excess mean by half {halves['first_mean_excess']:.6f} / {halves['second_mean_excess']:.6f}, "
           f"{round(neff)} effective observations (needs 30)") if halves else "too few bars to split the period",
          source="review (can only hold a conclusion back)"),
    ]


def evaluate(prog_res: dict[str, Any], proto: dict[str, Any], trk: dict[str, Any], cmp: dict[str, Any] | None,
             cand: dict[str, Any], unc: dict[str, Any], integ: dict[str, Any]) -> dict[str, Any]:
    crit = classify.criteria(prog_res, proto, trk, cmp, cand)
    rule = classify.rule_state(prog_res, proto, trk, cmp, cand)
    ready = readiness(prog_res, proto, trk, cmp, cand, unc, integ)
    return {"protocol_criteria": crit, "rule_state": rule["state"], "rule_reasons": rule["reasons"],
            "readiness_checks": ready,
            "met": [c["criterion"] for c in crit if c["kind"] == "accept" and c["met"]],
            "unmet": [c["criterion"] for c in crit if c["kind"] == "accept" and not c["met"]],
            "reject_triggered": [c["criterion"] for c in crit if c["kind"] == "reject" and c["met"]],
            "not_evaluable": [c["criterion"] for c in crit if not c["evaluable"]]}


# -- amendments ---------------------------------------------------------------------------------------------------
def amend(s: Session, protocol_id: str, body: AmendmentIn) -> Experiment:
    if registry.is_agent(body.by):
        raise AmendmentError("agents and automated processes cannot amend a protocol")
    pe = protocol.get(s, protocol_id)
    if not protocol.verify(pe):
        raise AmendmentError("the protocol record does not match its hash")
    bad = [k for k in body.changes if k not in AMENDABLE]
    if bad:
        raise AmendmentError(f"cannot amend {bad}: only {list(AMENDABLE)}; mode, candidate, horizon and replay need "
                             "a new protocol and a new program")
    merged = {**pe.config, **body.changes}
    try:  # the amended values must still form a valid protocol
        protocol.ProtocolIn.model_validate({k: v for k, v in merged.items() if k in protocol.ProtocolIn.model_fields})
    except Exception as exc:
        raise AmendmentError(f"the amended protocol is not valid: {exc}") from exc
    progs = [e for e in s.scalars(select(Experiment).where(Experiment.kind == "paper_program")).all()
             if e.config.get("protocol_id") == pe.id]
    seen = any(((p.results or {}).get("last_evaluation") or {}).get("tracking", {}).get("bars") for p in progs)
    prev = amendments(s, pe.id)
    cfg = {"amendment_version": AMENDMENT_VERSION, "protocol_id": pe.id, "protocol_hash": pe.config_hash,
           "number": len(prev) + 1, "changes": body.changes,
           "previous_values": {k: pe.config.get(k) for k in body.changes}, "by": body.by,
           "at": datetime.now(UTC).isoformat(), "justification": body.justification,
           "impact_expected": body.impact_expected, "post_hoc": seen,
           "programs": [p.id for p in progs],
           "rule": "the original protocol stays the basis of every decision; this amendment is shown next to it"}
    e = Experiment(kind=AMENDMENT_KIND, status="registered", algorithm_version=AMENDMENT_VERSION,
                   code_version=code_version(), config=cfg, config_hash=stable_hash(cfg), results={})
    s.add(e)
    s.flush()
    audit.record(s, body.by, "paper_protocol_amended", "experiment", e.id, protocol=pe.id, post_hoc=seen,
                 changes=list(body.changes))
    return e


def amendments(s: Session, protocol_id: str) -> list[Experiment]:
    rows = s.scalars(select(Experiment).where(Experiment.kind == AMENDMENT_KIND).order_by(Experiment.created_at)).all()
    return [e for e in rows if e.config.get("protocol_id") == protocol_id]


def amendment_impact(s: Session, protocol_id: str, prog_res: dict[str, Any], proto: dict[str, Any],
                     trk: dict[str, Any], cmp: dict[str, Any] | None, cand: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    base = {c["criterion"]: c["met"] for c in classify.criteria(prog_res, proto, trk, cmp, cand)}
    base_state = classify.rule_state(prog_res, proto, trk, cmp, cand)["state"]
    acc: dict[str, Any] = {}
    for a in amendments(s, protocol_id):
        acc.update(a.config["changes"])
        alt = {**proto, **acc}
        crit = {c["criterion"]: c["met"] for c in classify.criteria(prog_res, alt, trk, cmp, cand)}
        state = classify.rule_state(prog_res, alt, trk, cmp, cand)["state"]
        out.append({"id": a.id, "number": a.config["number"], "by": a.config["by"], "at": a.config["at"],
                    "post_hoc": a.config["post_hoc"], "changes": a.config["changes"],
                    "previous_values": a.config["previous_values"], "justification": a.config["justification"],
                    "integrity_ok": stable_hash(a.config) == a.config_hash,
                    "changed_criteria": [{"criterion": k, "original": base.get(k), "amended": v}
                                         for k, v in crit.items() if base.get(k) != v],
                    "rule_state_original": base_state, "rule_state_amended": state,
                    "used_for_decision": False})
    return out
