"""Reconciliation: rebuild the session state from its ledgers and compare it with the stored state.

Sources of truth are the fills (positions, average prices, realized P&L, commissions) and the cash ledger
(cash). The stored positions, the session cash and every order's filled quantity must match what the ledgers
imply. Any difference is reported with the field, the stored value and the rebuilt value; the engine pauses
the session on a mismatch. The same rebuild restores state after a restart.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qal.models import PaperFill, PaperLedger, PaperOrder, PaperPosition, PaperSession
from qal.paper import accounting, orders


def _close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def rebuild(s: Session, ps: PaperSession) -> dict[str, Any]:
    fills = s.execute(select(PaperFill, PaperOrder.created_ts, PaperOrder.client_order_id)
                      .join(PaperOrder, PaperOrder.id == PaperFill.order_id)
                      .where(PaperFill.session_id == ps.id)
                      .order_by(PaperFill.bar_ts, PaperOrder.created_ts, PaperOrder.client_order_id, PaperFill.seq)).all()
    book: dict[tuple[str, str], accounting.Pos] = {}
    fees: dict[tuple[str, str], float] = {}
    filled: dict[str, float] = {}
    for f, _, _ in fills:
        k = (f.strategy_id, f.instrument)
        p = book.setdefault(k, accounting.Pos())
        accounting.apply_fill(p, orders.direction(f.side) * f.quantity, f.price, f.fx_rate)
        fees[k] = fees.get(k, 0.0) + f.commission
        filled[f.order_id] = filled.get(f.order_id, 0.0) + f.quantity
    cash = ps.initial_capital + float(s.scalar(select(func.coalesce(func.sum(PaperLedger.amount), 0.0))
                                               .where(PaperLedger.session_id == ps.id, PaperLedger.kind != "deposit")))
    return {"positions": book, "fees": fees, "filled": filled, "cash": cash, "n_fills": len(fills)}


def reconcile(s: Session, ps: PaperSession) -> dict[str, Any]:
    r = rebuild(s, ps)
    mm: list[dict[str, Any]] = []
    stored = {(p.strategy_id, p.instrument): p for p in
              s.scalars(select(PaperPosition).where(PaperPosition.session_id == ps.id)).all()}
    for k in set(stored) | set(r["positions"]):
        sp, rp = stored.get(k), r["positions"].get(k, accounting.Pos())
        sq = sp.quantity if sp else 0.0
        if not _close(sq, rp.quantity):
            mm.append({"what": "position", "key": "/".join(k), "stored": sq, "rebuilt": rp.quantity})
        if sp and abs(rp.quantity) > 1e-12 and not _close(sp.avg_price, rp.avg_price):
            mm.append({"what": "avg_price", "key": "/".join(k), "stored": sp.avg_price, "rebuilt": rp.avg_price})
        if sp and not _close(sp.realized_pnl, rp.realized):
            mm.append({"what": "realized_pnl", "key": "/".join(k), "stored": sp.realized_pnl, "rebuilt": rp.realized})
        if sp and not _close(sp.fees, r["fees"].get(k, 0.0)):
            mm.append({"what": "fees", "key": "/".join(k), "stored": sp.fees, "rebuilt": r["fees"].get(k, 0.0)})
    if not _close(ps.cash, r["cash"]):
        mm.append({"what": "cash", "key": ps.id, "stored": ps.cash, "rebuilt": r["cash"]})
    for o in s.scalars(select(PaperOrder).where(PaperOrder.session_id == ps.id, PaperOrder.filled_quantity > 0)).all():
        if not _close(o.filled_quantity, r["filled"].get(o.id, 0.0)):
            mm.append({"what": "order_filled_quantity", "key": o.client_order_id, "stored": o.filled_quantity,
                       "rebuilt": r["filled"].get(o.id, 0.0)})
    return {"ok": not mm, "mismatches": mm, "fills": r["n_fills"], "cash": r["cash"]}
