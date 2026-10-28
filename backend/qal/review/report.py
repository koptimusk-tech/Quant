"""Report per strategy, rebuilt from a stored review decision (and the manual reviews of it).

Twelve sections plus "What we can conclude and what we cannot conclude". Every statement points to the evidence in
the record: the integrity checks, the metrics with their sample sizes, the intervals, the criteria and the rules
that decided. The report can be exported as JSON or Markdown and regenerated from the stored data.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from qal.models import Experiment
from qal.review import decision, service


def build(s: Session, rec: Experiment) -> dict[str, Any]:
    c = rec.config
    inv = c["inventory"]
    hist = service.history(s, c["subject"])
    manual = [h.config for h in hist if h.kind == service.MANUAL_KIND and h.config["decision_id"] == rec.id]
    crit = c.get("criteria") or {}
    sections = {
        "1_hypothesis": inv.get("hypothesis") or {"note": "no registered hypothesis (session outside a program)"},
        "2_original_protocol": {**(c.get("protocol") or {"note": "no protocol was registered before the session"}),
                                "id": (inv.get("protocol") or {}).get("id"),
                                "hash": (inv.get("protocol") or {}).get("hash"),
                                "amendments": c.get("amendments") or []},
        "3_evidence": {k: inv[k] for k in ("evaluation_mode", "evidence", "observed", "markets", "assets",
                                           "timeframes", "observations", "signals", "signal_changes", "trades",
                                           "independent_trades", "missing_or_inactive", "backtest_reference",
                                           "out_of_sample", "paper", "costs_and_assumptions", "risk_limits")},
        "4_data_quality": {"integrity": c["integrity"], "missing_or_inactive": inv["missing_or_inactive"]},
        "5_performance_and_risk": c["metrics"],
        "6_comparison_backtest_and_baseline": c.get("comparison") or {"note": "no protocol: no predefined comparison"},
        "7_uncertainty": {**c["uncertainty"], "multiple_testing": c["multiple_testing"],
                          "layers": c["layers"]},
        "8_operational_incidents": {"incidents": inv["incidents"], "timeline": inv["alerts_pauses_state_changes"],
                                    "diagnosis": c.get("diagnosis")},
        "9_criteria": {"met": crit.get("met", []), "unmet": crit.get("unmet", []),
                       "reject_triggered": crit.get("reject_triggered", []),
                       "not_evaluable": crit.get("not_evaluable", []),
                       "protocol_criteria": crit.get("protocol_criteria", []),
                       "readiness_checks": crit.get("readiness_checks", [])},
        "10_limitations": c["layers"]["limitations"],
        "11_final_state": {"engine": c["decision"], "manual_reviews": manual,
                           "current": service.current_state(s, c["subject"])},
        "12_next_steps": c["decision"]["recommended_actions"],
    }
    return {"decision_id": rec.id, "subject": c["subject"], "strategy": c["strategy"],
            "version": c["candidate_version"], "label": service.mode_label(rec),
            "evidence_class": inv["evaluation_mode"], "decided_at": c["decided_at"],
            "engine_version": c["decision"]["engine_version"], "record_hash": rec.config_hash,
            "integrity_ok": service.verify(rec), "inputs": c["inputs"], "sections": sections,
            "what_we_can_and_cannot_conclude": c["conclusions"],
            "not_an_authorisation": decision.NOT_AUTHORISATION,
            "simulation_only": "No order was sent to any broker, exchange or account."}


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def markdown(rep: dict[str, Any]) -> str:
    sec = rep["sections"]
    L = [f"# Strategy review: {rep['strategy']} v{rep['version'] or '-'}", "",
         (f"Evidence class **{rep['evidence_class']}** · mode {rep['label']} · decided {rep['decided_at']} · engine "
          f"{rep['engine_version']} · record `{rep['record_hash'][:12]}` "
          f"(integrity {'ok' if rep['integrity_ok'] else 'FAILED'})"),
         "", f"> {rep['not_an_authorisation']}", "", "## What we can conclude and what we cannot conclude", "",
         "**We can conclude:**", ""]
    L += [f"- {x}" for x in rep["what_we_can_and_cannot_conclude"]["can_conclude"]] or ["- nothing"]
    L += ["", "**We cannot conclude:**", ""] + [f"- {x}" for x in rep["what_we_can_and_cannot_conclude"]["cannot_conclude"]]
    eng = sec["11_final_state"]["engine"]
    L += ["", "## 11. Final state", "", f"**{eng['state']}**: {eng['reason']}", "", "Rules evaluated:", ""]
    L += [f"- {'→ ' if r['applies'] else ''}{r['rule']}: {r['detail']}" for r in eng["rules"]]
    for m in sec["11_final_state"]["manual_reviews"]:
        L.append(f"- manual review by {m['by']} ({m['at']}): {m['state']}. {m['rationale']}")
    L += ["", "## 1. Hypothesis", ""] + [f"- {k}: {_fmt(v)}" for k, v in sec["1_hypothesis"].items()]
    L += ["", "## 2. Original protocol", ""] + [f"- {k}: {_fmt(v)}" for k, v in sec["2_original_protocol"].items()
                                               if k not in ("acceptance", "rejection", "amendments")]
    for a in sec["2_original_protocol"].get("amendments") or []:
        L.append(f"- amendment {a['number']} by {a['by']} ({'post hoc' if a['post_hoc'] else 'before results'}): "
                 f"{a['changes']} (not used for the decision)")
    ev = sec["3_evidence"]
    L += ["", "## 3. Evidence available", "", f"- {ev['evidence']['note']}",
          (f"- period {ev['observed']['first_bar']} → {ev['observed']['last_bar']}, {ev['observations']} bars, "
           f"{ev['signals']} signals, {ev['trades']} trades ({_fmt(ev['independent_trades'])} independent)"),
          f"- markets {ev['markets']}, assets {ev['assets']}, timeframes {ev['timeframes']}"]
    L += ["", "## 4. Data quality and integrity", "", f"Status **{sec['4_data_quality']['integrity']['status']}**", ""]
    L += [f"- {c['status']} {c['check']}: {c['detail']}" for c in sec["4_data_quality"]["integrity"]["checks"]]
    L += ["", "## 5. Performance and risk", ""]
    for group in ("performance", "risk", "execution"):
        for k, m in sec["5_performance_and_risk"][group].items():
            val = m["value"] if not isinstance(m["value"], (dict, list)) else "(see JSON)"
            L.append(f"- {group}.{k}: {_fmt(val) if m['status'] == 'OK' else 'INSUFFICIENT_DATA'} (n={m['n']}"
                     + (f", {m['note']}" if m.get("note") else "") + ")")
    cmp = sec["6_comparison_backtest_and_baseline"]
    L += ["", "## 6. Comparison with the backtest and the baseline", "", f"- comparable: {cmp.get('comparable')}"]
    if cmp.get("note"):
        L.append(f"- {cmp['note']}")
    for d in cmp.get("deviations") or []:
        L.append(f"- deviation {d['item']}: {d['measured']}; cause {d['cause']}")
    if cmp.get("baseline"):
        L.append(f"- excess over {cmp['baseline']['baseline']}: mean {_fmt(cmp['baseline']['excess_mean'])}, 95% CI "
                 f"{cmp['baseline']['excess_ci95']}")
    u = sec["7_uncertainty"]
    L += ["", "## 7. Uncertainty", "", f"- statistical: {u['layers']['statistical_evidence']}",
          f"- economic: {u['layers']['economic_evidence']}", f"- effective observations: {_fmt(u.get('effective_n'))}",
          f"- {u['p_note']}"]
    inc = sec["8_operational_incidents"]
    L += ["", "## 8. Operational incidents", "", f"- {inc['incidents']}"]
    if inc.get("diagnosis") and inc["diagnosis"].get("triggered"):
        L += ["", "Diagnosis (results worse than expected: " + "; ".join(inc["diagnosis"]["why"]) + "):", ""]
        L += [f"- {c['status']}: {c['cause']}. {c['evidence']}" for c in inc["diagnosis"]["causes"]]
    cr = sec["9_criteria"]
    L += ["", "## 9. Criteria", "", f"- met: {cr['met']}", f"- unmet: {cr['unmet']}",
          f"- reject criteria met: {cr['reject_triggered']}"]
    L += [f"- readiness {r['check']}: {r['met']} ({r['detail']})" for r in cr["readiness_checks"]]
    L += ["", "## 10. Limitations", ""] + [f"- {x}" for x in sec["10_limitations"]]
    L += ["", "## 12. Next steps", ""] + [f"- {x}" for x in sec["12_next_steps"]]
    L += ["", f"_{rep['simulation_only']}_", ""]
    return "\n".join(L)
