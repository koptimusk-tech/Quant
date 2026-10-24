"""Execution simulator: decides whether and at what price a simulated order fills on one OHLCV bar.

Assumptions (OHLCV bars do not show the path of prices inside the bar, so every rule is conservative):

* An order is never filled on the bar whose close was used to decide it. It becomes active on the first bar
  that *starts* at or after the decision time (the bar's ``available_ts``), plus ``delay_bars`` more bars.
* Bars are mid prices. Buying pays half the spread above mid, selling receives half the spread below mid.
* **Market**: fills at the open, plus half spread, fixed slippage and range slippage (a fraction of the
  previous bar's high-low range, known before the bar opens).
* **Limit buy**: fills only if the ask went strictly below the limit (``low * (1 + half spread) < limit``);
  touching the limit is not a fill. Price: the better of the open ask and the limit. Sells mirror this.
* **Stop buy**: triggers if ``high * (1 + half spread) >= stop``. Price: ``max(open, stop)`` plus half spread
  and slippage, so a gap through the stop fills at the (worse) open. A stop limits the *intended* loss only.
* **Stop-limit buy**: triggers like a stop. If it triggers at the open (gap) and the open ask is within the
  limit it fills at the open; if it triggers inside the bar it does **not** fill on that bar (the path after
  the trigger is unknown) and works as a limit order from the next bar.
* **Partial fills**: with traded volume, a bar fills at most ``max_participation`` x volume (in units).
  Without usable volume the order fills completely and the fill says liquidity was not evaluated.
* Quantities are rounded down to the contract's lot step; prices are not rounded to ticks for fills (the
  simulated price is an estimate, rounding it would add false precision).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from qal.research.costs import CostModel

COST_SCENARIOS = {"base": 1.0, "adverse": 2.0, "severe": 4.0}


@dataclass
class Bar:
    ts: Any
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    prev_range: float  # (high - low) / close of the previous bar, 0 if unknown


@dataclass
class OrderView:
    side: str
    order_type: str
    remaining: float
    limit_price: float | None
    stop_price: float | None
    triggered: bool = False


@dataclass
class FillDecision:
    filled: float = 0.0
    price: float | None = None
    triggered: bool = False
    rule: str = ""
    spread_cost: float = 0.0  # in quote currency, for the filled quantity
    slippage_cost: float = 0.0
    notes: list[str] = field(default_factory=list)


def _round_down(q: float, step: float) -> float:
    return math.floor(q / step + 1e-9) * step


def simulate(order: OrderView, bar: Bar, cost: CostModel, *, lot_step_units: float,
             multiplier: float = 1.0) -> FillDecision:
    d = 1 if order.side == "buy" else -1
    hs = cost.half_spread_bps / 1e4
    slip = cost.slippage_bps / 1e4 + cost.slippage_range_frac * max(bar.prev_range, 0.0)
    out = FillDecision(triggered=order.triggered)
    px: float | None = None
    impact = 0.0  # slippage applied (fraction of price)

    if order.order_type == "market":
        px, impact, out.rule = bar.open, slip, "market: next open + half spread + slippage"
    elif order.order_type == "limit" or (order.order_type == "stop_limit" and order.triggered):
        lim = order.limit_price
        if d > 0 and bar.low * (1 + hs) < lim:
            px, out.rule = min(bar.open * (1 + hs), lim) / (1 + hs), "limit buy: ask traded below the limit"
        elif d < 0 and bar.high * (1 - hs) > lim:
            px, out.rule = max(bar.open * (1 - hs), lim) / (1 - hs), "limit sell: bid traded above the limit"
        else:
            out.rule = "limit not crossed (a touch is not a fill)"
    elif order.order_type in ("stop", "stop_limit"):
        stop = order.stop_price
        hit = bar.high * (1 + hs) >= stop if d > 0 else bar.low * (1 - hs) <= stop
        if not hit:
            out.rule = "stop not triggered"
        else:
            out.triggered = True
            gap = bar.open * (1 + hs) >= stop if d > 0 else bar.open * (1 - hs) <= stop
            if order.order_type == "stop":
                base = max(bar.open, stop / (1 + hs)) if d > 0 else min(bar.open, stop / (1 - hs))
                px, impact = base, slip
                out.rule = "stop: gapped through, filled at the open" if gap else "stop: triggered inside the bar"
            else:
                lim = order.limit_price
                open_ask = bar.open * (1 + d * hs)
                if gap and ((d > 0 and open_ask <= lim) or (d < 0 and open_ask >= lim)):
                    px, out.rule = bar.open, "stop-limit: triggered at the open within the limit"
                else:
                    out.rule = ("stop-limit: triggered inside the bar; works as a limit from the next bar "
                                "(intrabar path unknown)")
    if px is None:
        return out

    qty = order.remaining
    if bar.volume is not None and math.isfinite(bar.volume) and bar.volume > 0:
        cap = _round_down(cost.max_participation * bar.volume * multiplier, lot_step_units)
        if qty > cap + 1e-12:
            out.notes.append(f"partial: {cost.max_participation:.0%} of bar volume")
            qty = cap
    else:
        out.notes.append("no usable volume: liquidity not evaluated, filled in full")
    if qty <= 0:
        out.notes.append("bar volume too small for one lot")
        return out
    fill = px * (1 + d * (hs + impact))
    out.filled, out.price = qty, fill
    out.spread_cost = qty * px * hs
    out.slippage_cost = qty * px * impact
    return out
