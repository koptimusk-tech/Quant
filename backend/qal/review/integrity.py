"""Integrity checks run before any result of a paper session is evaluated.

Every check returns ``PASS``, ``WARN``, ``FAIL`` or ``NOT_EVALUABLE`` with its evidence. A ``FAIL`` marked
*material* means the defect can change the results: the evaluation is ``BLOCKED`` until a person resolves it. Other
failures and warnings make it ``LIMITED`` (the results are shown with the limitation). Nothing here corrects data or
recomputes stored results: the checks only read.

Checks: no lookahead (fills after the decision bar, signals decided once their bar was available, a sample of
decisions recomputed from the data known at the time), temporal order, signals vs risk decisions, orders vs fills,
reconciliation of positions, cash and P&L, costs applied once and with the configured models, duplicates, open
positions and missing data, configuration vs the registered protocol, and code or parameter changes during the
period.
"""

from __future__ import annotations

import bisect
import itertools
from collections import Counter
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.models import (
    Experiment,
    PaperEquity,
    PaperEvent,
    PaperFill,
    PaperLedger,
    PaperOrder,
    PaperPosition,
    PaperSession,
    PaperSignal,
)
from qal.paper import engine
from qal.paper.reconcile import reconcile
from qal.paper.signals import decide
from qal.versioning import code_version, stable_hash

LOOKAHEAD_SAMPLE = 20
TOL = 1e-9


def _chk(name: str, status: str, detail: str, *, material: bool, **evidence: Any) -> dict[str, Any]:
    return {"check": name, "status": status, "material": bool(material and status == "FAIL"), "detail": detail,
            "evidence": evidence}


def _u(x: Any) -> pd.Timestamp:
    return engine.utc(x)


def no_lookahead(s: Session, ps: PaperSession, rt: Any) -> dict[str, Any]:
    sigs = s.scalars(select(PaperSignal).where(PaperSignal.session_id == ps.id).order_by(PaperSignal.decided_at)).all()
    fills = s.scalars(select(PaperFill).where(PaperFill.session_id == ps.id)).all()
    orders = {o.id: o for o in s.scalars(select(PaperOrder).where(PaperOrder.session_id == ps.id))}
    early_sig = [x.id for x in sigs if _u(x.decided_at) < _u(x.bar_ts)]
    dec = [_u(x.decided_at) for x in sigs]
    bars = [_u(x.bar_ts) for x in sigs]
    bad_fill = []
    for f in fills:
        o = orders.get(f.order_id)
        if o is None:
            continue
        if _u(f.bar_ts) < _u(o.created_ts):
            bad_fill.append(f.id)
            continue
        if o.purpose == "rebalance":
            i = bisect.bisect_right(dec, _u(o.created_ts)) - 1
            if i >= 0 and _u(f.bar_ts) <= max(bars[: i + 1]):
                bad_fill.append(f.id)
    # recompute a sample of decisions from the data known at the time they were taken (future data rewritten)
    recomputed, mismatched = 0, []
    plans = {p.id: p for p in rt.plans}
    step = max(1, len(sigs) // LOOKAHEAD_SAMPLE)
    for sg in sigs[::step][:LOOKAHEAD_SAMPLE]:
        plan = plans.get(sg.strategy_id)
        if plan is None:
            continue
        syms = [rt.by_dataset[d] for d in plan.dataset_ids]
        frames = []
        for sym in syms:
            known = rt.instr[sym].known(sg.decided_at)
            f = rt.instr[sym].frame.copy()
            f.loc[len(known):, ["open", "high", "low", "close"]] *= 3.0  # data after the decision is rewritten
            frames.append(f.iloc[:len(known)])
        again = decide(plan, frames, rt.cfg.history_bars)
        if again is None:
            continue
        recomputed += 1
        if any(abs(again[j] - sg.targets.get(sym, 0.0)) > 1e-9 for j, sym in enumerate(syms)):
            mismatched.append(str(sg.bar_ts))
    problems = len(early_sig) + len(bad_fill) + len(mismatched)
    if not sigs:
        return _chk("no_lookahead", "NOT_EVALUABLE", "no signals stored", material=True)
    return _chk("no_lookahead", "FAIL" if problems else "PASS",
                f"{len(early_sig)} signal(s) decided before their bar was available, {len(bad_fill)} fill(s) on or "
                f"before the decision bar, {len(mismatched)} of {recomputed} recomputed decision(s) differ when the "
                "data after the decision is rewritten", material=True,
                signals_early=early_sig[:10], fills_on_decision_bar=bad_fill[:10], recomputed=recomputed,
                recomputed_mismatch=mismatched[:10])


def temporal_order(s: Session, ps: PaperSession) -> dict[str, Any]:
    eq = [_u(e.bar_ts) for e in s.scalars(select(PaperEquity).where(PaperEquity.session_id == ps.id)
                                          .order_by(PaperEquity.id))]
    ordered = sorted(eq)
    dup = [str(t) for t, n in Counter(eq).items() if n > 1]
    evs = s.scalars(select(PaperEvent).where(PaperEvent.session_id == ps.id).order_by(PaperEvent.seq)).all()
    seqs = [e.seq for e in evs]
    sim = [_u(e.sim_ts) for e in evs if e.sim_ts is not None]
    back = sum(1 for a, b in itertools.pairwise(sim) if b < a)
    fills = s.scalars(select(PaperFill).where(PaperFill.session_id == ps.id)).all()
    outside = [f.id for f in fills if eq and not (ordered[0] <= _u(f.bar_ts) <= ordered[-1])]
    problems = []
    if dup:
        problems.append(f"{len(dup)} duplicated equity bar(s)")
    if len(set(seqs)) != len(seqs) or seqs != sorted(seqs):
        problems.append("event sequence numbers are not unique and increasing")
    if outside:
        problems.append(f"{len(outside)} fill(s) outside the processed bars")
    if back:
        problems.append(f"the simulated clock goes back {back} time(s) in the event log")
    if not eq:
        return _chk("temporal_order", "NOT_EVALUABLE", "no bars processed", material=True)
    return _chk("temporal_order", "FAIL" if problems else "PASS",
                "; ".join(problems) or f"{len(eq)} bars from {ordered[0]} to {ordered[-1]}, events in order",
                material=True, duplicated_bars=dup[:10], fills_outside=outside[:10], clock_backwards=back)


def signals_vs_risk(s: Session, ps: PaperSession) -> dict[str, Any]:
    sigs = s.scalars(select(PaperSignal).where(PaperSignal.session_id == ps.id)).all()
    by = {(x.strategy_id, _u(x.decided_at)) for x in sigs}
    orders = s.scalars(select(PaperOrder).where(PaperOrder.session_id == ps.id)).all()
    orphan = [o.client_order_id for o in orders if o.purpose == "rebalance" and (o.strategy_id, _u(o.created_ts))
              not in by]
    order_at = {(o.strategy_id, _u(o.created_ts)) for o in orders}
    risk = {(e.strategy_id, _u(e.sim_ts)) for e in s.scalars(select(PaperEvent).where(
        PaperEvent.session_id == ps.id, PaperEvent.category.in_(["risk", "order"]))) if e.sim_ts is not None}
    silent = [str(x.bar_ts) for x in sigs if x.changed and (x.strategy_id, _u(x.decided_at)) not in order_at
              and (x.strategy_id, _u(x.decided_at)) not in risk and (None, _u(x.decided_at)) not in risk]
    status = "FAIL" if orphan else "WARN" if silent else "PASS"
    return _chk("signals_vs_risk_decisions", status,
                f"{len(orphan)} rebalance order(s) without a signal at their decision time; {len(silent)} changed "
                "signal(s) with neither an order nor a risk or order event at that time (e.g. a change below the "
                "minimum lot)", material=True, orphan_orders=orphan[:10], changed_without_action=silent[:10])


def orders_vs_fills(s: Session, ps: PaperSession) -> dict[str, Any]:
    orders = {o.id: o for o in s.scalars(select(PaperOrder).where(PaperOrder.session_id == ps.id))}
    fills = s.scalars(select(PaperFill).where(PaperFill.session_id == ps.id)).all()
    filled: dict[str, float] = {}
    bad = []
    for f in fills:
        o = orders.get(f.order_id)
        if o is None:
            bad.append({"fill": f.id, "problem": "fill without an order of this session"})
            continue
        filled[o.id] = filled.get(o.id, 0.0) + f.quantity
        if f.instrument != o.instrument or f.side != o.side:
            bad.append({"fill": f.id, "problem": "instrument or side differs from its order"})
    for o in orders.values():
        q = filled.get(o.id, 0.0)
        if abs(q - o.filled_quantity) > TOL * max(1.0, abs(q)):
            bad.append({"order": o.client_order_id, "problem": f"filled {o.filled_quantity} vs fills {q}"})
        if o.filled_quantity > o.quantity * (1 + 1e-9) + TOL:
            bad.append({"order": o.client_order_id, "problem": "filled more than ordered"})
        if o.status == "FILLED" and abs(o.filled_quantity - o.quantity) > 1e-9 * max(1.0, o.quantity):
            bad.append({"order": o.client_order_id, "problem": "FILLED with a remaining quantity"})
        if o.status == "REJECTED" and q > 0:
            bad.append({"order": o.client_order_id, "problem": "REJECTED with fills"})
    return _chk("orders_vs_fills", "FAIL" if bad else "PASS",
                f"{len(bad)} inconsistency(ies) between {len(orders)} orders and {len(fills)} fills", material=True,
                problems=bad[:10])


def reconciliation(s: Session, ps: PaperSession) -> dict[str, Any]:
    rec = reconcile(s, ps)
    pos = s.scalars(select(PaperPosition).where(PaperPosition.session_id == ps.id)).all()
    last = s.scalar(select(PaperEquity).where(PaperEquity.session_id == ps.id).order_by(PaperEquity.bar_ts.desc())
                    .limit(1))
    pnl_ok, detail = True, ""
    if last is not None:
        rebuilt = ps.initial_capital + sum(p.realized_pnl + p.unrealized_pnl - p.fees - p.holding_costs for p in pos)
        pnl_ok = abs(rebuilt - last.equity) <= 1e-6 * max(1.0, abs(last.equity))
        detail = f"; equity {last.equity:.2f} vs capital + P&L - costs {rebuilt:.2f}"
    ok = rec["ok"] and pnl_ok
    return _chk("reconciliation", "PASS" if ok else "FAIL",
                f"{len(rec['mismatches'])} mismatch(es) rebuilding positions, cash and orders from the ledgers{detail}",
                material=True, mismatches=rec["mismatches"][:10], equity_identity=pnl_ok)


def costs_applied(s: Session, ps: PaperSession, rt: Any) -> dict[str, Any]:
    fills = s.scalars(select(PaperFill).where(PaperFill.session_id == ps.id)).all()
    led = s.scalars(select(PaperLedger).where(PaperLedger.session_id == ps.id, PaperLedger.kind == "commission")).all()
    neg = [f.id for f in fills if min(f.commission, f.spread_cost, f.slippage_cost) < -TOL]
    missing: list[str] = []
    for f in fills:
        cm = rt.costs.get((f.strategy_id, f.instrument))
        if cm is None:
            continue
        if cm.commission_bps > 0 and f.commission <= 0:
            missing.append(f"{f.id}: no commission")
        if cm.half_spread_bps > 0 and f.spread_cost <= 0:
            missing.append(f"{f.id}: no spread")
    ledger, fill_comm = sum(-x.amount for x in led), sum(f.commission for f in fills)
    once = abs(ledger - fill_comm) <= 1e-6 * max(1.0, abs(fill_comm))
    problems = len(neg) + len(missing) + (0 if once else 1)
    if not fills:
        return _chk("costs_applied", "NOT_EVALUABLE", "no fills", material=False)
    return _chk("costs_applied", "FAIL" if problems else "PASS",
                f"{len(neg)} fill(s) with a negative cost, {len(missing)} fill(s) without a configured cost, ledger "
                f"commissions {ledger:.4f} vs fills {fill_comm:.4f} ({'counted once' if once else 'mismatch'})",
                material=True, negative=neg[:10], missing=missing[:10], ledger_commission=ledger,
                fills_commission=fill_comm, note="cost models are configured estimates, not observed costs")


def duplicates(s: Session, ps: PaperSession) -> dict[str, Any]:
    ords = s.scalars(select(PaperOrder).where(PaperOrder.session_id == ps.id)).all()
    fills = s.scalars(select(PaperFill).where(PaperFill.session_id == ps.id)).all()
    sigs = s.scalars(select(PaperSignal).where(PaperSignal.session_id == ps.id)).all()
    d_ord = [k for k, n in Counter(o.client_order_id for o in ords).items() if n > 1]
    d_fill = [f"{k[0]}#{k[1]}" for k, n in Counter((f.order_id, f.seq) for f in fills).items() if n > 1]
    d_sig = [f"{k[0]}@{k[1]}" for k, n in Counter((x.strategy_id, str(x.bar_ts)) for x in sigs).items() if n > 1]
    found = len(d_ord) + len(d_fill) + len(d_sig)
    return _chk("no_duplicates", "FAIL" if found else "PASS",
                f"{len(d_ord)} duplicated order id(s), {len(d_fill)} duplicated fill(s), {len(d_sig)} duplicated "
                "signal(s)", material=True, orders=d_ord[:10], fills=d_fill[:10], signals=d_sig[:10])


def open_positions_and_missing(s: Session, ps: PaperSession, prog: Experiment | None,
                               proto: dict[str, Any] | None) -> dict[str, Any]:
    pos = [p for p in s.scalars(select(PaperPosition).where(PaperPosition.session_id == ps.id)) if abs(p.quantity) > TOL]
    missing = (prog.results or {}).get("missing_bars", 0) if prog is not None else 0
    n = ps.bars_processed
    frac = missing / max(1, n + missing)
    limit = (proto or {}).get("max_missing_frac")
    over = limit is not None and frac > limit
    status = "FAIL" if over else "WARN" if pos or missing else "PASS"
    return _chk("open_positions_and_missing_data", status,
                f"{len(pos)} position(s) open at the last bar (valued at the last close, P&L unrealised); {missing} "
                f"missing or skipped bar(s) ({frac:.1%}" + (f", limit {limit:.0%})" if limit is not None else ")"),
                material=False, open_positions=[{"instrument": p.instrument, "quantity": p.quantity,
                                                 "unrealized_pnl": p.unrealized_pnl} for p in pos],
                missing_bars=missing, missing_frac=frac)


def configuration(s: Session, ps: PaperSession, prog: Experiment | None, rt: Any) -> dict[str, Any]:
    from qal.program import protocol, registry

    problems = []
    if stable_hash(ps.criteria) != ps.criteria_hash:
        problems.append("session criteria do not match their hash")
    if prog is None:
        return _chk("configuration_matches_protocol", "NOT_EVALUABLE",
                    "no protocol was registered before this session: there is nothing to check it against"
                    + ("; " + "; ".join(problems) if problems else ""), material=False)
    pe = s.get(Experiment, prog.config["protocol_id"])
    ce = s.get(Experiment, prog.config["candidate_id"])
    if pe is None or not protocol.verify(pe):
        problems.append("the protocol record does not match its hash")
    elif pe.config_hash != prog.config.get("protocol_hash"):
        problems.append("the program points to a different protocol version")
    if ce is None or not registry.verify(ce):
        problems.append("the candidate record does not match its hash")
    elif ce.config_hash != prog.config.get("candidate_hash"):
        problems.append("the program points to a different candidate version")
    if ps.config.get("candidate_id") != prog.config["candidate_id"] or ps.config.get("program_id") != prog.id:
        problems.append("the session belongs to another candidate or program")
    if ce is not None and rt.plans:
        plan = rt.plans[0]
        st = ce.config["strategy"]
        if plan.type != st["type"] or plan.params != st["params"]:
            problems.append(f"session strategy {plan.type} {plan.params} differs from the candidate {st['type']} "
                            f"{st['params']}")
        if abs(plan.allocation - ce.config["allocation"]) > 1e-12:
            problems.append("allocation differs from the candidate")
    return _chk("configuration_matches_protocol", "FAIL" if problems else "PASS",
                "; ".join(problems) or "protocol, candidate and session configuration match their hashes and each "
                "other", material=True, protocol_id=prog.config["protocol_id"], candidate_id=prog.config["candidate_id"])


def changes_during_period(s: Session, ps: PaperSession, prog: Experiment | None) -> dict[str, Any]:
    changes = s.scalars(select(PaperEvent).where(PaperEvent.session_id == ps.id, PaperEvent.code == "config_changed")
                        .order_by(PaperEvent.seq)).all()
    history = [h for h in (ps.config_history or []) if h.get("change") != "created"]
    changed = bool(history) or bool(changes) or ps.config_version != 1
    now = code_version()
    codes = {"session": ps.code_version, "program": prog.code_version if prog is not None else None, "review": now}
    differs = len({c for c in codes.values() if c}) > 1
    status = "FAIL" if changed else "WARN" if differs else "PASS"
    detail = (f"configuration or parameters changed during the period ({max(len(history), len(changes))} change(s))"
              if changed else "no configuration or parameter change during the period")
    detail += "; code versions " + ", ".join(f"{k} {v}" for k, v in codes.items() if v)
    if differs:
        detail += " (the code changed after the run: the review recomputes with the current code and says so)"
    return _chk("code_and_parameter_changes", status, detail, material=True, code_versions=codes,
                config_history=history, config_changes=[e.message for e in changes][:10])


def run_all(s: Session, ps: PaperSession, prog: Experiment | None, proto: dict[str, Any] | None,
            rt: Any) -> dict[str, Any]:
    checks = [no_lookahead(s, ps, rt), temporal_order(s, ps), signals_vs_risk(s, ps), orders_vs_fills(s, ps),
              reconciliation(s, ps), costs_applied(s, ps, rt), duplicates(s, ps),
              open_positions_and_missing(s, ps, prog, proto), configuration(s, ps, prog, rt),
              changes_during_period(s, ps, prog)]
    blocking = [c["check"] for c in checks if c["material"]]
    limited = [c["check"] for c in checks if c["status"] in ("WARN", "NOT_EVALUABLE") or
               (c["status"] == "FAIL" and not c["material"])]
    status = "BLOCKED" if blocking else "LIMITED" if limited else "OK"
    return {"status": status, "blocking": blocking, "limited_by": limited, "checks": checks,
            "note": "BLOCKED: a defect that can change the results; nothing is evaluated as evidence until a person "
                    "resolves it. LIMITED: results are shown with the listed limitations. No data was corrected."}
