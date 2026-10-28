"""Paper trading validation program: start, run, pause causes, audited resume, early stop, kill switch, review.

A program is one candidate version + one frozen protocol + one paper session (simulation only). It is stored as
an ``Experiment`` of kind ``paper_program``; its record keeps the timeline of state changes with the events behind
them, every pause (cause, evidence, timestamps, who resolved it and how), every engine error, the per-bar feed log
(arrival delay, prospective flag) and every review. Nothing is deleted or overwritten: the paper session of a
program cannot be reset, deleted or reconfigured, and a stopped program keeps its whole period.

Each bar runs in its own transaction (the paper engine's guarantee): a crash leaves the program at the previous
bar and the next run repeats it; client order ids make that idempotent.

After every bar the monitor checks the protocol's pause causes. Any cause pauses the paper session before the next
bar. Resuming needs a named person (never an agent), a reason, an explicit confirmation and the cause-specific
check passing again (a reconciliation that passes, a persistence check that passes, ...). A drawdown beyond the
stop level cannot be resumed: it is a reject criterion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qal import audit
from qal.models import (
    Experiment,
    PaperEquity,
    PaperEvent,
    PaperFill,
    PaperOrder,
    PaperSession,
)
from qal.paper import engine
from qal.paper import service as paper
from qal.paper.config import EvaluationCriteria, SessionConfig, StrategyIn
from qal.paper.reconcile import reconcile
from qal.program import classify, compare, protocol, registry, tracking
from qal.versioning import code_version, stable_hash

KIND = "paper_program"
PROGRAM_VERSION = "paper-program@1.0.0"
ENGINE_PAUSES = {"data_quality_pause": "stale_or_corrupt_data", "reconciliation_failed":
                 "inconsistent_orders_positions_pnl", "paused_by_person": "manual_pause", "kill_switch_on": "kill_switch"}
FEED_LOG_MAX = 20_000


class ProgramError(ValueError):
    pass


def now_utc() -> datetime:
    return datetime.now(UTC)


def _person(by: str, what: str) -> None:
    if registry.is_agent(by):
        raise ProgramError(f"agents and automated processes cannot {what}: a named person must do it")


def get(s: Session, pid: str) -> Experiment:
    e = s.get(Experiment, pid)
    if e is None or e.kind != KIND:
        raise LookupError("program not found")
    return e


def parts(s: Session, prog: Experiment) -> tuple[PaperSession, dict[str, Any], dict[str, Any]]:
    ps = s.get(PaperSession, prog.config["session_id"])
    cand = s.get(Experiment, prog.config["candidate_id"])
    proto = s.get(Experiment, prog.config["protocol_id"])
    return ps, cand.config, proto.config


def _update(prog: Experiment, **kw: Any) -> dict[str, Any]:
    prog.results = {**(prog.results or {}), **kw}
    return prog.results


def _timeline(s: Session, prog: Experiment, ps: PaperSession | None, kind: str, detail: str, **evidence: Any) -> None:
    seq = int((ps.summary or {}).get("event_seq", 0)) if ps is not None else 0
    entry = {"at": now_utc().isoformat(), "sim_ts": str(ps.clock_ts) if ps is not None and ps.clock_ts else None,
             "kind": kind, "detail": detail, "event_seq": seq, "evidence": evidence}
    _update(prog, timeline=[*(prog.results or {}).get("timeline", []), entry])


# -- start -----------------------------------------------------------------------------------------------
def start(s: Session, protocol_id: str, by: str) -> Experiment:
    _person(by, "start a program")
    pe = protocol.get(s, protocol_id)
    if not protocol.verify(pe):
        raise ProgramError("the protocol record does not match its hash")
    pc = pe.config
    rec = registry.get(s, pe.results["candidate_id"])
    if not registry.verify(rec) or rec.config_hash != pc["candidate_hash"]:
        raise ProgramError("the candidate record does not match the version the protocol was written for")
    used = [e for e in s.scalars(select(Experiment).where(Experiment.kind == KIND)).all()
            if e.config.get("protocol_id") == pe.id]
    if used:
        raise ProgramError(f"protocol {pe.id} already governs program {used[0].id}; write a new protocol for a new run")
    now = now_utc()
    if protocol.frozen_at(pe) > pd.Timestamp(now):
        raise ProgramError("the protocol is dated after the start: criteria must be fixed before the program starts")
    cand = rec.config
    mode = pc["mode"]
    win = pc.get("replay") or {}
    cfg = SessionConfig(
        name=f"{cand['name']} v{cand['version']} - {protocol.MODES[mode]}",
        mode="new_data" if mode == "B" else "replay",
        strategies=[StrategyIn(strategy_id=f"P:{rec.id}", allocation=cand["allocation"])],
        start=pd.Timestamp(win["start"]).to_pydatetime() if win.get("start") else None,
        end=pd.Timestamp(win["end"]).to_pydatetime() if win.get("end") else None,
        criteria=EvaluationCriteria(min_bars=max(10, pc["horizon"]["horizon_bars"]), min_fills=max(1, pc["min_trades"]),
                                    min_return_correlation=pc["min_return_correlation"],
                                    max_drawdown_ratio=pc["max_drawdown_ratio"], min_fill_rate=pc["min_fill_rate"],
                                    max_cost_ratio=pc["max_cost_ratio"],
                                    min_signal_agreement=pc["min_signal_agreement"]),
        max_staleness_hours=pc["max_staleness_hours"], created_by=by, program_id="pending")
    body = {"program_version": PROGRAM_VERSION, "candidate_id": rec.id, "candidate_hash": rec.config_hash,
            "candidate_name": cand["name"], "candidate_version": cand["version"], "protocol_id": pe.id,
            "protocol_hash": pe.config_hash, "mode": mode, "mode_label": protocol.MODES[mode],
            "admission_kind": cand["admission"]["kind"], "is_synthetic": cand["is_synthetic"], "started_by": by,
            "started_at": now.isoformat()}
    prog = Experiment(kind=KIND, status="running", algorithm_version=PROGRAM_VERSION, code_version=code_version(),
                      config=body, config_hash=stable_hash(body), data_refs=list(rec.data_refs),
                      results={"status": "RUNNING", "pauses": [], "reviews": [], "errors": [], "feed_log": [],
                               "timeline": [], "missing_bars": 0, "rejected_streak": 0, "consecutive_errors": 0,
                               "rule_state": "CONTINUE_PAPER"})
    s.add(prog)
    s.flush()
    cfg.program_id = prog.id
    try:
        ps = paper.create_session(s, cfg)
    except paper.PaperError as exc:
        raise ProgramError(str(exc)) from exc
    ps.config = {**ps.config, "candidate_id": rec.id, "protocol_id": pe.id}
    prog.config = {**body, "session_id": ps.id}
    prog.config_hash = stable_hash(prog.config)
    paper.start(s, ps, by, via_program=True)
    _timeline(s, prog, ps, "started", f"program started by {by} in mode {mode} ({protocol.MODES[mode]})",
              protocol_frozen_at=pc["frozen_at"], horizon=pc["horizon"]["horizon_bars"])
    audit.record(s, by, "paper_program_started", "experiment", prog.id, candidate=rec.id, protocol=pe.id,
                 paper_session=ps.id)
    return prog


# -- monitor ---------------------------------------------------------------------------------------------
def _pause(s: Session, prog: Experiment, ps: PaperSession, cause: str, message: str, **evidence: Any) -> None:
    p = {"id": len((prog.results or {}).get("pauses", [])) + 1, "cause": cause, "message": message,
         "description": protocol.PAUSE_CAUSES.get(cause, cause), "evidence": evidence, "at": now_utc().isoformat(),
         "sim_ts": str(ps.clock_ts) if ps.clock_ts else None, "bar": str(ps.cursor_ts) if ps.cursor_ts else None,
         "resolved_at": None}
    _update(prog, pauses=[*prog.results.get("pauses", []), p], status="PAUSED")
    if ps.status == "RUNNING":
        engine.pause(s, ps, f"program pause: {cause}: {message}", level="error", category="program",
                     code=f"program_{cause}", **{k: v for k, v in evidence.items() if k != "message"})
    _timeline(s, prog, ps, "paused", f"{cause}: {message}", **evidence)


def _bar_checks(s: Session, prog: Experiment, ps: PaperSession, proto: dict[str, Any],
                cand: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    rl = cand["risk_limits"]
    rows = s.scalars(select(PaperEquity).where(PaperEquity.session_id == ps.id)
                     .order_by(PaperEquity.bar_ts.desc()).limit(2)).all()
    out: list[tuple[str, str, dict[str, Any]]] = []
    if not rows:
        return out
    last = rows[0]
    prev_eq = rows[1].equity if len(rows) > 1 else ps.initial_capital
    res = prog.results
    if last.drawdown <= -rl["drawdown_stop"]:
        out.append(("drawdown_stop", f"drawdown {last.drawdown:.2%} beyond the stop level {-rl['drawdown_stop']:.0%}",
                    {"drawdown": last.drawdown, "limit": -rl["drawdown_stop"]}))
    elif last.drawdown <= -rl["drawdown_pause"] and res.get("dd_ack_peak") != ps.peak_equity:
        out.append(("drawdown", f"drawdown {last.drawdown:.2%} beyond the pause level {-rl['drawdown_pause']:.0%}",
                    {"drawdown": last.drawdown, "limit": -rl["drawdown_pause"]}))
    loss = last.equity / prev_eq - 1
    if loss <= -rl["daily_loss_pause"]:
        out.append(("risk_limit_breach", f"one-bar loss {loss:.2%} beyond {-rl['daily_loss_pause']:.0%}",
                    {"bar_return": loss, "limit": -rl["daily_loss_pause"]}))
    if last.gross_exposure > rl["max_gross_exposure"] + 1e-9:
        out.append(("exposure", f"gross exposure {last.gross_exposure:.2f} above {rl['max_gross_exposure']}",
                    {"gross_exposure": last.gross_exposure, "limit": rl["max_gross_exposure"]}))
    fills = s.scalars(select(PaperFill).where(PaperFill.session_id == ps.id, PaperFill.bar_ts == last.bar_ts)).all()
    for f in fills:
        notional = abs(f.quantity * f.price * (f.fx_rate or 1.0))
        bps = (f.commission + f.spread_cost + f.slippage_cost) / notional * 1e4 if notional else 0.0
        if bps > proto["anomalous_cost_bps"]:
            msg = f"fill on {f.instrument} cost {bps:.1f} bps (threshold {proto['anomalous_cost_bps']})"
            out.append(("anomalous_costs", msg, {"fill_id": f.id, "cost_bps": bps}))
            break
    rejected = s.scalar(select(func.count()).select_from(PaperOrder).where(
        PaperOrder.session_id == ps.id, PaperOrder.status == "REJECTED", PaperOrder.created_ts == ps.clock_ts)) or 0
    streak = res.get("rejected_streak", 0) + 1 if rejected else 0
    _update(prog, rejected_streak=streak)
    if streak >= proto["max_rejected_bars"]:
        out.append(("risk_limit_breach", f"orders rejected by the risk gate in {streak} bars in a row",
                    {"rejected_streak": streak}))
    return out


def _feed_entry(rt: engine.Runtime, T: pd.Timestamp, frozen: pd.Timestamp, mode: str, now: datetime) -> dict[str, Any]:
    avail, closed = [], []
    for sym in rt.instr:
        rows = rt.rows_at(sym, T)
        if len(rows):
            avail.append(pd.Timestamp(rows["available_ts"].iloc[-1]))
            closed.append(pd.Timestamp(rows["event_ts"].iloc[-1]))
    a = max(avail) if avail else None
    c = max(closed) if closed else None
    lat = (pd.Timestamp(now) - a).total_seconds() / 3600 if a is not None else None
    return {"bar": str(T), "closed": str(c), "available": str(a), "processed_at": now.isoformat(),
            "processing_delay_h": round(lat, 3) if lat is not None else None,
            "prospective": bool(mode == "B" and c is not None and c > frozen)}


def _new_events(s: Session, prog: Experiment, ps: PaperSession) -> list[PaperEvent]:
    seen = prog.results.get("event_seen", 0)
    evs = s.scalars(select(PaperEvent).where(PaperEvent.session_id == ps.id, PaperEvent.seq > seen)
                    .order_by(PaperEvent.seq)).all()
    if evs:
        _update(prog, event_seen=evs[-1].seq)
    return evs


def run(s: Session, pid: str, max_bars: int, *, now: datetime | None = None,
        fault: Any = None) -> dict[str, Any]:
    """Process up to ``max_bars`` bars. ``fault`` (tests) is called after each step before the commit and may raise
    to simulate a crash or an engine error."""
    prog = get(s, pid)
    if prog.results.get("status") in ("FINISHED", "STOPPED_EARLY"):
        raise ProgramError(f"the program is {prog.results['status']}")
    ps, cand, proto = parts(s, prog)
    if ps.kill_switch:
        _sync_external_pause(s, prog, ps)
        s.commit()
        return {"bars": 0, "status": prog.results["status"], "reason": "kill switch active: no new operations"}
    if ps.status != "RUNNING":
        _sync_external_pause(s, prog, ps)
        s.commit()
        return {"bars": 0, "status": prog.results["status"], "reason": f"session is {ps.status}"}
    rt = engine.load_runtime(s, ps)
    frozen = protocol.frozen_at(s.get(Experiment, prog.config["protocol_id"]))
    done, reason = 0, ""
    skip = bool(prog.results.pop("skip_next_bad_bar", False))
    for i in range(max_bars):
        t_now = now or now_utc()
        try:
            out = engine.step(s, ps, rt, now=t_now, skip_bad_bar=skip and i == 0)
            if fault is not None:
                fault(i)
            evs = _new_events(s, prog, ps)
            gaps = sum(int((e.details or {}).get("bars", 0)) for e in evs if e.code == "gap")
            skipped = sum(1 for e in evs if e.code == "bar_skipped")
            if gaps or skipped:
                _update(prog, missing_bars=prog.results.get("missing_bars", 0) + gaps + skipped)
            if out.get("processed") and not out.get("skipped"):
                log = [*prog.results.get("feed_log", []), _feed_entry(rt, pd.Timestamp(out["bar"]), frozen,
                                                                      prog.config["mode"], t_now)]
                _update(prog, feed_log=log[-FEED_LOG_MAX:], consecutive_errors=0)
                for cause, msg, ev in _bar_checks(s, prog, ps, proto, cand):
                    _pause(s, prog, ps, cause, msg, **ev)
            s.commit()
        except Exception as exc:  # recorded, never hidden; the bar is repeated by the next run
            s.rollback()
            prog, ps = get(s, pid), s.get(PaperSession, prog.config["session_id"])
            n = prog.results.get("consecutive_errors", 0) + 1
            _update(prog, consecutive_errors=n, errors=[*prog.results.get("errors", []),
                                                        {"at": now_utc().isoformat(), "bar_after": str(ps.cursor_ts),
                                                         "error": f"{type(exc).__name__}: {exc}"}][-500:])
            if n >= proto["max_consecutive_errors"]:
                _pause(s, prog, ps, "engine_errors", f"{n} consecutive engine errors", last_error=str(exc)[:500])
            s.commit()
            rt = engine.load_runtime(s, ps)
            if ps.status != "RUNNING":
                reason = "paused after engine errors"
                break
            continue
        # persistence: what was committed must match what was processed
        stored = s.scalar(select(func.count()).select_from(PaperEquity).where(PaperEquity.session_id == ps.id)) or 0
        if stored != ps.bars_processed:
            _pause(s, prog, ps, "persistence_failure", f"{stored} equity rows stored for {ps.bars_processed} bars",
                   stored=stored, processed=ps.bars_processed)
            s.commit()
            reason = "persistence check failed"
            break
        if not out.get("processed"):
            reason = out.get("reason", "")
            if ps.status == "COMPLETED":
                _finish(s, prog, ps, "the replay data ended")
            elif ps.status != "RUNNING":
                _sync_external_pause(s, prog, ps)
            elif reason == "waiting for new data" and prog.config["mode"] == "B":
                last = max(pd.Timestamp(r.frame["available_ts"].iloc[-1]) for r in rt.instr.values())
                age = (pd.Timestamp(t_now) - last).total_seconds() / 3600
                if age > proto["max_staleness_hours"]:
                    _pause(s, prog, ps, "stale_or_corrupt_data", f"no new bar for {age:.0f} h",
                           hours=round(age, 1), limit=proto["max_staleness_hours"])
            s.commit()
            break
        done += 1
        if out.get("skipped"):
            continue
        if ps.status != "RUNNING":  # paused by the engine inside the bar (e.g. reconciliation)
            _sync_external_pause(s, prog, ps)
            s.commit()
            reason = ps.pause_reason or ""
            break
        if _horizon_reached(s, prog, ps, rt, proto):
            _finish(s, prog, ps, "the planned horizon was reached")
            s.commit()
            reason = "horizon reached"
            break
    refresh_state(s, prog)
    s.commit()
    return {"bars": done, "status": prog.results["status"], "session_status": ps.status, "reason": reason,
            "cursor": str(ps.cursor_ts) if ps.cursor_ts else None, "rule_state": prog.results.get("rule_state")}


def _horizon_reached(s: Session, prog: Experiment, ps: PaperSession, rt: engine.Runtime, proto: dict[str, Any]) -> bool:
    n = ps.bars_processed
    if n >= proto["max_duration_bars"]:
        return True
    if n < proto["horizon"]["horizon_bars"]:
        return False
    trades = len(tracking.paper_trades(s, ps, rt, tracking.equity_frame(s, ps)))
    return trades >= proto["min_trades"]


def _sync_external_pause(s: Session, prog: Experiment, ps: PaperSession) -> None:
    """A pause raised by the engine or by a person on the session is registered as a program pause once."""
    if ps.status not in ("PAUSED", "HALTED"):
        return
    if any(not p.get("resolved_at") for p in prog.results.get("pauses", [])):
        return
    last = s.scalar(select(PaperEvent).where(PaperEvent.session_id == ps.id, PaperEvent.code.in_(list(ENGINE_PAUSES)))
                    .order_by(PaperEvent.seq.desc()).limit(1))
    cause = ENGINE_PAUSES.get(last.code, "manual_pause") if last is not None else "manual_pause"
    if ps.kill_switch:
        cause = "kill_switch"
    _pause(s, prog, ps, cause, ps.pause_reason or cause, event_seq=last.seq if last is not None else None,
           event_details=(last.details if last is not None else {}))


def _finish(s: Session, prog: Experiment, ps: PaperSession, why: str) -> None:
    if ps.status == "RUNNING":
        engine.pause(s, ps, f"program finished: {why}", level="info", category="program", code="program_finished")
    _update(prog, status="FINISHED", finished={"at": now_utc().isoformat(), "reason": why,
                                               "bars": ps.bars_processed})
    _timeline(s, prog, ps, "finished", why, bars=ps.bars_processed)
    prog.status, prog.finished_at = "succeeded", now_utc()


# -- state ------------------------------------------------------------------------------------------------
def evaluate(s: Session, prog: Experiment, *, with_curves: bool = True) -> dict[str, Any]:
    ps, cand, proto = parts(s, prog)
    rt = engine.load_runtime(s, ps)
    trk = tracking.track(s, ps, rt, proto)
    cmp = compare.compare(s, ps, rt, cand, proto, trk) if trk.get("bars") else None
    rs = classify.rule_state(prog.results, proto, trk, cmp, cand)
    if not with_curves:
        trk = {k: v for k, v in trk.items() if k not in ("curves", "rolling", "trade_list")}
    return {"tracking": trk, "comparison": cmp, "rule": rs}


def refresh_state(s: Session, prog: Experiment) -> dict[str, Any]:
    ev = evaluate(s, prog, with_curves=False)
    rs = ev["rule"]
    if rs["state"] != prog.results.get("rule_state"):
        ps = s.get(PaperSession, prog.config["session_id"])
        _timeline(s, prog, ps, "rule_state", f"{prog.results.get('rule_state')} -> {rs['state']}",
                  reasons=rs["reasons"][:10])
    _update(prog, rule_state=rs["state"], rule_reasons=rs["reasons"], last_evaluation={
        "at": now_utc().isoformat(), "tracking": ev["tracking"], "rule": rs,
        "comparison": {k: v for k, v in (ev["comparison"] or {}).items() if k not in ("per_asset",)}})
    return ev


def classification(prog: Experiment) -> dict[str, Any]:
    """The reviewed classification, when a review exists for the current rule state; otherwise pending."""
    reviews = prog.results.get("reviews", [])
    rule = prog.results.get("rule_state")
    last = reviews[-1] if reviews else None
    if last and last["rule_state"] == rule:
        return {"state": last["decision"], "reviewed": True, "by": last["by"], "at": last["at"],
                "rule_state": rule}
    return {"state": None, "reviewed": False, "rule_state": rule,
            "pending": "a person must review the rule state and record the classification"}


# -- operations by a person ------------------------------------------------------------------------------
def pause(s: Session, pid: str, by: str, reason: str) -> Experiment:
    _person(by, "operate the program")
    prog = get(s, pid)
    ps, _, _ = parts(s, prog)
    if ps.status != "RUNNING":
        raise ProgramError(f"the session is {ps.status}")
    paper.pause(s, ps, by, reason)
    _pause(s, prog, ps, "manual_pause", f"paused by {by}: {reason}", by=by)
    audit.record(s, by, "paper_program_paused", "experiment", prog.id, reason=reason)
    return prog


def resume(s: Session, pid: str, by: str, reason: str, resolution: str, confirm: bool,
           skip_bad_bar: bool = False) -> Experiment:
    """Audited resume: who, why, how the cause was resolved, and the cause-specific checks passing again."""
    _person(by, "resume a program")
    if not confirm:
        raise ProgramError("resuming must be confirmed explicitly")
    if len(reason.strip()) < 10 or len(resolution.strip()) < 10:
        raise ProgramError("give a reason and how the cause was resolved (10 characters at least each)")
    prog = get(s, pid)
    if prog.results.get("status") in ("FINISHED", "STOPPED_EARLY"):
        raise ProgramError(f"the program is {prog.results['status']}")
    ps, _, _ = parts(s, prog)
    if ps.kill_switch:
        raise ProgramError("kill switch active: release it first (named person and confirmation)")
    open_p = [p for p in prog.results.get("pauses", []) if not p.get("resolved_at")]
    checks = []
    for p in open_p:
        if p["cause"] == "drawdown_stop":
            raise ProgramError("the drawdown stop is a reject criterion: the program cannot resume (stop it and "
                               "record the review)")
        if p["cause"] == "inconsistent_orders_positions_pnl":
            rec = reconcile(s, ps)
            checks.append({"check": "reconciliation", "passed": rec["ok"], "mismatches": rec["mismatches"][:5]})
            if not rec["ok"]:
                raise ProgramError("reconciliation still fails: the program cannot resume")
            ps.summary = {**ps.summary, "reconciliation_failed": False}
        if p["cause"] == "persistence_failure":
            stored = s.scalar(select(func.count()).select_from(PaperEquity).where(PaperEquity.session_id == ps.id))
            ok = stored == ps.bars_processed
            checks.append({"check": "persistence", "passed": ok, "stored": stored, "processed": ps.bars_processed})
            if not ok:
                raise ProgramError("stored rows still do not match the processed bars")
    now = now_utc().isoformat()
    pauses = [{**p, "resolved_at": now, "resolved_by": by, "resolution": resolution, "resume_reason": reason,
               "checks": checks} if not p.get("resolved_at") else p for p in prog.results.get("pauses", [])]
    upd: dict[str, Any] = {"pauses": pauses, "status": "RUNNING"}
    if any(p["cause"] == "drawdown" for p in open_p):
        upd["dd_ack_peak"] = ps.peak_equity  # the same drawdown episode does not pause again
    if any(p["cause"] == "risk_limit_breach" for p in open_p):
        upd["rejected_streak"] = 0
    if any(p["cause"] == "engine_errors" for p in open_p):
        upd["consecutive_errors"] = 0
    if skip_bad_bar:
        upd["skip_next_bad_bar"] = True
    _update(prog, **upd)
    if ps.status in ("PAUSED", "CREATED"):
        paper.start(s, ps, by, via_program=True)
    _timeline(s, prog, ps, "resumed", f"resumed by {by}: {reason}", resolution=resolution, checks=checks,
              resolved_pauses=[p["id"] for p in open_p], skip_bad_bar=skip_bad_bar)
    audit.record(s, by, "paper_program_resumed", "experiment", prog.id, reason=reason, resolution=resolution,
                 pauses=[p["id"] for p in open_p])
    return prog


def kill_switch(s: Session, pid: str, by: str, active: bool, confirm: bool, reason: str = "") -> Experiment:
    _person(by, "operate the kill switch")
    prog = get(s, pid)
    ps, _, _ = parts(s, prog)
    try:
        paper.kill_switch(s, ps, active=active, by=by, confirm=confirm, reason=reason)
    except paper.PaperError as exc:
        raise ProgramError(str(exc)) from exc
    if active:
        _pause(s, prog, ps, "kill_switch", f"kill switch by {by}: {reason}".strip(": "), by=by)
    else:
        _timeline(s, prog, ps, "kill_switch_released", f"released by {by}: {reason}")
    return prog


def stop(s: Session, pid: str, by: str, reason: str, confirm: bool) -> Experiment:
    """Stop before the horizon. The whole period stays in the record and the report; the rule state can then only be
    INCONCLUSIVE or REJECTED, never COMPLETED."""
    _person(by, "stop a program")
    if not confirm:
        raise ProgramError("stopping must be confirmed explicitly")
    if len(reason.strip()) < 10:
        raise ProgramError("give the reason for stopping (10 characters at least)")
    prog = get(s, pid)
    if prog.results.get("status") in ("FINISHED", "STOPPED_EARLY"):
        raise ProgramError(f"the program is already {prog.results['status']}")
    ps, _, _ = parts(s, prog)
    if ps.status == "RUNNING":
        engine.pause(s, ps, f"program stopped early by {by}: {reason}", level="warn", category="program",
                     code="program_stopped")
    _update(prog, status="STOPPED_EARLY", stopped={"by": by, "reason": reason, "at": now_utc().isoformat(),
                                                   "bars": ps.bars_processed})
    _timeline(s, prog, ps, "stopped_early", f"stopped by {by}: {reason}", bars=ps.bars_processed)
    prog.status, prog.finished_at = "succeeded", now_utc()
    audit.record(s, by, "paper_program_stopped", "experiment", prog.id, reason=reason)
    refresh_state(s, prog)
    return prog


def review(s: Session, pid: str, by: str, decision: str, rationale: str) -> Experiment:
    _person(by, "classify a program")
    if len(rationale.strip()) < 30:
        raise ProgramError("the review needs a written rationale (30 characters at least)")
    prog = get(s, pid)
    rs = refresh_state(s, prog)["rule"]
    allowed = classify.ALLOWED[rs["state"]]
    if decision not in allowed:
        raise ProgramError(f"the rules give {rs['state']}; a review may only choose {list(allowed)} (it can be more "
                           "conservative than the rules, never less)")
    entry = {"by": by, "at": now_utc().isoformat(), "decision": decision, "rule_state": rs["state"],
             "rationale": rationale, "criteria": rs["criteria"], "provisional": rs["provisional"]}
    _update(prog, reviews=[*prog.results.get("reviews", []), entry])
    ps = s.get(PaperSession, prog.config["session_id"])
    _timeline(s, prog, ps, "reviewed", f"{decision} recorded by {by} (rules: {rs['state']})", rationale=rationale)
    audit.record(s, by, "paper_program_reviewed", "experiment", prog.id, decision=decision, rule_state=rs["state"])
    return prog


def summary_row(prog: Experiment) -> dict[str, Any]:
    r, c = prog.results or {}, prog.config
    return {"id": prog.id, "created_at": str(prog.created_at), "candidate_id": c["candidate_id"],
            "candidate_name": c["candidate_name"], "candidate_version": c["candidate_version"],
            "protocol_id": c["protocol_id"], "session_id": c.get("session_id"), "mode": c["mode"],
            "mode_label": c["mode_label"], "admission_kind": c["admission_kind"], "is_synthetic": c["is_synthetic"],
            "status": r.get("status"), "rule_state": r.get("rule_state"), "classification": classification(prog),
            "open_pauses": sum(1 for p in r.get("pauses", []) if not p.get("resolved_at")),
            "bars": ((r.get("last_evaluation") or {}).get("tracking") or {}).get("bars"),
            "prospective_bars": sum(1 for x in r.get("feed_log", []) if x.get("prospective"))}
