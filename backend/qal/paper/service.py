"""Paper session lifecycle: create, start, pause, kill switch, manual orders, config changes, reset, delete.

States: CREATED -> RUNNING <-> PAUSED; RUNNING -> COMPLETED (replay reached its last bar); any open state ->
HALTED by the kill switch (all open orders cancelled, no new orders). Leaving HALTED needs a named person and
an explicit confirmation. Reset and delete need an explicit confirmation too; a deleted session keeps its
experiment record (marked deleted, with the final summary) so nothing silently disappears from the registry.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from qal import audit
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
    RiskDecision,
    RiskPolicy,
)
from qal.paper import accounting, engine, feed
from qal.paper.config import MUTABLE_FIELDS, SessionConfig
from qal.paper.orders import OrderIn
from qal.paper.signals import DEMO_NOTE, resolve
from qal.portfolio.risk_manager import PolicyLimits, default_policy, policy_label
from qal.portfolio.strategies import EligibilityCriteria, catalog
from qal.research.strategies import STRATEGIES
from qal.versioning import code_version, stable_hash

REPLAY_NOTE = "HISTORICAL REPLAY: a reproduction of past bars processed one at a time. It is not real-time trading."
SIM_NOTE = "SIMULATION ONLY: no order is sent to any broker, exchange or account."


class PaperError(ValueError):
    pass


def eligible_strategies(s: Session) -> dict[str, Any]:
    recs = catalog(s, EligibilityCriteria(), include_synthetic=False)
    validated = [{"strategy_id": r.id, "name": r.name, "status": r.approval_status, "eligible": r.approval_status in
                  ("VALIDATION_PASSED", "PAPER_TRADING_ELIGIBLE"), "reasons": r.reasons, "universe": r.universe,
                  "is_synthetic": r.is_synthetic} for r in recs]
    demos = [{"strategy_id": f"DEMO:{k}", "name": k, "description": v.description, "defaults": v.defaults,
              "n_assets": v.n_assets, "note": DEMO_NOTE} for k, v in STRATEGIES.items()]
    return {"validated": validated, "demo": demos}


def create_session(s: Session, cfg: SessionConfig) -> PaperSession:
    plans, problems = [], []
    for st in cfg.strategies:
        plan, why = resolve(s, st.strategy_id, dataset_ids=st.dataset_ids, params=st.params, allocation=st.allocation,
                            allow_demo=cfg.allow_unvalidated_demo)
        problems += why
        if plan:
            plans.append(plan)
    if problems:
        raise PaperError("; ".join(problems))
    policy = s.get(RiskPolicy, cfg.policy_id) if cfg.policy_id else default_policy(s)
    if policy is None:
        raise PaperError(f"risk policy {cfg.policy_id} not found")
    limits = PolicyLimits.model_validate(policy.limits)
    cap_notes = []
    for p in plans:  # the HRM per-strategy cap applies to allocations as well
        if p.allocation > limits.strategy.max_weight:
            cap_notes.append(f"{p.id}: allocation {p.allocation} capped to {limits.strategy.max_weight}")
            p.allocation = limits.strategy.max_weight
    if sum(p.allocation for p in plans) > limits.portfolio.max_leverage + 1e-9:
        raise PaperError(f"total allocation {sum(p.allocation for p in plans):.2f} above max leverage "
                         f"{limits.portfolio.max_leverage}")
    ds_ids = list(dict.fromkeys(d for p in plans for d in p.dataset_ids))
    instr = feed.instruments_for(s, ds_ids)
    syms = [i.symbol for i in instr]
    if len(set(syms)) != len(syms):
        raise PaperError("two datasets of the same symbol in one session")
    freqs = {i.frequency for i in instr}
    if len(freqs) != 1:
        raise PaperError(f"all instruments must share one frequency, got {sorted(freqs)}")
    bad = [i.symbol for i in instr if i.quality_status == "fail"]
    if bad:
        raise PaperError(f"datasets with failed data quality cannot be traded: {bad}")
    conv, conv_problems = accounting.conversion_plan(
        {i.symbol: {"asset_class": i.asset_class, "currency": i.currency} for i in instr}, cfg.account_currency.upper())
    if conv_problems:
        raise PaperError("; ".join(conv_problems))
    is_syn = any(i.is_synthetic for i in instr)
    is_demo = any(p.kind == "demo" for p in plans)
    cfg_dump = cfg.model_dump(mode="json")
    if cfg.start is None and cfg.mode == "replay":  # default: the last quarter of the common history
        frames = [feed.load(s, i, "replay")[0] for i in instr]
        first = max(pd.Timestamp(f["ts"].iloc[0]) for f in frames)
        last = min(pd.Timestamp(f["ts"].iloc[-1]) for f in frames)
        cfg_dump["start"] = (first + (last - first) * 0.75).isoformat()
    refs = [{"dataset_id": i.dataset_id, "symbol": i.symbol, "content_hash": i.content_hash,
             "provider": i.provider_id, "is_synthetic": i.is_synthetic, "asset_class": i.asset_class} for i in instr]
    assumed = [s_ for s_, p in conv.items() if p["assumed"]]
    resolved = {"plans": [p.__dict__ for p in plans], "conversion": conv, "engine": engine.ENGINE_VERSION,
                "notes": cap_notes + ([f"currency unknown for {assumed}: assumed {cfg.account_currency}"] if assumed else [])}
    chash = stable_hash(cfg_dump)
    exp = Experiment(kind="paper_session", status="running", algorithm_version=engine.ENGINE_VERSION,
                     code_version=code_version(), config={**cfg_dump, "resolved": resolved}, config_hash=chash,
                     data_refs=refs, n_tests=0, results={})
    s.add(exp)
    s.flush()
    ps = PaperSession(name=cfg.name, status="CREATED", mode=cfg.mode, is_synthetic=is_syn, is_demo=is_demo,
                      config={**cfg_dump, "resolved": resolved}, config_hash=chash, config_version=1,
                      config_history=[{"version": 1, "at": datetime.now(UTC).isoformat(), "by": cfg.created_by,
                                       "change": "created"}],
                      criteria=cfg.criteria.model_dump(), criteria_hash=stable_hash(cfg.criteria.model_dump()),
                      code_version=code_version(), data_refs=refs, experiment_id=exp.id, policy_id=policy.id,
                      policy_version=policy_label(policy), initial_capital=cfg.capital,
                      account_currency=cfg.account_currency.upper(), cash=cfg.capital, peak_equity=cfg.capital,
                      day_start_equity=cfg.capital, created_by=cfg.created_by,
                      summary={"equity": cfg.capital, "cash": cfg.capital, "return_net": 0.0})
    s.add(ps)
    s.flush()
    engine.emit(s, ps, "info", "system", "session_created", f"session created by {cfg.created_by}: {SIM_NOTE}",
                strategies=[p.id for p in plans], is_synthetic=is_syn, is_demo=is_demo)
    for n in resolved["notes"]:
        engine.emit(s, ps, "warn", "config", "assumption", n)
    if is_demo:
        engine.emit(s, ps, "warn", "config", "unvalidated_demo", DEMO_NOTE)
    audit.record(s, cfg.created_by, "paper_session_created", "paper_session", ps.id, experiment_id=exp.id)
    return ps


def get(s: Session, sid: str) -> PaperSession:
    ps = s.get(PaperSession, sid)
    if ps is None or ps.status == "DELETED":
        raise LookupError("paper session not found")
    return ps


def start(s: Session, ps: PaperSession, by: str) -> PaperSession:
    if ps.status not in ("CREATED", "PAUSED"):
        raise PaperError(f"cannot start a session that is {ps.status}")
    if ps.kill_switch:
        raise PaperError("kill switch active: release it first")
    if (ps.summary or {}).get("reconciliation_failed"):
        raise PaperError("reconciliation failed: run a reconciliation that passes before resuming")
    if ps.status == "CREATED" and ps.mode == "new_data":
        rt = engine.load_runtime(s, ps)  # only bars that arrive after the start are traded
        ps.cursor_ts = max(r.frame["ts"].iloc[-1] for r in rt.instr.values()).to_pydatetime()
        ps.clock_ts = max(r.frame["available_ts"].iloc[-1] for r in rt.instr.values()).to_pydatetime()
    ps.status, ps.pause_reason = "RUNNING", None
    engine.emit(s, ps, "info", "system", "session_started", f"started by {by}"
                + (f" ({REPLAY_NOTE})" if ps.mode == "replay" else " (new data only)"))
    return ps


def pause(s: Session, ps: PaperSession, by: str, reason: str = "paused by a person") -> PaperSession:
    if ps.status != "RUNNING":
        raise PaperError(f"cannot pause a session that is {ps.status}")
    engine.pause(s, ps, f"{reason} ({by})", level="info", code="paused_by_person")
    return ps


def kill_switch(s: Session, ps: PaperSession, *, active: bool, by: str, confirm: bool, reason: str = "") -> PaperSession:
    if not confirm:
        raise PaperError("the kill switch must be confirmed explicitly")
    if active:
        n = 0
        for o in engine.open_orders(s, ps):
            engine.cancel(s, ps, o, "kill switch")
            n += 1
        ps.kill_switch, ps.status, ps.pause_reason = True, "HALTED", f"kill switch by {by}: {reason}".strip(": ")
        engine.emit(s, ps, "critical", "risk", "kill_switch_on", f"kill switch activated by {by}; {n} open orders "
                    "cancelled; no new orders", reason=reason)
    else:
        if not ps.kill_switch:
            raise PaperError("kill switch is not active")
        ps.kill_switch, ps.status = False, "PAUSED"
        ps.pause_reason = f"kill switch released by {by}; start the session to resume"
        engine.emit(s, ps, "warn", "risk", "kill_switch_off", f"kill switch released by {by}", reason=reason)
    audit.record(s, by, "paper_kill_switch", "paper_session", ps.id, active=active, reason=reason)
    return ps


def manual_order(s: Session, ps: PaperSession, body: OrderIn, by: str) -> PaperOrder:
    if ps.status not in ("RUNNING", "PAUSED"):
        raise PaperError(f"orders can be entered only in a running or paused session (it is {ps.status})")
    rt = engine.load_runtime(s, ps)
    if body.instrument not in rt.instr:
        raise PaperError(f"{body.instrument} is not an instrument of this session")
    if ps.clock_ts is None:
        raise PaperError("process at least one bar first: there is no last price to reference")
    book = engine.positions(s, ps)
    spec = rt.instr[body.instrument].spec
    units = body.lots * spec.multiplier * (1 if body.side == "buy" else -1)
    ref = engine._last_close(rt, ps.clock_ts)[body.instrument]
    n = s.query(PaperOrder).filter(PaperOrder.session_id == ps.id, PaperOrder.purpose == "manual").count()
    o = engine.place_order(s, ps, rt, book, sid="MANUAL", sym=body.instrument, delta_units=units,
                           order_type=body.order_type, purpose="manual", ref_price=ref, limit_price=body.limit_price,
                           stop_price=body.stop_price, tif_bars=body.tif_bars, seq=n + 1)
    audit.record(s, by, "paper_manual_order", "paper_session", ps.id, order_id=o.id if o else None)
    return o


def cancel_order(s: Session, ps: PaperSession, order_id: str, by: str) -> PaperOrder:
    o = s.get(PaperOrder, order_id)
    if o is None or o.session_id != ps.id:
        raise LookupError("order not found")
    engine.cancel(s, ps, o, f"cancelled by {by}")
    return o


def change_config(s: Session, ps: PaperSession, changes: dict[str, Any], by: str, reason: str) -> PaperSession:
    bad = sorted(set(changes) - set(MUTABLE_FIELDS))
    if bad:
        raise PaperError(f"these fields cannot change during a session: {bad} (criteria, strategies, data and "
                         "capital are fixed when the session is created)")
    base = {k: v for k, v in ps.config.items() if k != "resolved"}
    new = SessionConfig.model_validate({**base, **changes}).model_dump(mode="json")
    diff = {k: {"from": base.get(k), "to": new[k]} for k in changes if base.get(k) != new[k]}
    if not diff:
        raise PaperError("nothing changes")
    ps.config = {**new, "resolved": ps.config["resolved"]}
    ps.config_version += 1
    ps.config_hash = stable_hash(new)
    ps.config_history = [*ps.config_history, {"version": ps.config_version, "at": datetime.now(UTC).isoformat(),
                                              "by": by, "reason": reason, "diff": diff, "at_bar": str(ps.cursor_ts)}]
    engine.emit(s, ps, "warn", "config", "config_changed", f"configuration v{ps.config_version} by {by}: "
                + ", ".join(f"{k} {v['from']} -> {v['to']}" for k, v in diff.items()), reason=reason)
    audit.record(s, by, "paper_config_changed", "paper_session", ps.id, diff=diff, reason=reason)
    return ps


_CHILD_TABLES = (PaperEvent, PaperEquity, PaperSignal, PaperLedger, PaperFill, PaperOrder, PaperPosition)


def reset(s: Session, ps: PaperSession, by: str, confirm: bool) -> PaperSession:
    if not confirm:
        raise PaperError("reset must be confirmed explicitly")
    exp = s.get(Experiment, ps.experiment_id)
    exp.results = {**(exp.results or {}), "resets": [*(exp.results or {}).get("resets", []),
                                                     {"at": datetime.now(UTC).isoformat(), "by": by,
                                                      "summary_before": ps.summary}]}
    for t in _CHILD_TABLES:
        s.execute(delete(t).where(t.session_id == ps.id))
    s.execute(delete(RiskDecision).where(RiskDecision.run_id == ps.experiment_id))
    ps.status, ps.cursor_ts, ps.clock_ts, ps.bars_processed = "CREATED", None, None, 0
    ps.cash = ps.peak_equity = ps.day_start_equity = ps.initial_capital
    ps.day, ps.kill_switch, ps.pause_reason, ps.evaluation = None, False, None, {}
    ps.summary = {"equity": ps.initial_capital, "cash": ps.initial_capital, "return_net": 0.0}
    engine.emit(s, ps, "warn", "system", "session_reset", f"session reset by {by}; previous summary kept in the "
                "experiment record")
    audit.record(s, by, "paper_session_reset", "paper_session", ps.id)
    return ps


def delete_session(s: Session, ps: PaperSession, by: str, confirm: bool) -> None:
    if not confirm:
        raise PaperError("delete must be confirmed explicitly")
    exp = s.get(Experiment, ps.experiment_id)
    exp.results = {**(exp.results or {}), "deleted": {"at": datetime.now(UTC).isoformat(), "by": by},
                   "final_summary": ps.summary, "evaluation": ps.evaluation}
    exp.status = "succeeded" if ps.bars_processed else "failed"
    exp.finished_at = datetime.now(UTC)
    for t in _CHILD_TABLES:
        s.execute(delete(t).where(t.session_id == ps.id))
    ps.status = "DELETED"
    audit.record(s, by, "paper_session_deleted", "paper_session", ps.id, experiment_id=exp.id)


def store_evaluation(s: Session, ps: PaperSession, ev: dict[str, Any]) -> None:
    ps.evaluation = ev
    exp = s.get(Experiment, ps.experiment_id)
    exp.results = {**(exp.results or {}), "summary": ps.summary, "evaluation": {k: v for k, v in ev.items()
                                                                                 if k != "curves"}}
    if ps.status == "COMPLETED":
        exp.status, exp.finished_at = "succeeded", datetime.now(UTC)
    engine.emit(s, ps, "info", "evaluation", "evaluated", f"evaluation: {ev['verdict']} (not an approval)",
                verdict=ev["verdict"])


EXPORTS = {
    "orders": (PaperOrder, ["client_order_id", "strategy_id", "instrument", "side", "quantity", "lots", "order_type",
                            "limit_price", "stop_price", "purpose", "status", "created_ts", "reference_price",
                            "filled_quantity", "avg_fill_price", "last_fill_ts", "commission", "slippage_cost",
                            "reject_reason", "cancel_reason"], PaperOrder.created_ts),
    "fills": (PaperFill, ["order_id", "seq", "strategy_id", "instrument", "bar_ts", "side", "quantity", "price",
                          "reference_price", "fx_rate", "commission", "spread_cost", "slippage_cost", "rule"],
              PaperFill.bar_ts),
    "equity": (PaperEquity, ["bar_ts", "cash", "equity", "realized_pnl", "unrealized_pnl", "fees", "holding_costs",
                             "gross_exposure", "net_exposure", "margin_used", "drawdown"], PaperEquity.bar_ts),
    "ledger": (PaperLedger, ["ts", "kind", "amount", "strategy_id", "instrument", "ref_id", "note"], PaperLedger.ts),
    "events": (PaperEvent, ["seq", "sim_ts", "level", "category", "code", "message", "strategy_id", "instrument"],
               PaperEvent.seq),
    "signals": (PaperSignal, ["strategy_id", "bar_ts", "decided_at", "targets", "changed"], PaperSignal.bar_ts),
}


def export_csv(s: Session, ps: PaperSession, table: str) -> str:
    if table not in EXPORTS:
        raise PaperError(f"table must be one of {list(EXPORTS)}")
    model, cols, order = EXPORTS[table]
    buf = io.StringIO()
    buf.write(f"# {SIM_NOTE} session={ps.id} mode={ps.mode} synthetic={ps.is_synthetic} demo={ps.is_demo}\n")
    w = csv.writer(buf)
    w.writerow(cols)
    for r in s.scalars(select(model).where(model.session_id == ps.id).order_by(order)).all():
        w.writerow([getattr(r, c) for c in cols])
    return buf.getvalue()
