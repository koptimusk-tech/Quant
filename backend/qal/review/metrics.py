"""Result metrics of a paper session: performance, risk and execution, each with its sample size.

Definitions follow the project's conventions: per-bar net returns are equity changes (``tracking.equity_frame``),
trades are segmented like the backtest's (``tracking.paper_trades``, net return on the strategy's capital), costs
are the simulator's fills plus holding costs. Every metric is returned as
``{"status": "OK" | "INSUFFICIENT_DATA", "value", "n", "min_n", "unit", "note"}``: when the sample is below the
minimum the value is withheld instead of being reported as if it meant something.

The last trade is *open* when a position is still held at the last bar: it is valued at the last close
(unrealised) and kept out of the closed-trade statistics.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.models import PaperEquity, PaperFill, PaperOrder, PaperPosition, PaperSession

MIN_BARS_RISK = 20
MIN_BARS_SHARPE = 60
MIN_TRADES = 5
MIN_ADVERSE_BARS = 5
METRICS_VERSION = "review-metrics@1.0.0"


def metric(value: Any, n: int, *, min_n: int = 1, unit: str = "", note: str = "") -> dict[str, Any]:
    ok = value is not None and n >= min_n and not (isinstance(value, float) and not math.isfinite(value))
    if not ok:
        why = f"needs at least {min_n} (has {n})" if n < min_n else "not defined for this sample"
        return {"status": "INSUFFICIENT_DATA", "value": None, "n": n, "min_n": min_n, "unit": unit,
                "note": (note + "; " if note else "") + why}
    return {"status": "OK", "value": float(value) if not isinstance(value, (list, dict)) else value, "n": n,
            "min_n": min_n, "unit": unit, "note": note}


def drawdown_episodes(r: np.ndarray) -> dict[str, Any]:
    eq = np.concatenate([[1.0], np.cumprod(1 + r)])
    under = eq < np.maximum.accumulate(eq) - 1e-15
    lengths, cur = [], 0
    for u in under[1:]:
        if u:
            cur += 1
        elif cur:
            lengths.append(cur)
            cur = 0
    return {"episodes": len(lengths) + (1 if cur else 0), "max_bars": max(lengths + [cur]) if (lengths or cur) else 0,
            "current_bars": cur, "recovered": cur == 0}


def _worst_windows(r: np.ndarray, ts: pd.DatetimeIndex, w: int) -> dict[str, Any] | None:
    if len(r) < w:
        return None
    lr = pd.Series(np.log1p(r)).rolling(w).sum()
    i = int(lr.idxmin())
    return {"bars": w, "return": float(np.expm1(lr.iloc[i])), "from": str(ts[i - w + 1]), "to": str(ts[i])}


def _dist(x: np.ndarray) -> dict[str, Any] | None:
    if len(x) < MIN_BARS_RISK:
        return None
    sd = float(np.std(x, ddof=1))
    z = (x - x.mean()) / sd if sd > 0 else np.zeros_like(x)
    return {"n": len(x), "mean": float(x.mean()), "std": sd, "skew": float((z ** 3).mean()),
            "kurtosis": float((z ** 4).mean()),
            "quantiles": {q: float(np.quantile(x, q)) for q in (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)}}


def compute(s: Session, ps: PaperSession, rt: Any, eq: pd.DataFrame, trades: list[dict[str, Any]],
            ppy: float) -> dict[str, Any]:
    n = len(eq)
    r = eq["r"].to_numpy() if n else np.array([])
    ts = pd.DatetimeIndex(eq["ts"]) if n else pd.DatetimeIndex([])
    fills = s.scalars(select(PaperFill).where(PaperFill.session_id == ps.id)).all()
    orders = s.scalars(select(PaperOrder).where(PaperOrder.session_id == ps.id)).all()
    pos = s.scalars(select(PaperPosition).where(PaperPosition.session_id == ps.id)).all()
    rows = s.scalars(select(PaperEquity).where(PaperEquity.session_id == ps.id).order_by(PaperEquity.bar_ts)).all()
    holding = float(rows[-1].holding_costs) if rows else 0.0
    trade_costs = {"commission": sum(f.commission for f in fills), "spread": sum(f.spread_cost for f in fills),
                   "slippage": sum(f.slippage_cost for f in fills), "holding": holding}
    cost_total = sum(trade_costs.values())
    cap = ps.initial_capital
    net_pnl = float(rows[-1].equity - cap) if rows else 0.0
    # spread and slippage are already in the fill prices: adding them back gives the P&L before costs
    gross_pnl = net_pnl + cost_total

    open_trade = any(abs(p.quantity) > 1e-12 for p in pos)
    closed = trades[:-1] if open_trade and trades else trades
    tr = np.array([t["net_pnl"] for t in closed])
    wins, losses = tr[tr > 0], tr[tr < 0]
    k = len(tr)
    pf = float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else None

    sd = float(np.std(r, ddof=1)) if n > 1 else None
    down = r[r < 0]
    dsd = float(np.sqrt((down ** 2).sum() / n)) if n and len(down) else None
    gross = np.array([e.gross_exposure for e in rows])
    net = np.array([e.net_exposure for e in rows])

    # concentration: share of absolute P&L (realised + unrealised, before costs) per instrument
    by_asset = {}
    for p in pos:
        by_asset[p.instrument] = by_asset.get(p.instrument, 0.0) + p.realized_pnl + p.unrealized_pnl
    tot = sum(abs(v) for v in by_asset.values())
    conc = [{"instrument": k_, "pnl": v, "share_abs": abs(v) / tot if tot else None} for k_, v in by_asset.items()]
    hhi = sum((c["share_abs"] or 0) ** 2 for c in conc) if tot else None

    # adverse scenarios: the worst decile of the first leg's own bar returns within the period
    adverse = None
    if n and rt.plans:
        sym = rt.by_dataset[rt.plans[0].dataset_ids[0]]
        f = rt.instr[sym].frame
        u = pd.Series(f["close"].to_numpy(dtype=float), index=pd.to_datetime(f["ts"], utc=True)).pct_change()
        u = u.reindex(ts).fillna(0.0).to_numpy()
        if n >= 10 * MIN_ADVERSE_BARS:
            mask = u <= np.quantile(u, 0.1)
            adverse = {"rule": f"bars in the worst 10% of {sym}'s bar return within the period",
                       "bars": int(mask.sum()), "paper_mean": float(r[mask].mean()),
                       "paper_total": float(np.prod(1 + r[mask]) - 1), "underlying_mean": float(u[mask].mean())}

    notional = [abs(f.quantity * f.price * (f.fx_rate or 1.0)) for f in fills]
    slip_bps = [f.slippage_cost / x * 1e4 for f, x in zip(fills, notional, strict=True) if x]
    cost_bps = [(f.commission + f.spread_cost + f.slippage_cost) / x * 1e4 for f, x in zip(fills, notional, strict=True)
                if x]
    shortfall = [(1 if f.side == "buy" else -1) * (f.price / f.reference_price - 1) * 1e4 for f in fills
                 if f.reference_price]
    finished = [o for o in orders if o.status in ("FILLED", "CANCELLED", "EXPIRED", "REJECTED")]

    def q(x: list[float]) -> dict[str, Any] | None:
        if not x:
            return None
        a = np.array(x)
        return {"n": len(a), "mean": float(a.mean()), "p50": float(np.median(a)), "p90": float(np.quantile(a, 0.9)),
                "max": float(a.max())}

    worst = sorted(closed, key=lambda t: t["net_pnl"])[:5]
    return {
        "version": METRICS_VERSION,
        "sample": {"bars": n, "first_bar": str(ts[0]) if n else None, "last_bar": str(ts[-1]) if n else None,
                   "trades_closed": k, "trade_open_at_end": open_trade, "fills": len(fills), "orders": len(orders),
                   "periods_per_year": ppy},
        "performance": {
            "pnl_gross": metric(gross_pnl, n, unit="account currency", note="before commissions, spread, slippage "
                                "and holding costs (estimated cost models)"),
            "pnl_net": metric(net_pnl, n, unit="account currency"),
            "cumulative_return": metric(float(np.prod(1 + r) - 1) if n else None, n, unit="fraction of capital"),
            "return_per_trade": metric(float(tr.mean()) if k else None, k, min_n=MIN_TRADES,
                                       unit="net return on the strategy's capital", note="closed trades"),
            "return_distribution": metric(_dist(r), n, min_n=MIN_BARS_RISK, unit="per bar"),
            "avg_win": metric(float(wins.mean()) if len(wins) else None, len(wins), min_n=MIN_TRADES // 2 or 1),
            "avg_loss": metric(float(losses.mean()) if len(losses) else None, len(losses), min_n=MIN_TRADES // 2 or 1),
            "expectancy_per_trade": metric(float(tr.mean()) if k else None, k, min_n=MIN_TRADES,
                                           note="mean net return of a closed trade"),
            "hit_rate_trades": metric(float((tr > 0).mean()) if k else None, k, min_n=MIN_TRADES),
            "profit_factor": metric(pf, k, min_n=MIN_TRADES, note="gross wins / gross losses of closed trades"),
        },
        "risk": {
            "volatility_ann": metric(sd * math.sqrt(ppy) if sd is not None else None, n, min_n=MIN_BARS_RISK),
            "max_drawdown": metric(float((np.concatenate([[1.0], np.cumprod(1 + r)]) / np.maximum.accumulate(
                np.concatenate([[1.0], np.cumprod(1 + r)])) - 1).min()) if n else None, n),
            "drawdown_duration": metric(drawdown_episodes(r) if n else None, n, unit="bars"),
            "sharpe_ann": metric(float(r.mean() / sd * math.sqrt(ppy)) if sd else None, n, min_n=MIN_BARS_SHARPE,
                                 note="see the uncertainty section before reading it"),
            "sortino_ann": metric(float(r.mean() / dsd * math.sqrt(ppy)) if dsd else None, n, min_n=MIN_BARS_SHARPE),
            "gross_exposure": metric({"avg": float(gross.mean()), "max": float(gross.max())} if len(gross) else None, n),
            "net_exposure": metric({"avg": float(net.mean()), "min": float(net.min()), "max": float(net.max())}
                                   if len(net) else None, n),
            "concentration_by_asset": metric({"assets": conc, "hhi": hhi} if conc else None, len(conc),
                                             note="share of absolute P&L before costs"),
            "worst_trades": metric(worst or None, k, unit="closed trades"),
            "worst_periods": metric({"5_bars": _worst_windows(r, ts, 5), "20_bars": _worst_windows(r, ts, 20)}
                                    if n >= 20 else None, n, min_n=20),
            "adverse_scenarios": metric(adverse, adverse["bars"] if adverse else 0, min_n=MIN_ADVERSE_BARS,
                                        note="historical bars inside the period, not a forecast"),
        },
        "execution": {
            "costs_total": metric(cost_total, len(fills), unit="account currency",
                                  note="configured cost models (estimates, not observed)"),
            "costs_breakdown": metric(trade_costs, len(fills), unit="account currency"),
            "cost_bps_per_fill": metric(q(cost_bps), len(cost_bps), unit="bps of notional"),
            "slippage_bps_per_fill": metric(q(slip_bps), len(slip_bps), unit="bps of notional",
                                            note="simulated by the cost model, not observed"),
            "price_vs_reference_bps": metric(q(shortfall), len(shortfall), unit="bps, positive = worse than the "
                                             "decision close"),
            "orders_not_filled": metric(sum(1 for o in finished if o.filled_quantity <= 0 and o.status in
                                            ("CANCELLED", "EXPIRED")), len(orders)),
            "partial_fills": metric(sum(1 for o in orders if 0 < o.filled_quantity < o.quantity - 1e-12), len(orders)),
            "rejected": metric(sum(1 for o in orders if o.status == "REJECTED"), len(orders)),
            "cancelled": metric(sum(1 for o in orders if o.status == "CANCELLED"), len(orders)),
            "expired": metric(sum(1 for o in orders if o.status == "EXPIRED"), len(orders)),
        },
        "note": "Metrics describe this simulated sample; INSUFFICIENT_DATA means the value is withheld, not zero.",
    }
