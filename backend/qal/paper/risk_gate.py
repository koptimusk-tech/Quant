"""Pre-trade risk gate for simulated orders (deterministic; no language model involved).

Every order, from a strategy signal or entered by hand, passes the same checks in a fixed order. Limits come
from the active, versioned risk policy of the Hierarchical Risk Manager (Phase 6); this module only reads
them. Exposure limits *reduce* an order to what the limit allows (recorded as blocked + action); the
other checks reject it. Orders that only reduce an existing position skip the exposure, loss and drawdown
checks (cutting risk is always allowed), but never the kill switch or the session state checks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from qal.portfolio.risk import fx_legs
from qal.portfolio.risk_manager import PolicyLimits
from qal.portfolio.strategies import ELIGIBLE_STATUSES


@dataclass
class GateContext:
    as_of: str
    session_status: str
    kill_switch: bool
    reconciliation_ok: bool
    strategy_status: str  # hypothesis status, or DEMO
    allow_demo: bool
    data_status: str  # pass, warn, fail (dataset and last bar checks)
    data_age_hours: float
    max_staleness_hours: float
    equity: float
    day_start_equity: float
    peak_equity: float
    margin_used: float
    positions: dict[str, float]  # instrument -> signed notional (account ccy), all strategies
    asset_class: dict[str, str]
    is_spot_crypto: bool
    prev_dollar_volume: float | None  # account currency
    max_participation: float
    max_leverage_contract: float


@dataclass
class GateResult:
    approved: bool
    quantity: float  # possibly reduced, units
    checks: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""


def evaluate(*, instrument: str, current_units: float, delta_units: float, price_acct: float, lot_units: float,
             min_units: float, ctx: GateContext, limits: PolicyLimits) -> GateResult:
    """``price_acct`` is the value of one unit in the account currency (price x fx)."""
    P, G = limits.portfolio, limits.group
    checks: list[dict[str, Any]] = []

    def chk(rule: str, observed: Any, limit: Any, ok: bool, reason: str, action: str = "") -> bool:
        checks.append({"rule": rule, "observed": observed, "limit": limit, "decision": "approved" if ok else "blocked",
                       "reason": reason, "action": action})
        return ok

    def reject(reason: str) -> GateResult:
        return GateResult(False, 0.0, checks, reason)

    if not chk("kill_switch", ctx.kill_switch, False, not ctx.kill_switch,
               "kill switch active: no new orders" if ctx.kill_switch else "kill switch off"):
        return reject("kill switch active")
    if not chk("system_status", ctx.session_status, "RUNNING", ctx.session_status == "RUNNING" and ctx.reconciliation_ok,
               "session running and reconciled" if ctx.session_status == "RUNNING" and ctx.reconciliation_ok
               else "session not running or reconciliation failed"):
        return reject("system not in a tradable state")
    demo = ctx.strategy_status == "DEMO"
    valid = ctx.strategy_status in ELIGIBLE_STATUSES or ctx.strategy_status == "MANUAL" or (demo and ctx.allow_demo)
    if not chk("validation_status", ctx.strategy_status, list(ELIGIBLE_STATUSES), valid,
               "UNVALIDATED DEMO strategy, allowed only to exercise the simulator" if demo and valid
               else "strategy validated" if valid else f"strategy status {ctx.strategy_status} is not eligible"):
        return reject(f"strategy status {ctx.strategy_status} is not eligible")
    if not chk("data_quality", ctx.data_status, "not fail", ctx.data_status != "fail",
               "data quality acceptable" if ctx.data_status != "fail" else "data quality failed"):
        return reject("data quality failed")
    fresh = ctx.data_age_hours <= ctx.max_staleness_hours
    if not chk("data_freshness_hours", round(ctx.data_age_hours, 2), ctx.max_staleness_hours, fresh,
               "data current" if fresh else "last bar too old to trade on"):
        return reject("stale data")

    new_units = current_units + delta_units
    reducing = abs(new_units) < abs(current_units) - 1e-12 and (new_units == 0 or (new_units > 0) == (current_units > 0))

    if ctx.is_spot_crypto and new_units < -1e-12:
        chk("spot_no_short", new_units, ">= 0", False, "spot crypto cannot be sold short", "rejected")
        return reject("spot crypto cannot be sold short")

    if not reducing:
        dd = ctx.equity / ctx.peak_equity - 1 if ctx.peak_equity > 0 else 0.0
        if not chk("drawdown", round(dd, 6), -P.max_drawdown, dd > -P.max_drawdown,
                   "within drawdown limit" if dd > -P.max_drawdown else "session drawdown beyond the limit: only "
                   "orders that reduce risk are accepted"):
            return reject("drawdown limit")
        day = ctx.equity / ctx.day_start_equity - 1 if ctx.day_start_equity > 0 else 0.0
        if not chk("daily_loss", round(day, 6), -P.max_daily_loss, day > -P.max_daily_loss,
                   "within daily loss limit" if day > -P.max_daily_loss else "daily loss limit reached"):
            return reject("daily loss limit")

        def cap_to(rule: str, observed: float, limit: float, allowed_abs_units: float, reason: str) -> None:
            nonlocal delta_units, new_units
            target = math.copysign(min(abs(new_units), max(allowed_abs_units, 0.0)), new_units)
            target = math.copysign(math.floor(abs(target) / lot_units + 1e-9) * lot_units, target)
            same_side = current_units and (new_units > 0) == (current_units > 0)
            if same_side and abs(target) < abs(current_units):
                target = current_units  # a cap never turns a buy into a sell (or vice versa): no trade instead
            chk(rule, round(observed, 6), limit, False, reason, f"reduced {new_units:g} -> {target:g} units")
            delta_units, new_units = target - current_units, target

        eq = max(ctx.equity, 1e-9)
        others = {k: v for k, v in ctx.positions.items() if k != instrument}
        inst_expo = abs(new_units * price_acct) / eq
        if inst_expo > G.max_instrument_gross + 1e-9:
            cap_to("max_instrument_gross", inst_expo, G.max_instrument_gross,
                   G.max_instrument_gross * eq / price_acct, "position above the per-instrument limit")
        else:
            chk("max_instrument_gross", round(inst_expo, 6), G.max_instrument_gross, True, "within limit")
        gross_others = sum(abs(v) for v in others.values())
        gross = (gross_others + abs(new_units * price_acct)) / eq
        if gross > P.max_gross_exposure + 1e-9:
            cap_to("max_gross_exposure", gross, P.max_gross_exposure,
                   (P.max_gross_exposure * eq - gross_others) / price_acct, "portfolio gross exposure above the limit")
        else:
            chk("max_gross_exposure", round(gross, 6), P.max_gross_exposure, True, "within limit")
        net_others = sum(others.values())
        net = (net_others + new_units * price_acct) / eq
        if abs(net) > P.max_net_exposure + 1e-9:
            room = (P.max_net_exposure * eq - math.copysign(net_others, new_units)) / price_acct
            cap_to("max_net_exposure", abs(net), P.max_net_exposure, room, "portfolio net exposure above the limit")
        else:
            chk("max_net_exposure", round(abs(net), 6), P.max_net_exposure, True, "within limit")
        legs = fx_legs(instrument) if ctx.asset_class.get(instrument) == "fx" else None
        if legs:
            for ccy, sgn in ((legs[0], 1.0), (legs[1], -1.0)):
                other_ccy = sum(v * (1 if fx_legs(k) and fx_legs(k)[0] == ccy else -1 if fx_legs(k) and fx_legs(k)[1] == ccy
                                     else 0) for k, v in others.items() if ctx.asset_class.get(k) == "fx")
                c_net = (other_ccy + sgn * new_units * price_acct) / eq
                if abs(c_net) > G.max_currency_net + 1e-9:
                    room = (G.max_currency_net * eq - math.copysign(other_ccy, sgn * new_units)) / price_acct
                    cap_to(f"max_currency_net:{ccy}", abs(c_net), G.max_currency_net, room,
                           f"net {ccy} exposure above the limit")
        lev_limit = min(P.max_leverage, ctx.max_leverage_contract if not ctx.is_spot_crypto else 1.0)
        lev = (gross_others + abs(new_units * price_acct)) / eq
        if lev > lev_limit + 1e-9:
            cap_to("max_leverage", lev, lev_limit, (lev_limit * eq - gross_others) / price_acct, "leverage above the limit")
        else:
            chk("max_leverage", round(lev, 6), lev_limit, True, "within limit")
        traded = abs(new_units - current_units) * price_acct
        if ctx.prev_dollar_volume:
            cap_notional = ctx.max_participation * ctx.prev_dollar_volume
            if traded > cap_notional + 1e-9:
                allowed = abs(current_units) + cap_notional / price_acct
                cap_to("liquidity", traded / ctx.prev_dollar_volume, ctx.max_participation, allowed,
                       "order above the participation limit of the previous bar's traded value")
            else:
                chk("liquidity", round(traded / ctx.prev_dollar_volume, 6), ctx.max_participation, True, "within limit")
        else:
            chk("liquidity", None, ctx.max_participation, True, "no usable traded volume: liquidity not evaluable")
        margin_needed = abs(delta_units) * price_acct / max(ctx.max_leverage_contract, 1e-9)
        free = ctx.equity - ctx.margin_used
        if not chk("margin", round(margin_needed, 2), round(free, 2), margin_needed <= free + 1e-6,
                   "enough free margin" if margin_needed <= free + 1e-6 else "not enough free margin"):
            return reject("not enough free margin")
    else:
        checks.append({"rule": "risk_reducing", "observed": f"{current_units:g} -> {new_units:g}", "limit": "",
                       "decision": "approved", "reason": "order only reduces the position: exposure and loss limits "
                       "do not apply", "action": ""})

    if abs(delta_units) < min_units - 1e-12 or abs(delta_units) < 1e-12:
        chk("min_lot", abs(delta_units), min_units, False, "quantity below the minimum lot after limits", "rejected")
        return reject("below minimum lot")
    return GateResult(True, delta_units, checks, "approved")
