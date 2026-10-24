"""Positions, realized and unrealized P&L, currency conversion and margin.

The account is a margin account in ``account_currency``: cash changes only with realized P&L, commissions
and holding costs (financing, borrow, funding, rollover). Open positions are valued at the last close, and
that value is *unrealized* until a fill closes it. Unrealized P&L is never added to realized P&L or cash.

Currency: P&L is earned in the instrument's quote currency and converted at the rate of the moment (fill
or valuation). For FX the quote currency is the second leg of the pair; a pair whose quote is not the
account currency converts with its own price (base = account currency) or with another pair of the session.
Instruments without a known conversion are refused when the session is created, never converted with a
guessed rate.
"""

from __future__ import annotations

from dataclasses import dataclass

from qal.portfolio.risk import fx_legs


@dataclass
class Pos:
    quantity: float = 0.0
    avg_price: float = 0.0
    realized: float = 0.0  # account currency


def apply_fill(pos: Pos, signed_qty: float, price: float, fx: float) -> float:
    """Update the position with a fill and return the realized P&L (account currency) it produced."""
    q0, q1 = pos.quantity, pos.quantity + signed_qty
    realized = 0.0
    if q0 == 0 or (q0 > 0) == (signed_qty > 0):  # opening or adding
        pos.avg_price = (abs(q0) * pos.avg_price + abs(signed_qty) * price) / abs(q1) if q1 else 0.0
    else:  # reducing, closing or flipping
        closed = min(abs(signed_qty), abs(q0))
        realized = closed * (price - pos.avg_price) * (1 if q0 > 0 else -1) * fx
        if abs(q1) < 1e-12:
            q1, pos.avg_price = 0.0, 0.0
        elif (q1 > 0) != (q0 > 0):  # flipped: the remainder opens at the fill price
            pos.avg_price = price
    pos.quantity = q1
    pos.realized += realized
    return realized


def unrealized(pos: Pos, mark: float, fx: float) -> float:
    return pos.quantity * (mark - pos.avg_price) * fx


def quote_currency(symbol: str, asset_class: str, currency: str | None, account: str) -> tuple[str, bool]:
    """(quote currency, assumed?) of an instrument."""
    if asset_class == "fx":
        legs = fx_legs(symbol)
        if legs:
            return legs[1], False
    if currency:
        return currency.upper(), False
    return account, True


def conversion_plan(symbols: dict[str, dict[str, str | None]], account: str) -> tuple[dict[str, dict], list[str]]:
    """How each instrument's quote currency converts to the account currency, using only session prices.

    Returns (plan, problems). plan[symbol] = {"quote": ccy, "via": symbol or None, "invert": bool, "assumed": bool}.
    """
    plan, problems = {}, []
    pairs = {}
    for s, meta in symbols.items():
        if meta["asset_class"] == "fx" and fx_legs(s):
            pairs[fx_legs(s)] = s
    for s, meta in symbols.items():
        quote, assumed = quote_currency(s, str(meta["asset_class"]), meta.get("currency"), account)
        if quote == account:
            plan[s] = {"quote": quote, "via": None, "invert": False, "assumed": assumed}
        elif (quote, account) in pairs:
            plan[s] = {"quote": quote, "via": pairs[(quote, account)], "invert": False, "assumed": False}
        elif (account, quote) in pairs:
            plan[s] = {"quote": quote, "via": pairs[(account, quote)], "invert": True, "assumed": False}
        else:
            problems.append(f"{s}: no {quote}/{account} rate among the session's instruments; add the pair")
    return plan, problems


def fx_rate(plan: dict[str, dict], symbol: str, last: dict[str, float]) -> float | None:
    p = plan[symbol]
    if p["via"] is None:
        return 1.0
    px = last.get(p["via"])
    if not px:
        return None
    return 1.0 / px if p["invert"] else px
