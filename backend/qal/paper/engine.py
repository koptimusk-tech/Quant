"""Paper trading engine: processes a session one bar at a time.

Order of operations for bar T (every step runs in one database transaction, so a crash leaves the session
at the previous bar and the step can simply be repeated; client order ids make it idempotent):

1. Data feed: take the bars that start at T. Check them (feed.check_bar); a failure pauses the session
   before the bar is used.
2. Execution: open orders that became active before T started are tried against bar T (execution.simulate).
3. Accounting: fills update positions and cash; positions are valued at T's close (unrealized P&L) and
   holding costs (financing, borrow, funding, rollover) are charged.
4. Signals: each strategy decides target weights at T's close using only bars available at that moment.
5. Orders: the change from current to target position becomes an order, after the pre-trade risk gate.
   It cannot fill before the next bar.
6. Reconciliation: positions, cash, fees and order fills are rebuilt from the fill and cash ledgers and
   compared with the stored state; a mismatch pauses the session.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.models import (
    Hypothesis,
    PaperEquity,
    PaperEvent,
    PaperFill,
    PaperLedger,
    PaperOrder,
    PaperPosition,
    PaperSession,
    PaperSignal,
    Provider,
    RiskDecision,
    RiskPolicy,
)
from qal.paper import accounting, execution, feed, orders, risk_gate
from qal.paper.config import SessionConfig
from qal.paper.signals import StrategyPlan, cost_model, decide
from qal.portfolio.risk_manager import PolicyLimits
from qal.portfolio.sizing import ContractSpec, contract_for
from qal.research.costs import CostModel, periods_per_year

ENGINE_VERSION = "paper@1.0.0"


# -- events --------------------------------------------------------------------------------------------
def emit(s: Session, ps: PaperSession, level: str, category: str, code: str, message: str, *,
         strategy_id: str | None = None, instrument: str | None = None, sim_ts: Any = None, **details: Any) -> None:
    seq = int((ps.summary or {}).get("event_seq", 0)) + 1
    ps.summary = {**(ps.summary or {}), "event_seq": seq}
    s.add(PaperEvent(session_id=ps.id, seq=seq, sim_ts=sim_ts if sim_ts is not None else ps.clock_ts, level=level,
                     category=category, code=code, message=message, strategy_id=strategy_id, instrument=instrument,
                     details=details))


def utc(x: Any) -> pd.Timestamp:
    """Database datetimes come back naive on SQLite: everything is compared as UTC timestamps."""
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _py(v: Any) -> Any:
    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    return v


# -- runtime -------------------------------------------------------------------------------------------
@dataclass
class InstrRT:
    info: feed.Instr
    frame: pd.DataFrame
    spec: ContractSpec
    is_spot_crypto: bool
    at: dict[Any, Any] = field(default_factory=dict)  # bar start -> row positions

    def upto(self, clock: Any) -> int:
        """Number of rows available at ``clock`` (available_ts <= clock); rows are in availability order."""
        return int(self.frame["available_ts"].searchsorted(utc(clock), "right"))

    def known(self, clock: Any) -> pd.DataFrame:
        return self.frame.iloc[:self.upto(clock)]


@dataclass
class Runtime:
    cfg: SessionConfig
    plans: list[StrategyPlan]
    instr: dict[str, InstrRT]  # symbol -> runtime
    by_dataset: dict[str, str]  # dataset id -> symbol
    conversion: dict[str, dict]
    limits: PolicyLimits
    policy: RiskPolicy
    costs: dict[tuple[str, str], CostModel] = field(default_factory=dict)  # (plan id, symbol) -> scaled model
    ts_index: dict[str, pd.Index] = field(default_factory=dict)

    def cost(self, plan_id: str, symbol: str) -> CostModel:
        return self.costs[(plan_id, symbol)]

    def timeline(self, after: pd.Timestamp | None) -> list[pd.Timestamp]:
        """Bar starts still to process (within start/end), in order."""
        if "all" not in self.ts_index:
            ts = pd.DatetimeIndex(sorted(set().union(*[set(r.frame["ts"]) for r in self.instr.values()])))
            if self.cfg.start:
                ts = ts[ts >= utc(self.cfg.start)]
            if self.cfg.end:
                ts = ts[ts <= utc(self.cfg.end)]
            self.ts_index["all"] = ts
        ts = self.ts_index["all"]
        if after is not None:
            ts = ts[ts.searchsorted(after, "right"):]
        return list(ts[:2])

    def rows_at(self, sym: str, t: pd.Timestamp) -> pd.DataFrame:
        ir = self.instr[sym]
        return ir.frame.iloc[ir.at.get(t, [])]

    def prev_row(self, sym: str, t: pd.Timestamp) -> pd.Series | None:
        f = self.instr[sym].frame
        i = int(f["ts"].searchsorted(t, "left"))
        return f.iloc[i - 1] if i > 0 else None


def _scenario(cfg: SessionConfig) -> float:
    return execution.COST_SCENARIOS[cfg.cost_scenario]


def load_runtime(s: Session, ps: PaperSession) -> Runtime:
    cfg = SessionConfig.model_validate({k: v for k, v in ps.config.items() if k != "resolved"})
    res = ps.config["resolved"]
    plans = [StrategyPlan(**p) for p in res["plans"]]
    instr: dict[str, InstrRT] = {}
    by_ds: dict[str, str] = {}
    for ref in ps.data_refs:
        info = feed.instruments_for(s, [ref["dataset_id"]])[0]
        frame, info = feed.load(s, info, ps.mode)
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
        spec = contract_for(info.metadata, info.asset_class, info.symbol)
        spot = info.asset_class == "crypto" and (info.metadata or {}).get("contract_type", "spot") == "spot"
        frame["available_ts"] = pd.to_datetime(frame["available_ts"], utc=True)
        instr[info.symbol] = InstrRT(info, frame, spec, spot, frame.groupby("ts").indices)
        by_ds[ref["dataset_id"]] = info.symbol
    policy = s.get(RiskPolicy, ps.policy_id)
    rt = Runtime(cfg, plans, instr, by_ds, res["conversion"], PolicyLimits.model_validate(policy.limits), policy)
    m = _scenario(cfg)
    for p in plans:
        for d in p.dataset_ids:
            sym = by_ds[d]
            base = cfg.cost_overrides.get(d) or cost_model(p, d, instr[sym].info.asset_class)
            rt.costs[(p.id, sym)] = base.scaled(m) if m != 1 else base
    return rt


# -- state helpers -------------------------------------------------------------------------------------
def positions(s: Session, ps: PaperSession) -> dict[tuple[str, str], PaperPosition]:
    return {(p.strategy_id, p.instrument): p for p in
            s.scalars(select(PaperPosition).where(PaperPosition.session_id == ps.id)).all()}


def _pos(s: Session, ps: PaperSession, book: dict, sid: str, sym: str) -> PaperPosition:
    if (sid, sym) not in book:
        p = PaperPosition(session_id=ps.id, strategy_id=sid, instrument=sym, quantity=0.0, avg_price=0.0,
                          realized_pnl=0.0, fees=0.0, holding_costs=0.0, fx_rate=1.0, unrealized_pnl=0.0)
        s.add(p)
        book[(sid, sym)] = p
    return book[(sid, sym)]


def open_orders(s: Session, ps: PaperSession) -> list[PaperOrder]:
    return s.scalars(select(PaperOrder).where(PaperOrder.session_id == ps.id,
                                              PaperOrder.status.in_(orders.OPEN_STATES))
                     .order_by(PaperOrder.created_ts, PaperOrder.client_order_id)).all()


def _ledger(s: Session, ps: PaperSession, ts: Any, kind: str, amount: float, *, sid: str | None = None,
            sym: str | None = None, ref: str | None = None, note: str = "") -> None:
    if amount == 0:
        return
    s.add(PaperLedger(session_id=ps.id, ts=ts, kind=kind, amount=float(amount), strategy_id=sid, instrument=sym,
                      ref_id=ref, note=note))
    ps.cash = float(ps.cash + amount)


def equity_of(ps: PaperSession, book: dict) -> float:
    return float(ps.cash + sum(p.unrealized_pnl for p in book.values()))


def notional_by_instrument(book: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for (_, sym), p in book.items():
        if p.quantity and p.last_price:
            out[sym] = out.get(sym, 0.0) + p.quantity * p.last_price * p.fx_rate
    return out


def margin_used(rt: Runtime, book: dict) -> float:
    return float(sum(abs(p.quantity * (p.last_price or 0) * p.fx_rate) / rt.instr[p.instrument].spec.max_leverage
                     for p in book.values() if p.instrument in rt.instr and p.quantity))


def _strategy_status(s: Session, rt: Runtime, sid: str) -> str:
    if sid == "MANUAL":
        return "MANUAL"
    plan = next((p for p in rt.plans if p.id == sid), None)
    if plan is None:
        return "UNKNOWN"
    if plan.kind == "demo":
        return "DEMO"
    if plan.kind == "candidate":  # re-checked at every order: an altered record stops trading
        from qal.models import Experiment
        from qal.program import registry

        rec = s.get(Experiment, plan.id[2:])
        return registry.admission_status(rec) if rec is not None else "UNKNOWN"
    h = s.get(Hypothesis, plan.hypothesis_id)
    return h.status if h else "UNKNOWN"


def _store_decisions(s: Session, ps: PaperSession, rt: Runtime, scope: str, as_of: str,
                     res: risk_gate.GateResult) -> None:
    rows = [c for c in res.checks if c["decision"] == "blocked"]
    for c in rows:
        s.add(RiskDecision(run_id=ps.experiment_id, policy_id=rt.policy.id, policy_version=ps.policy_version,
                           level="order", scope=scope, rule=c["rule"], observed=None if c["observed"] is None
                           else str(c["observed"]), limit_value=str(c["limit"]), decision="blocked",
                           action=c.get("action", ""), reason=c["reason"], as_of=as_of))
    passed = [c["rule"] for c in res.checks if c["decision"] == "approved"]
    s.add(RiskDecision(run_id=ps.experiment_id, policy_id=rt.policy.id, policy_version=ps.policy_version,
                       level="order", scope=scope, rule="pre_trade", observed=f"{len(passed)} checks passed",
                       limit_value=",".join(passed)[:2000], decision="approved" if res.approved else "blocked",
                       action="" if res.approved else "order rejected", reason=res.reason, as_of=as_of))


# -- order creation (strategy, protective stop or manual) -----------------------------------------------
def place_order(s: Session, ps: PaperSession, rt: Runtime, book: dict, *, sid: str, sym: str, delta_units: float,
                order_type: str, purpose: str, ref_price: float, limit_price: float | None = None,
                stop_price: float | None = None, tif_bars: int | None = None, seq: int = 0,
                data_status: str = "pass", data_age_hours: float = 0.0) -> PaperOrder | None:
    ir = rt.instr[sym]
    decided_at = utc(ps.clock_ts)
    coid = orders.client_order_id(ps.id, sid, sym, decided_at.isoformat(), purpose, seq)
    if s.scalar(select(PaperOrder.id).where(PaperOrder.client_order_id == coid)):
        return None  # already created for this decision (idempotent re-run)
    fx = accounting.fx_rate(rt.conversion, sym, _last_close(rt, decided_at)) or 1.0
    pos = book.get((sid, sym))
    cur = pos.quantity if pos else 0.0
    eq = equity_of(ps, book)
    prev = ir.known(decided_at).tail(1)
    dvol = None
    if len(prev) and np.isfinite(prev["volume"].iloc[0]) and prev["volume"].iloc[0] > 0:
        dvol = float(prev["close"].iloc[0] * prev["volume"].iloc[0] * ir.spec.multiplier * fx)
    last_avail = pd.Timestamp(prev["available_ts"].iloc[0]) if len(prev) else None
    age = data_age_hours if last_avail is None else max(data_age_hours, (decided_at - last_avail)
                                                        .total_seconds() / 3600)
    cm = rt.cost(sid, sym) if (sid, sym) in rt.costs else next(iter(c for (p, y), c in rt.costs.items() if y == sym))
    ctx = risk_gate.GateContext(
        as_of=str(decided_at), session_status=ps.status, kill_switch=ps.kill_switch,
        reconciliation_ok=not (ps.summary or {}).get("reconciliation_failed", False),
        strategy_status=_strategy_status(s, rt, sid), allow_demo=rt.cfg.allow_unvalidated_demo,
        data_status="fail" if ir.info.quality_status == "fail" else data_status, data_age_hours=age,
        max_staleness_hours=rt.cfg.max_staleness_hours if ps.mode == "new_data" else 1e9,
        equity=eq, day_start_equity=ps.day_start_equity, peak_equity=ps.peak_equity, margin_used=margin_used(rt, book),
        positions=notional_by_instrument(book), asset_class={k: v.info.asset_class for k, v in rt.instr.items()},
        is_spot_crypto=ir.is_spot_crypto, prev_dollar_volume=dvol, max_participation=cm.max_participation,
        max_leverage_contract=ir.spec.max_leverage)
    lot_units = ir.spec.lot_size * ir.spec.multiplier
    res = risk_gate.evaluate(instrument=sym, current_units=cur, delta_units=delta_units, price_acct=ref_price * fx,
                             lot_units=lot_units, min_units=ir.spec.min_lot * ir.spec.multiplier, ctx=ctx,
                             limits=rt.limits)
    qty = abs(res.quantity) if res.approved else abs(delta_units)
    side = "buy" if (res.quantity if res.approved else delta_units) > 0 else "sell"
    o = PaperOrder(session_id=ps.id, client_order_id=coid, strategy_id=sid, instrument=sym, side=side,
                   quantity=float(qty), lots=float(qty / ir.spec.multiplier), order_type=order_type,
                   limit_price=limit_price, stop_price=stop_price, tif_bars=tif_bars, delay_bars=rt.cfg.delay_bars,
                   purpose=purpose, status="CREATED", created_ts=decided_at.to_pydatetime(), reference_price=float(ref_price),
                   details={"risk_checks": res.checks, "requested_units": float(delta_units), "fx_at_decision": fx})
    s.add(o)
    s.flush()
    scope = f"{sid}/{sym}"
    _store_decisions(s, ps, rt, scope, str(decided_at), res)
    if res.approved:
        orders.transition(o, "PENDING")
        capped = [c for c in res.checks if c["decision"] == "blocked"]
        if capped:
            emit(s, ps, "warn", "risk", "order_reduced", f"{side} {sym} reduced by risk limits: "
                 + "; ".join(c["reason"] for c in capped), strategy_id=sid, instrument=sym, order_id=o.id)
        emit(s, ps, "info", "order", "order_created", f"{purpose} {order_type} {side} {qty:g} {sym}",
             strategy_id=sid, instrument=sym, order_id=o.id)
    else:
        orders.transition(o, "REJECTED", res.reason)
        emit(s, ps, "warn", "risk", "order_rejected", f"{side} {sym} rejected: {res.reason}", strategy_id=sid,
             instrument=sym, order_id=o.id)
    return o


def _last_close(rt: Runtime, clock: Any) -> dict[str, float]:
    out = {}
    for sym, ir in rt.instr.items():
        k = ir.upto(clock)
        if k:
            out[sym] = float(ir.frame["close"].iloc[k - 1])
    return out


def cancel(s: Session, ps: PaperSession, o: PaperOrder, reason: str) -> None:
    orders.transition(o, "CANCELLED", reason)
    emit(s, ps, "info", "order", "order_cancelled", f"{o.purpose} {o.side} {o.instrument}: {reason}",
         strategy_id=o.strategy_id, instrument=o.instrument, order_id=o.id)


# -- the step ------------------------------------------------------------------------------------------
def pause(s: Session, ps: PaperSession, reason: str, *, level: str = "error", category: str = "system",
          code: str = "paused", **details: Any) -> None:
    ps.status, ps.pause_reason = "PAUSED", reason
    emit(s, ps, level, category, code, f"session paused: {reason}", **details)


def step(s: Session, ps: PaperSession, rt: Runtime, *, now: datetime | None = None,
         skip_bad_bar: bool = False) -> dict[str, Any]:
    if ps.status != "RUNNING":
        return {"processed": False, "reason": f"session is {ps.status}"}
    pending = rt.timeline(utc(ps.cursor_ts) if ps.cursor_ts else None)
    if not pending:
        if ps.mode == "replay":
            for o in open_orders(s, ps):
                cancel(s, ps, o, "replay finished")
            ps.status = "COMPLETED"
            emit(s, ps, "info", "system", "replay_finished", "historical replay reached the last bar")
            return {"processed": False, "reason": "replay finished"}
        last = max(pd.Timestamp(r.frame["available_ts"].iloc[-1]) for r in rt.instr.values())
        chk = feed.stale_feed_check(last, now or feed.utcnow(), rt.cfg.max_staleness_hours)
        for i in chk.issues:
            emit(s, ps, "warn", "data", i["check"], i["message"])
        return {"processed": False, "reason": "waiting for new data"}
    T = pending[0]
    bars: dict[str, pd.Series] = {}
    for sym in rt.instr:
        rows = rt.rows_at(sym, T)
        if len(rows):
            bars[sym] = rows.iloc[-1]
    clock = max(pd.Timestamp(b["available_ts"]) for b in bars.values())

    # 1. data checks
    data_status: dict[str, str] = {}
    for sym, b in bars.items():
        ir = rt.instr[sym]
        prov = s.get(Provider, ir.info.provider_id) if ps.mode == "new_data" else None
        chk = feed.check_bar(rt.rows_at(sym, T), rt.prev_row(sym, T), ir.info, mode=ps.mode, now=now or feed.utcnow(),
                             max_gap_bars=rt.cfg.max_gap_bars, max_delay_hours=rt.cfg.max_delay_hours,
                             max_staleness_hours=rt.cfg.max_staleness_hours, provider=prov)
        data_status[sym] = chk.status
        for i in chk.issues:
            emit(s, ps, "error" if i["severity"] == "fail" else "warn", "data", i["check"], f"{sym} {T}: {i['message']}",
                 instrument=sym, sim_ts=clock, **{k: v for k, v in i.items() if k not in ("check", "message")})
        if chk.status == "fail" and not skip_bad_bar:
            pause(s, ps, f"data quality failure on {sym} at {T}", category="data", code="data_quality_pause",
                  instrument=sym)
            return {"processed": False, "reason": ps.pause_reason}
    if skip_bad_bar and any(v == "fail" for v in data_status.values()):
        emit(s, ps, "warn", "data", "bar_skipped", f"bar {T} skipped by a person after a data failure", sim_ts=clock)
        ps.cursor_ts = T.to_pydatetime()
        return {"processed": True, "skipped": True, "bar": str(T)}

    book = positions(s, ps)
    if (d := str(T.date())) != ps.day:
        ps.day, ps.day_start_equity = d, equity_of(ps, book)

    # 2. execution against bar T
    opens = {sym: float(b["open"]) for sym, b in bars.items()}
    last_known = {**(_last_close(rt, ps.clock_ts) if ps.clock_ts else {}), **opens}
    for o in open_orders(s, ps):
        b = bars.get(o.instrument)
        if b is None or T < utc(o.created_ts):
            continue  # no bar for the instrument, or the bar started before the decision was made
        o.bars_active += 1
        if o.bars_active <= o.delay_bars:
            continue
        _execute(s, ps, rt, book, o, b, T, last_known)
        if o.status in orders.OPEN_STATES and o.tif_bars and o.bars_active - o.delay_bars >= o.tif_bars:
            orders.transition(o, "EXPIRED", f"not filled within {o.tif_bars} bar(s)")
            emit(s, ps, "info", "order", "order_expired", f"{o.order_type} {o.side} {o.instrument} expired "
                 f"({o.filled_quantity:g}/{o.quantity:g} filled)", strategy_id=o.strategy_id, instrument=o.instrument,
                 sim_ts=clock, order_id=o.id)
    s.flush()

    # 3. valuation at T's close and holding costs
    ps.clock_ts = clock.to_pydatetime()
    closes = _last_close(rt, clock)
    for (sid, sym), p in book.items():
        if sym not in closes:
            continue
        fx = accounting.fx_rate(rt.conversion, sym, closes) or p.fx_rate
        p.last_price, p.fx_rate = closes[sym], fx
        pp = accounting.Pos(p.quantity, p.avg_price, p.realized_pnl)
        p.unrealized_pnl = accounting.unrealized(pp, closes[sym], fx)
        p.updated_ts = clock.to_pydatetime()
        if p.quantity and sym in bars:
            _holding_costs(s, ps, rt, p, sid, sym, closes[sym] * fx, clock)
    eq = equity_of(ps, book)
    ps.peak_equity = max(ps.peak_equity, eq)
    expo = notional_by_instrument(book)
    s.add(PaperEquity(session_id=ps.id, bar_ts=T.to_pydatetime(), cash=ps.cash, equity=eq,
                      realized_pnl=sum(p.realized_pnl for p in book.values()),
                      unrealized_pnl=sum(p.unrealized_pnl for p in book.values()),
                      fees=sum(p.fees for p in book.values()), holding_costs=sum(p.holding_costs for p in book.values()),
                      gross_exposure=sum(abs(v) for v in expo.values()) / eq if eq else 0.0,
                      net_exposure=sum(expo.values()) / eq if eq else 0.0, margin_used=margin_used(rt, book),
                      drawdown=eq / ps.peak_equity - 1 if ps.peak_equity else 0.0))

    # 4-5. signals and orders
    stopped = dict((ps.summary or {}).get("stopped_out", {}))
    for plan in rt.plans:
        syms = [rt.by_dataset[d] for d in plan.dataset_ids]
        if any(sym not in bars for sym in syms):
            continue
        frames = [rt.instr[sym].known(clock) for sym in syms]
        w = decide(plan, frames, rt.cfg.history_bars)
        if w is None:
            continue
        targets = {sym: w[j] for j, sym in enumerate(syms)}
        prev_sig = s.scalar(select(PaperSignal).where(PaperSignal.session_id == ps.id, PaperSignal.strategy_id == plan.id)
                            .order_by(PaperSignal.bar_ts.desc()).limit(1))
        changed = prev_sig is None or any(abs(prev_sig.targets.get(k, 0.0) - v) > 1e-12 for k, v in targets.items())
        s.add(PaperSignal(session_id=ps.id, strategy_id=plan.id, bar_ts=T.to_pydatetime(), decided_at=clock.to_pydatetime(),
                          targets=targets, changed=changed))
        if changed:
            emit(s, ps, "info", "signal", "signal_changed", f"{plan.id}: " + ", ".join(f"{k} {v:+.3f}" for k, v in
                                                                                     targets.items()), strategy_id=plan.id)
        for sym in syms:
            key = f"{plan.id}|{sym}"
            tw = targets[sym]
            if key in stopped:
                if np.sign(tw) == stopped[key]:
                    tw = 0.0  # stopped out: no re-entry until the signal changes direction
                else:
                    stopped.pop(key)
            _rebalance(s, ps, rt, book, plan, sym, tw, closes[sym], data_status.get(sym, "pass"))
    ps.summary = {**(ps.summary or {}), "stopped_out": stopped}

    # 6. reconciliation
    from qal.paper.reconcile import reconcile

    s.flush()
    rec = reconcile(s, ps)
    if not rec["ok"]:
        ps.summary = {**ps.summary, "reconciliation_failed": True}
        pause(s, ps, "reconciliation mismatch", level="critical", category="reconcile", code="reconciliation_failed",
              mismatches=rec["mismatches"][:20])
    ps.cursor_ts = T.to_pydatetime()
    ps.bars_processed += 1
    ps.updated_at = feed.utcnow()
    ps.summary = {**ps.summary, **summary(ps, book, rt)}
    return {"processed": True, "bar": str(T), "equity": eq}


def _rebalance(s: Session, ps: PaperSession, rt: Runtime, book: dict, plan: StrategyPlan, sym: str, weight: float,
               close: float, data_status: str) -> None:
    ir = rt.instr[sym]
    fx = accounting.fx_rate(rt.conversion, sym, _last_close(rt, ps.clock_ts)) or 1.0
    eq = equity_of(ps, book)
    lot_units = ir.spec.lot_size * ir.spec.multiplier
    raw = weight * plan.allocation * eq / (close * fx)
    target = math.copysign(math.floor(abs(raw) / lot_units + 1e-9) * lot_units, raw) if raw else 0.0
    pos = book.get((plan.id, sym))
    cur = pos.quantity if pos else 0.0
    delta = target - cur
    small = abs(delta * close * fx) < rt.cfg.rebalance_threshold * eq
    for o in open_orders(s, ps):
        if o.strategy_id == plan.id and o.instrument == sym and o.purpose == "rebalance":
            if small or abs(delta) < 1e-12:
                return  # the order in flight already moves towards the target
            cancel(s, ps, o, "replaced by a new decision")
    if abs(delta) < 1e-12 or (small and target != 0):
        return
    otype = rt.cfg.order_type
    limit = None
    if otype == "limit":
        off = rt.cfg.limit_offset_bps / 1e4
        limit = close * (1 - off) if delta > 0 else close * (1 + off)
    place_order(s, ps, rt, book, sid=plan.id, sym=sym, delta_units=delta, order_type=otype, purpose="rebalance",
                ref_price=close, limit_price=limit, tif_bars=rt.cfg.order_ttl_bars, data_status=data_status)


def _execute(s: Session, ps: PaperSession, rt: Runtime, book: dict, o: PaperOrder, b: pd.Series, T: pd.Timestamp,
             last_known: dict[str, float]) -> None:
    ir = rt.instr[o.instrument]
    prev = rt.prev_row(o.instrument, T)
    prev_range = float((prev["high"] - prev["low"]) / prev["close"]) if prev is not None else 0.0
    vol = b["volume"]
    bar = execution.Bar(T, float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"]),
                        float(vol) if pd.notna(vol) else None, prev_range)
    cm = rt.cost(o.strategy_id, o.instrument) if (o.strategy_id, o.instrument) in rt.costs else next(
        c for (_, y), c in rt.costs.items() if y == o.instrument)
    view = execution.OrderView(o.side, o.order_type, orders.remaining(o), o.limit_price, o.stop_price, o.triggered)
    dec = execution.simulate(view, bar, cm, lot_step_units=ir.spec.lot_size * ir.spec.multiplier,
                             multiplier=ir.spec.multiplier)
    if dec.triggered and not o.triggered:
        o.triggered = True
        emit(s, ps, "info", "order", "stop_triggered", f"{o.order_type} {o.side} {o.instrument} triggered: {dec.rule}",
             strategy_id=o.strategy_id, instrument=o.instrument, sim_ts=b["available_ts"], order_id=o.id)
    if not dec.filled:
        return
    fx = accounting.fx_rate(rt.conversion, o.instrument, last_known) or 1.0
    notional = dec.filled * dec.price * fx
    commission = notional * cm.commission_bps / 1e4
    seq = s.scalar(select(PaperFill.seq).where(PaperFill.order_id == o.id).order_by(PaperFill.seq.desc()).limit(1))
    f = PaperFill(session_id=ps.id, order_id=o.id, seq=(seq or 0) + 1, strategy_id=o.strategy_id, instrument=o.instrument,
                  bar_ts=T.to_pydatetime(), side=o.side, quantity=float(dec.filled), price=float(dec.price),
                  reference_price=o.reference_price, fx_rate=float(fx), commission=float(commission),
                  spread_cost=float(dec.spread_cost * fx), slippage_cost=float(dec.slippage_cost * fx),
                  rule=dec.rule + ("; " + "; ".join(dec.notes) if dec.notes else ""))
    s.add(f)
    filled_before = o.filled_quantity
    o.filled_quantity = float(filled_before + dec.filled)
    o.avg_fill_price = float(((o.avg_fill_price or 0) * filled_before + dec.price * dec.filled) / o.filled_quantity)
    o.last_fill_ts = T.to_pydatetime()
    o.commission = float(o.commission + commission)
    o.slippage_cost = float(o.slippage_cost + f.slippage_cost)
    orders.transition(o, "FILLED" if orders.remaining(o) < 1e-9 else "PARTIALLY_FILLED")
    p = _pos(s, ps, book, o.strategy_id, o.instrument)
    pp = accounting.Pos(p.quantity, p.avg_price, p.realized_pnl)
    realized = accounting.apply_fill(pp, orders.direction(o.side) * dec.filled, dec.price, fx)
    p.quantity, p.avg_price, p.realized_pnl = pp.quantity, pp.avg_price, pp.realized
    p.fees = float(p.fees + commission)
    s.flush()
    _ledger(s, ps, T.to_pydatetime(), "realized_pnl", realized, sid=o.strategy_id, sym=o.instrument, ref=f.id)
    _ledger(s, ps, T.to_pydatetime(), "commission", -commission, sid=o.strategy_id, sym=o.instrument, ref=f.id)
    emit(s, ps, "info", "fill", "fill", f"{o.side} {dec.filled:g} {o.instrument} @ {dec.price:.6g} ({dec.rule})",
         strategy_id=o.strategy_id, instrument=o.instrument, sim_ts=b["available_ts"], order_id=o.id,
         slippage_vs_reference_bps=round(orders.direction(o.side) * (dec.price / o.reference_price - 1) * 1e4, 2))
    if o.purpose == "protective_stop":
        stopped = dict((ps.summary or {}).get("stopped_out", {}))
        stopped[f"{o.strategy_id}|{o.instrument}"] = -orders.direction(o.side)
        ps.summary = {**(ps.summary or {}), "stopped_out": stopped}
    _sync_stop(s, ps, rt, book, o.strategy_id, o.instrument, dec.price)


def _sync_stop(s: Session, ps: PaperSession, rt: Runtime, book: dict, sid: str, sym: str, ref: float) -> None:
    """Keep one protective stop per position, sized to the position (only when stop_loss_pct is set)."""
    pct = rt.cfg.stop_loss_pct
    p = book.get((sid, sym))
    q = p.quantity if p else 0.0
    for o in open_orders(s, ps):
        if o.strategy_id == sid and o.instrument == sym and o.purpose == "protective_stop":
            if q and abs(o.quantity - o.filled_quantity - abs(q)) < 1e-9 and orders.direction(o.side) == -np.sign(q):
                return
            cancel(s, ps, o, "position changed: protective stop replaced")
    if not pct or not q:
        return
    stop = p.avg_price * (1 - pct) if q > 0 else p.avg_price * (1 + pct)
    limit = None
    if rt.cfg.stop_order_type == "stop_limit":
        off = rt.cfg.stop_limit_offset_bps / 1e4
        limit = stop * (1 - off) if q > 0 else stop * (1 + off)
    n = s.query(PaperOrder).filter(PaperOrder.session_id == ps.id, PaperOrder.purpose == "protective_stop").count()
    place_order(s, ps, rt, book, sid=sid, sym=sym, delta_units=-q, order_type=rt.cfg.stop_order_type,
                purpose="protective_stop", ref_price=ref, stop_price=stop, limit_price=limit, tif_bars=None, seq=n + 1)


def _holding_costs(s: Session, ps: PaperSession, rt: Runtime, p: PaperPosition, sid: str, sym: str, px_acct: float,
                   clock: pd.Timestamp) -> None:
    ir = rt.instr[sym]
    cm = rt.cost(sid, sym) if (sid, sym) in rt.costs else CostModel()
    ppy = periods_per_year(ir.info.frequency, ir.info.asset_class)
    bars_per_day = ppy / (365.0 if ir.info.asset_class == "crypto" else 252.0)
    notional = p.quantity * px_acct
    rate = (cm.financing_bps_annual if notional > 0 else cm.borrow_bps_annual) / 1e4 / ppy
    cost = abs(notional) * rate
    kind = "financing" if notional > 0 else "borrow"
    funding = 0.0
    if ir.info.asset_class == "crypto" and not ir.is_spot_crypto:
        funding = abs(notional) * cm.funding_bps_daily / 1e4 / max(bars_per_day, 1e-9)
    roll = 0.0
    if cm.roll_every_bars and (ps.bars_processed + 1) % cm.roll_every_bars == 0:
        roll = abs(notional) * cm.rollover_bps_per_roll / 1e4
    ts = pd.Timestamp(p.updated_ts).to_pydatetime() if p.updated_ts else clock.to_pydatetime()
    for k, amt in ((kind, cost), ("funding", funding), ("rollover", roll)):
        if amt:
            _ledger(s, ps, ts, k, -amt, sid=sid, sym=sym)
            p.holding_costs = float(p.holding_costs + amt)


def summary(ps: PaperSession, book: dict, rt: Runtime) -> dict[str, Any]:
    eq = equity_of(ps, book)
    expo = notional_by_instrument(book)
    return {"equity": eq, "cash": ps.cash, "realized_pnl": sum(p.realized_pnl for p in book.values()),
            "unrealized_pnl": sum(p.unrealized_pnl for p in book.values()),
            "fees": sum(p.fees for p in book.values()), "holding_costs": sum(p.holding_costs for p in book.values()),
            "gross_exposure": sum(abs(v) for v in expo.values()) / eq if eq else 0.0,
            "net_exposure": sum(expo.values()) / eq if eq else 0.0, "margin_used": margin_used(rt, book),
            "drawdown": eq / ps.peak_equity - 1 if ps.peak_equity else 0.0,
            "return_net": eq / ps.initial_capital - 1}


def run(s: Session, ps: PaperSession, max_bars: int, *, now: datetime | None = None,
        skip_bad_bar: bool = False) -> dict[str, Any]:
    """Process up to max_bars bars, committing after each one (a crash loses at most the bar in progress)."""
    rt = load_runtime(s, ps)
    done, last = 0, {}
    for i in range(max_bars):
        last = step(s, ps, rt, now=now, skip_bad_bar=skip_bad_bar and i == 0)
        s.commit()
        if not last.get("processed"):
            break
        done += 1
    return {"bars": done, "status": ps.status, "last": last, "cursor": str(ps.cursor_ts) if ps.cursor_ts else None}
