"""Review service: inventory, evaluation bundle, decisions with history, manual reviews and reproducibility.

A *subject* is one paper session: a validation program (one candidate version + one frozen protocol + one
session) or a session started outside any program. Results are kept per subject and never pooled across candidate
versions.

Persistence reuses the experiment registry:

* ``strategy_review`` - one decision of the engine, immutable, with every input it used (hashes of the program,
  protocol, candidate and session, the bars, the code and engine versions), the integrity checks, metrics,
  uncertainty, comparison, criteria, amendments, diagnosis and the decision. A new review that reaches the same
  outputs is not stored again; a different one is stored as a new decision that points to the previous one.
* ``strategy_review_manual`` - a person's review of a decision: the same state or a more conservative one, with
  a written rationale. It never edits the engine's decision.

Reports are rebuilt from the stored record; ``regenerate`` recomputes a decision from the stored data and
configuration and says whether it reproduces.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal import audit
from qal.models import Experiment, PaperSession, PaperSignal
from qal.paper import engine
from qal.program import compare as program_compare
from qal.program import protocol as program_protocol
from qal.program import registry, tracking
from qal.program import service as program
from qal.research.costs import periods_per_year
from qal.review import (
    comparison,
    criteria,
    decision,
    diagnosis,
    integrity,
    metrics,
    uncertainty,
)
from qal.versioning import code_version, stable_hash

KIND = "strategy_review"
MANUAL_KIND = "strategy_review_manual"
REVIEW_VERSION = "strategy-review@1.0.0"


class ReviewError(ValueError):
    pass


def now_utc() -> datetime:
    return datetime.now(UTC)


def _conv(x: Any) -> Any:
    if isinstance(x, np.generic):
        return x.item()
    if isinstance(x, (pd.Timestamp, datetime)):
        return str(x)
    return str(x)


def jsonable(obj: Any) -> Any:
    """Plain JSON (numpy scalars and timestamps converted, non-finite floats as null) so the record hashes and
    stores the same way everywhere."""
    def fix(v: Any) -> Any:
        if isinstance(v, float) and not math.isfinite(v):
            return None
        if isinstance(v, dict):
            return {str(k): fix(x) for k, x in v.items()}
        if isinstance(v, list):
            return [fix(x) for x in v]
        return v

    return fix(json.loads(json.dumps(obj, default=_conv)))


# -- subjects -----------------------------------------------------------------------------------------------------
def programs(s: Session) -> list[Experiment]:
    return list(s.scalars(select(Experiment).where(Experiment.kind == program.KIND)
                          .order_by(Experiment.created_at)).all())


def standalone_sessions(s: Session) -> list[PaperSession]:
    return [ps for ps in s.scalars(select(PaperSession).order_by(PaperSession.created_at)).all()
            if not (ps.config or {}).get("program_id")]


def subject_id(kind: str, ref: str) -> str:
    return f"{kind}:{ref}"


def evidence_class(ps: PaperSession, prog: Experiment | None, cand: dict[str, Any] | None) -> dict[str, Any]:
    n = ps.bars_processed
    synthetic = bool(ps.is_synthetic or (cand or {}).get("is_synthetic"))
    if prog is not None:
        log = (prog.results or {}).get("feed_log", [])
        pros = sum(1 for x in log if x.get("prospective"))
        mode = prog.config["mode"]
    else:
        pros, mode = 0, None
    if synthetic:
        cls = "SYNTHETIC"
        note = "synthetic data: the result tests the procedure, not any market"
    elif prog is not None and mode == "B" and pros:
        cls = "PROSPECTIVE"
        note = (f"{pros} of {n} bars closed after the protocol was frozen; only those are prospective evidence")
    elif prog is None and ps.mode == "new_data":
        cls = "NEW_DATA_WITHOUT_PROTOCOL"
        note = "new data processed without a protocol frozen beforehand: not certified as prospective evidence"
    else:
        cls = "HISTORICAL"
        note = "data already known when the bars were processed (replay): not prospective evidence, even if it was " \
               "processed bar by bar in real time"
    return {"class": cls, "note": note, "bars": n, "prospective_bars": pros,
            "prospective_share": pros / n if n else 0.0, "mode": mode, "session_mode": ps.mode,
            "is_demo": bool(ps.is_demo)}


def _regime(rt: Any, ts: pd.DatetimeIndex) -> list[dict[str, Any]]:
    out = []
    if not len(ts):
        return out
    for sym, ir in rt.instr.items():
        f = ir.frame
        lr = np.log(f["close"].astype(float)).diff()
        t = pd.to_datetime(f["ts"], utc=True)
        before, during = lr[t < ts[0]].tail(250), lr[(t >= ts[0]) & (t <= ts[-1])]
        if len(before) > 20 and len(during) > 20 and before.std() > 0:
            ratio = float(during.std() / before.std())
            out.append({"instrument": sym, "vol_ratio_session_vs_prior_250": round(ratio, 3),
                        "shift": ratio > 1.5 or ratio < 0.67})
    return out


def _inventory_row(s: Session, ps: PaperSession, prog: Experiment | None, cand: dict[str, Any] | None,
                   proto: dict[str, Any] | None, rt: Any, eq: pd.DataFrame, trades: list[dict[str, Any]],
                   unc: dict[str, Any] | None, ev: dict[str, Any]) -> dict[str, Any]:
    sigs = s.scalars(select(PaperSignal).where(PaperSignal.session_id == ps.id)).all()
    gross = eq["gross"].to_numpy() if len(eq) else np.array([])
    flat, run = 0, 0
    for g in gross:
        run = run + 1 if abs(g) < 1e-12 else 0
        flat = max(flat, run)
    res = (prog.results or {}) if prog is not None else {}
    infos = [ir.info for ir in rt.instr.values()]
    row = {
        "subject": subject_id("program", prog.id) if prog is not None else subject_id("session", ps.id),
        "program_id": prog.id if prog is not None else None, "session_id": ps.id,
        "strategy": (cand or {}).get("name") or ", ".join(p.id for p in rt.plans),
        "candidate_id": prog.config["candidate_id"] if prog is not None else None,
        "version": (cand or {}).get("version"),
        "hypothesis": (cand or {}).get("hypothesis"),
        "protocol": ({"id": prog.config["protocol_id"], "hash": prog.config.get("protocol_hash"),
                      "frozen_at": proto["frozen_at"], "mode": proto["mode"], "horizon_bars":
                      proto["horizon"]["horizon_bars"], "min_trades": proto["min_trades"],
                      "baseline": proto["baseline"], "acceptance": proto["acceptance"], "rejection": proto["rejection"]}
                     if prog is not None else None),
        "observed": {"first_bar": str(eq["ts"].iloc[0]) if len(eq) else None,
                     "last_bar": str(eq["ts"].iloc[-1]) if len(eq) else None},
        "evaluation_mode": ev["class"], "evidence": ev,
        "markets": sorted({i.asset_class for i in infos}), "assets": sorted(i.symbol for i in infos),
        "timeframes": sorted({i.frequency for i in infos}),
        "observations": len(eq), "signals": len(sigs), "signal_changes": sum(1 for x in sigs if x.changed),
        "trades": len(trades),
        "independent_trades": (unc or {}).get("independent_trades", {}).get("independent") if unc else None,
        "missing_or_inactive": {"missing_or_skipped_bars": res.get("missing_bars", 0),
                                "bars_without_exposure": int((np.abs(gross) < 1e-12).sum()),
                                "longest_flat_run_bars": int(flat)},
        "incidents": {"pauses": len(res.get("pauses", [])), "open_pauses": sum(1 for p in res.get("pauses", [])
                                                                                if not p.get("resolved_at")),
                      "engine_errors": len(res.get("errors", []))},
        "backtest_reference": ((cand or {}).get("results") or {}).get("holdout"),
        "out_of_sample": ((cand or {}).get("results") or {}).get("oos"),
        "paper": {"total_return": float(np.prod(1 + eq["r"].to_numpy()) - 1) if len(eq) else None,
                  "mean_per_bar": float(eq["r"].mean()) if len(eq) else None},
        "costs_and_assumptions": {k: v.get("source") for k, v in ((cand or {}).get("costs") or {}).items()} or
        {"cost_scenario": rt.cfg.cost_scenario, "source": "session cost models (estimates)"},
        "risk_limits": (cand or {}).get("risk_limits") or {"policy": ps.policy_version},
        "alerts_pauses_state_changes": [{k: t.get(k) for k in ("at", "sim_ts", "kind", "detail")}
                                        for t in res.get("timeline", [])][-50:],
        "admission_kind": (cand or {}).get("admission", {}).get("kind"),
        "is_synthetic": ev["class"] == "SYNTHETIC",
    }
    return row


def bundle(s: Session, ps: PaperSession, prog: Experiment | None) -> dict[str, Any]:
    """Everything the decision uses, computed from what is stored (no decision yet)."""
    cand = proto = None
    if prog is not None:
        ps, cand, proto = program.parts(s, prog)
    rt = engine.load_runtime(s, ps)
    eq = tracking.equity_frame(s, ps)
    info = next(iter(rt.instr.values())).info
    ppy = float(proto["horizon"]["periods_per_year"]) if proto else periods_per_year(info.frequency, info.asset_class)
    trades = tracking.paper_trades(s, ps, rt, eq) if len(eq) else []
    ts = pd.DatetimeIndex(eq["ts"]) if len(eq) else pd.DatetimeIndex([])
    r = eq["r"].to_numpy() if len(eq) else np.array([])
    baseline = proto["baseline"] if proto else "no_trade"
    bl = tracking.baseline_returns(rt, ts, baseline) if len(eq) else np.array([])
    excess = r - bl
    unc = uncertainty.analyse(r, excess, trades, ppy)
    integ = integrity.run_all(s, ps, prog, proto, rt)
    met = metrics.compute(s, ps, rt, eq, trades, ppy)
    ev = evidence_class(ps, prog, cand)
    out: dict[str, Any] = {"session": ps, "program": prog, "candidate": cand, "protocol": proto, "evidence": ev,
                           "integrity": integ, "metrics": met, "uncertainty": unc, "ppy": ppy}
    out["inventory"] = _inventory_row(s, ps, prog, cand, proto, rt, eq, trades, unc, ev)
    if prog is None:
        out.update({"tracking": None, "comparison": None, "criteria": None, "amendments": [], "diagnosis": None,
                    "observed": {"bars": len(r), "mean_net": float(r.mean()) if len(r) else None,
                                 "mean_excess": float(excess.mean()) if len(r) else None}})
        return out
    trk = tracking.track(s, ps, rt, proto)
    cmp = program_compare.compare(s, ps, rt, cand, proto, trk) if trk.get("bars") else None
    crit = criteria.evaluate(prog.results, proto, trk, cmp, cand, unc, integ)
    comp = comparison.build(cmp, trk, unc, cand, proto)
    regime = _regime(rt, ts)
    diag = diagnosis.diagnose(trk, cmp, cand, proto, unc, crit, integ, met, prog.results, prog.config["mode"], regime)
    amends = criteria.amendment_impact(s, prog.config["protocol_id"], prog.results, proto, trk, cmp, cand)
    out.update({"tracking": {k: v for k, v in trk.items() if k not in ("curves", "rolling", "trade_list")},
                "curves": trk.get("curves"), "comparison": comp, "criteria": crit, "amendments": amends,
                "diagnosis": diag, "regime": regime,
                "observed": {"bars": len(r), "trades": len(trades), "mean_net": float(r.mean()) if len(r) else None,
                             "mean_excess": float(excess.mean()) if len(r) else None,
                             "total_return": float(np.prod(1 + r) - 1) if len(r) else None,
                             "baseline": baseline}})
    return out


def _cost_per_bar(b: dict[str, Any]) -> float | None:
    m = b["metrics"]
    tot = m["execution"]["costs_total"]["value"]
    n = m["sample"]["bars"]
    if tot is None or not n:
        return None
    return float(tot) / b["session"].initial_capital / n


def _limitations(b: dict[str, Any]) -> list[str]:
    out = [b["evidence"]["note"]]
    cand = b["candidate"] or {}
    if cand.get("admission", {}).get("kind") == "exploratory":
        out.append("exploratory admission: the candidate did not meet the evidence requirements")
    out.append("costs, spreads and slippage come from configured models (estimates), not from observed executions")
    out.append("fills are simulated (next bar, participation cap); queue position and market impact are not observed")
    if b["integrity"]["status"] != "OK":
        out.append(f"integrity {b['integrity']['status']}: {', '.join(b['integrity']['limited_by'] + b['integrity']['blocking'])}")
    if b["protocol"] is None:
        out.append("no protocol was registered before the session")
    sh = b["uncertainty"].get("sharpe") or {}
    if sh.get("note"):
        out.append(sh["note"])
    if b["uncertainty"].get("bootstrap", {}).get("status") != "OK":
        out.append("too few bars for bootstrap intervals")
    return out


def _decide(b: dict[str, Any], adj_p: float | None) -> dict[str, Any]:
    prog = b["program"]
    res = (prog.results or {}) if prog is not None else {}
    unc = b["uncertainty"]
    uncert = []
    if unc.get("excess_ci95"):
        uncert.append(f"excess over the baseline per bar, 95% CI {uncertainty.fmt_ci(unc['excess_ci95'])}")
    if unc.get("effective_n") is not None:
        uncert.append(f"{round(unc['effective_n'])} effective observations of {unc['n']} bars")
    ctx = {"has_protocol": prog is not None, "evidence": b["evidence"], "integrity": b["integrity"],
           "program_status": res.get("status"), "criteria": b["criteria"],
           "readiness": (b["criteria"] or {}).get("readiness_checks"),
           "open_pauses": sum(1 for p in res.get("pauses", []) if not p.get("resolved_at")),
           "admission_kind": (b["candidate"] or {}).get("admission", {}).get("kind"),
           "is_synthetic": b["evidence"]["class"] == "SYNTHETIC", "adjusted_p": adj_p, "uncertainties": uncert}
    return decision.decide(ctx)


def _record_body(b: dict[str, Any], dec: dict[str, Any], family: list[dict[str, Any]], adj_p: float | None,
                 by: str) -> dict[str, Any]:
    ps, prog = b["session"], b["program"]
    pe = b["protocol"]
    inputs = {"session_id": ps.id, "session_config_hash": ps.config_hash, "bars_processed": ps.bars_processed,
              "last_bar": b["inventory"]["observed"]["last_bar"], "session_code_version": ps.code_version,
              "review_code_version": code_version(), "engine_version": decision.ENGINE_VERSION,
              "metrics_version": metrics.METRICS_VERSION, "review_version": REVIEW_VERSION}
    if prog is not None:
        inputs.update({"program_id": prog.id, "program_config_hash": prog.config_hash,
                       "protocol_id": prog.config["protocol_id"], "protocol_hash": prog.config.get("protocol_hash"),
                       "candidate_id": prog.config["candidate_id"], "candidate_hash": prog.config.get("candidate_hash"),
                       "program_status": (prog.results or {}).get("status"),
                       "pauses": len((prog.results or {}).get("pauses", [])),
                       "amendments": [a["id"] for a in b["amendments"]]})
    own = next((f["p"] for f in family if f["subject"] == b["inventory"]["subject"]), None)
    lay = uncertainty.layers(b["observed"], b["uncertainty"], adj_p, _cost_per_bar(b), _limitations(b))
    concl = decision.allowed_conclusion(dec["state"], b["evidence"]["class"])
    lay["allowed_conclusion"] = concl
    return jsonable({
        "review_version": REVIEW_VERSION, "subject": b["inventory"]["subject"],
        "subject_kind": "program" if prog is not None else "session",
        "strategy": b["inventory"]["strategy"], "candidate_version": b["inventory"]["version"],
        "decided_at": now_utc().isoformat(), "triggered_by": by, "inputs": inputs,
        "multiple_testing": {"method": "Holm (family-wise) over the subjects reviewed together",
                             "family": family, "p_one_sided": own, "adjusted_p": adj_p},
        "inventory": b["inventory"], "integrity": b["integrity"], "metrics": b["metrics"],
        "uncertainty": b["uncertainty"], "comparison": b["comparison"], "criteria": b["criteria"],
        "amendments": b["amendments"], "diagnosis": b["diagnosis"], "tracking": b.get("tracking"),
        "layers": lay, "decision": dec, "conclusions": concl,
        "protocol": ({k: pe.get(k) for k in ("mode", "mode_label", "frozen_at", "horizon", "min_trades", "baseline",
                                             "acceptance", "rejection", "primary_metric", "secondary_metrics",
                                             "rationale", "created_by", "replay", "comparison_method")}
                     if pe else None),
    })


OUTPUT_KEYS = ("subject", "inputs", "multiple_testing", "integrity", "metrics", "uncertainty", "comparison",
               "criteria", "amendments", "diagnosis", "decision")


def outputs_hash(body: dict[str, Any]) -> str:
    inputs = {k: v for k, v in body["inputs"].items() if k != "review_code_version"}
    return stable_hash({**{k: body.get(k) for k in OUTPUT_KEYS}, "inputs": inputs})


def history(s: Session, subject: str) -> list[Experiment]:
    rows = s.scalars(select(Experiment).where(Experiment.kind.in_([KIND, MANUAL_KIND]))
                     .order_by(Experiment.created_at)).all()
    return [e for e in rows if (e.results or {}).get("subject") == subject]


def latest(s: Session, subject: str) -> Experiment | None:
    h = [e for e in history(s, subject) if e.kind == KIND]
    return h[-1] if h else None


def _all_subjects(s: Session) -> list[tuple[PaperSession, Experiment | None]]:
    out: list[tuple[PaperSession, Experiment | None]] = []
    for prog in programs(s):
        ps = s.get(PaperSession, prog.config.get("session_id"))
        if ps is not None:
            out.append((ps, prog))
    out += [(ps, None) for ps in standalone_sessions(s)]
    return out


def run(s: Session, by: str, *, subjects: list[str] | None = None) -> list[dict[str, Any]]:
    """Review every subject (or the listed ones, still adjusting over every subject with a p-value), store the
    decisions that changed and return a summary row per subject."""
    bundles = []
    for ps, prog in _all_subjects(s):
        bundles.append(bundle(s, ps, prog))
    family = [{"subject": b["inventory"]["subject"], "p": b["uncertainty"]["excess_test"]["p_one_sided"]}
              for b in bundles]
    adj = uncertainty.holm({f["subject"]: f["p"] for f in family})
    rows = []
    for b in bundles:
        sub = b["inventory"]["subject"]
        if subjects and sub not in subjects:
            continue
        dec = _decide(b, adj[sub])
        body = _record_body(b, dec, family, adj[sub], by)
        oh = outputs_hash(body)
        prev = latest(s, sub)
        if prev is not None and prev.results.get("outputs_hash") == oh:
            rows.append({**summary_row(prev), "stored": False})
            continue
        body["previous_decision_id"] = prev.id if prev is not None else None
        rec = Experiment(kind=KIND, status=dec["state"], algorithm_version=decision.ENGINE_VERSION,
                         code_version=code_version(), config=body, config_hash=stable_hash(body),
                         data_refs=list((b["program"].data_refs if b["program"] is not None else b["session"].data_refs)
                                        or []),
                         results={"subject": sub, "outputs_hash": oh, "state": dec["state"]})
        s.add(rec)
        s.flush()
        audit.record(s, by, "strategy_review_decided", "experiment", rec.id, subject=sub, state=dec["state"],
                     previous=body["previous_decision_id"])
        rows.append({**summary_row(rec), "stored": True})
    return rows


def get_decision(s: Session, did: str) -> Experiment:
    e = s.get(Experiment, did)
    if e is None or e.kind != KIND:
        raise LookupError("review decision not found")
    return e


def verify(rec: Experiment) -> bool:
    return rec.kind in (KIND, MANUAL_KIND) and stable_hash(rec.config) == rec.config_hash


def manual_review(s: Session, did: str, by: str, state: str, rationale: str) -> Experiment:
    """A person's review of an engine decision: the same state or a more conservative one, as a new record."""
    if registry.is_agent(by):
        raise ReviewError("agents and LLMs cannot change a classification: a named person must record the review")
    if len(rationale.strip()) < 30:
        raise ReviewError("the review needs a written rationale (30 characters at least)")
    rec = get_decision(s, did)
    if not verify(rec):
        raise ReviewError("the decision record does not match its hash")
    sub = rec.results["subject"]
    last = latest(s, sub)
    if last is None or last.id != rec.id:
        raise ReviewError("a newer engine decision exists for this strategy: review that one")
    engine_state = rec.config["decision"]["state"]
    allowed = decision.ALLOWED_MANUAL[engine_state]
    if state not in allowed:
        raise ReviewError(f"the engine gives {engine_state}; a review may only choose {list(allowed)} (the same or "
                          "more conservative, never less)")
    cfg = {"review_version": REVIEW_VERSION, "decision_id": rec.id, "subject": sub, "engine_state": engine_state,
           "state": state, "by": by, "at": now_utc().isoformat(), "rationale": rationale,
           "warning": ("REJECTED by a person: a short unfavourable period alone does not show the hypothesis is "
                       "false") if state == "REJECTED" and engine_state != "REJECTED" else None}
    m = Experiment(kind=MANUAL_KIND, status=state, algorithm_version=REVIEW_VERSION, code_version=code_version(),
                   config=cfg, config_hash=stable_hash(cfg), results={"subject": sub, "decision_id": rec.id})
    s.add(m)
    s.flush()
    audit.record(s, by, "strategy_review_manual", "experiment", m.id, decision=rec.id, state=state,
                 engine_state=engine_state)
    return m


def current_state(s: Session, subject: str) -> dict[str, Any]:
    h = history(s, subject)
    eng = [e for e in h if e.kind == KIND]
    if not eng:
        return {"state": None, "source": None}
    last = eng[-1]
    man = [e for e in h if e.kind == MANUAL_KIND and e.config["decision_id"] == last.id]
    if man:
        return {"state": man[-1].config["state"], "source": "manual review", "by": man[-1].config["by"],
                "engine_state": last.config["decision"]["state"], "decision_id": last.id}
    return {"state": last.config["decision"]["state"], "source": "engine (pending a person's review)",
            "engine_state": last.config["decision"]["state"], "decision_id": last.id}


def regenerate(s: Session, did: str) -> dict[str, Any]:
    """Recompute a stored decision from the stored data and configuration and compare the outputs."""
    rec = get_decision(s, did)
    body = rec.config
    inp = body["inputs"]
    ps = s.get(PaperSession, inp["session_id"])
    prog = s.get(Experiment, inp["program_id"]) if inp.get("program_id") else None
    if ps is None:
        return {"reproducible": False, "reason": "the paper session is no longer stored"}
    if ps.bars_processed != inp["bars_processed"] or (prog is not None and prog.config_hash !=
                                                     inp["program_config_hash"]):
        return {"reproducible": None, "reason": f"the session has moved on ({ps.bars_processed} bars now, "
                f"{inp['bars_processed']} at the decision): a later decision covers the new data"}
    b = bundle(s, ps, prog)
    fam = [dict(f) for f in body["multiple_testing"]["family"]]
    own = b["uncertainty"]["excess_test"]["p_one_sided"]
    for f in fam:
        if f["subject"] == body["subject"]:
            f["p"] = own
    adj = uncertainty.holm({f["subject"]: f["p"] for f in fam})[body["subject"]]
    again = _record_body(b, _decide(b, adj), fam, adj, body["triggered_by"])
    again["inputs"] = {**again["inputs"], "review_code_version": body["inputs"].get("review_code_version")}
    same = outputs_hash(again) == rec.results["outputs_hash"]
    diff = [k for k in OUTPUT_KEYS if stable_hash(again.get(k)) != stable_hash(body.get(k))]
    return {"reproducible": same, "differences": diff, "decision_id": rec.id,
            "state_stored": body["decision"]["state"], "state_recomputed": again["decision"]["state"],
            "code_version_now": code_version(), "code_version_at_decision": body["inputs"].get("review_code_version")}


def summary_row(rec: Experiment) -> dict[str, Any]:
    c = rec.config
    inv = c["inventory"]
    m = c["metrics"]
    unc = c["uncertainty"]
    crit = c.get("criteria") or {}
    return {"decision_id": rec.id, "subject": c["subject"], "subject_kind": c["subject_kind"],
            "strategy": c["strategy"], "version": c["candidate_version"], "program_id": inv["program_id"],
            "session_id": inv["session_id"], "evidence_class": inv["evaluation_mode"],
            "admission_kind": inv.get("admission_kind"), "markets": inv["markets"], "assets": inv["assets"],
            "timeframes": inv["timeframes"], "first_bar": inv["observed"]["first_bar"],
            "last_bar": inv["observed"]["last_bar"], "bars": inv["observations"],
            "horizon_bars": (inv.get("protocol") or {}).get("horizon_bars"), "trades": inv["trades"],
            "independent_trades": inv["independent_trades"],
            "cumulative_return": m["performance"]["cumulative_return"],
            "max_drawdown": m["risk"]["max_drawdown"], "excess_ci95": unc.get("excess_ci95"),
            "backtest_diff_ci95_bps": (((c.get("comparison") or {}).get("backtest") or {}).get("returns") or {})
            .get("diff_ci95_bps"), "comparable": (c.get("comparison") or {}).get("comparable"),
            "integrity": c["integrity"]["status"], "open_incidents": inv["incidents"]["open_pauses"],
            "criteria_met": len(crit.get("met", [])), "criteria_total": len(crit.get("met", [])) +
            len(crit.get("unmet", [])), "state": c["decision"]["state"], "reason": c["decision"]["reason"],
            "decided_at": c["decided_at"], "engine_version": rec.algorithm_version, "integrity_ok": verify(rec)}


def phase_summary(s: Session) -> dict[str, Any]:
    """Whether the stored evidence allows any conclusion, and what still has to be collected."""
    subs = _all_subjects(s)
    rows = []
    for ps, prog in subs:
        sub = subject_id("program", prog.id) if prog is not None else subject_id("session", ps.id)
        last = latest(s, sub)
        rows.append({"subject": sub, "decision": summary_row(last) if last else None,
                     "current": current_state(s, sub)})
    prospective = [r for r in rows if r["decision"] and r["decision"]["evidence_class"] == "PROSPECTIVE"]
    concluded = [r for r in prospective if r["decision"]["state"] in ("COMPLETED", "SUPPORTED_FOR_FURTHER_RESEARCH",
                                                                      "REJECTED", "INCONCLUSIVE")]
    status = "EVIDENCE_AVAILABLE" if concluded else "INSUFFICIENT_EVIDENCE"
    elig = []
    from qal.lab import service as lab

    for e in s.scalars(select(Experiment).where(Experiment.kind == lab.KIND, Experiment.status == "COMPLETED")).all():
        el = registry.eligibility(s, e, registry.latest_robustness(s, e.id))
        elig.append({"lab_experiment_id": e.id, "title": e.config.get("title"),
                     "is_synthetic": any(r.get("is_synthetic") for r in e.data_refs),
                     "decision": el["decision"], "meets_evidence_requirements": all(
                         c["passed"] for c in el["always"] + el["evidence"])})
    to_collect = []
    if not subs:
        to_collect.append("no paper trading session exists: nothing has been observed")
    if not prospective:
        to_collect.append("prospective evidence: a program in mode B (new data only) with a protocol frozen before "
                          "its first bar, run until its planned horizon")
    if not any(x["meets_evidence_requirements"] and not x["is_synthetic"] for x in elig):
        to_collect.append("an admissible candidate: no real-data lab experiment meets the evidence requirements "
                          "(OOS_SUPPORTED or better and a robustness run ROBUST_UNDER_TESTED_ASSUMPTIONS)")
    unsure = [r for r in rows if r["decision"] and r["decision"]["state"] == "CONTINUE_PAPER"]
    if unsure:
        to_collect.append(f"{len(unsure)} program(s) still collecting bars towards their horizon")
    return jsonable({"status": status, "subjects": rows, "prospective_subjects": len(prospective),
                     "concluded_prospective": len(concluded), "lab_candidates": elig, "to_collect": to_collect,
                     "note": "INSUFFICIENT_EVIDENCE: the stored sessions do not allow a conclusion about any strategy. "
                             "No session is fabricated and no evaluation period is presented as concluded."
                     if status == "INSUFFICIENT_EVIDENCE" else "Conclusions are limited to the subjects listed."})


def mode_label(rec: Experiment) -> str:
    m = (rec.config.get("protocol") or {}).get("mode")
    return program_protocol.MODES.get(m, "NO_PROTOCOL") if m else "NO_PROTOCOL"
