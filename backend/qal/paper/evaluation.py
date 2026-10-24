"""Evaluation of a paper session against the backtest of the same strategies on the same bars.

The backtest (research.backtest.run_backtest) is run on exactly the bars the session processed, with the
session's cost scenario, so differences come from the simulator (lots, limits, risk gate, partial fills,
order types, stops, delays) and not from different data. The comparison uses criteria that were fixed and
hashed when the session was created.

Verdicts: ``insufficient_sample``, ``consistent_with_backtest`` or ``deviates_from_backtest``. None of them
is an approval: the evaluation never changes a strategy's status and never says a strategy is ready to trade.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.models import PaperEquity, PaperFill, PaperOrder, PaperSession, PaperSignal
from qal.paper.config import EvaluationCriteria
from qal.paper.engine import load_runtime
from qal.research.backtest import align, run_backtest

NOT_APPROVAL = ("This compares the simulation with the backtest on the same bars. It is not an approval to trade: "
                "no result here changes a strategy's status or makes it ready for real execution.")


def _max_dd(eq: np.ndarray) -> float:
    if len(eq) == 0:
        return 0.0
    return float((eq / np.maximum.accumulate(eq) - 1).min())


def _sharpe(r: np.ndarray, ppy: float) -> float | None:
    if len(r) < 2 or np.std(r, ddof=1) == 0:
        return None
    return float(np.mean(r) / np.std(r, ddof=1) * np.sqrt(ppy))


def evaluate(s: Session, ps: PaperSession) -> dict[str, Any]:
    crit = EvaluationCriteria.model_validate(ps.criteria)
    rt = load_runtime(s, ps)
    eq_rows = s.scalars(select(PaperEquity).where(PaperEquity.session_id == ps.id).order_by(PaperEquity.bar_ts)).all()
    if not eq_rows:
        return {"verdict": "insufficient_sample", "reasons": ["no bars processed"], "note": NOT_APPROVAL,
                "criteria": crit.model_dump(), "criteria_hash": ps.criteria_hash}
    ts = pd.DatetimeIndex([pd.Timestamp(e.bar_ts) for e in eq_rows]).tz_convert("UTC") if pd.Timestamp(
        eq_rows[0].bar_ts).tzinfo else pd.DatetimeIndex([pd.Timestamp(e.bar_ts, tz="UTC") for e in eq_rows])
    equity = np.array([e.equity for e in eq_rows])
    paper_r = np.diff(np.concatenate([[ps.initial_capital], equity])) / np.concatenate([[ps.initial_capital], equity[:-1]])
    first, last = ts[0], ts[-1]

    # backtest on the same bars, weighted by the strategies' allocations
    bt_r = pd.Series(0.0, index=ts)
    bt_gross_expo = pd.Series(0.0, index=ts)
    agree, compared, per_strategy = 0, 0, []
    bt_cost = 0.0
    ppy = 252.0
    for plan in rt.plans:
        syms = [rt.by_dataset[d] for d in plan.dataset_ids]
        frames = [rt.instr[x].frame[rt.instr[x].frame["ts"] <= last] for x in syms]
        al = align(frames)
        w = plan.strategy().weights(al)
        costs = [rt.cost(plan.id, x) for x in syms]
        ac = rt.instr[syms[0]].info.asset_class
        res = run_backtest(al, w, costs, capital=ps.initial_capital * plan.allocation, asset_class=ac, names=syms)
        ppy = res.periods_per_year
        bars = res.bars.set_index(pd.to_datetime(res.bars["ts"], utc=True))
        sel = bars.loc[(bars.index >= first) & (bars.index <= last)]
        bt_cost += float(sel["cost"].sum()) * ps.initial_capital * plan.allocation
        bt_r = bt_r.add(sel["net"].reindex(ts).fillna(0.0) * plan.allocation, fill_value=0.0)
        bt_gross_expo = bt_gross_expo.add(sel["gross_exposure"].reindex(ts).fillna(0.0) * plan.allocation, fill_value=0.0)
        # signal agreement: the session's decision at bar t vs the full-history weights at bar t
        sigs = s.scalars(select(PaperSignal).where(PaperSignal.session_id == ps.id,
                                                   PaperSignal.strategy_id == plan.id)).all()
        wfull = w.copy()
        wfull.index = pd.to_datetime(al[0]["ts"], utc=True)
        a = c = 0
        for sg in sigs:
            t = pd.Timestamp(sg.bar_ts)
            t = t.tz_localize("UTC") if t.tzinfo is None else t
            if t not in wfull.index:
                continue
            row = wfull.loc[t].fillna(0.0).to_numpy(dtype=float)
            g = np.abs(row).sum()
            row = row / g if g > 1 else row
            c += 1
            a += all(abs(sg.targets.get(x, 0.0) - row[j]) < 1e-9 for j, x in enumerate(syms))
        agree, compared = agree + a, compared + c
        per_strategy.append({"strategy_id": plan.id, "kind": plan.kind, "allocation": plan.allocation,
                             "backtest_return_net": float(np.prod(1 + sel["net"].to_numpy()) - 1),
                             "signals_compared": c, "signals_agree": a,
                             "validation": plan.validation, "notes": plan.notes})
    bt = bt_r.to_numpy()
    bt_eq = np.cumprod(1 + bt)

    # orders and fills
    ords = s.scalars(select(PaperOrder).where(PaperOrder.session_id == ps.id)).all()
    fills = s.scalars(select(PaperFill).where(PaperFill.session_id == ps.id)).all()
    finished = [o for o in ords if o.status in ("FILLED", "CANCELLED", "EXPIRED") and o.purpose != "protective_stop"]
    ordered = sum(o.quantity for o in finished)
    fill_rate = (sum(o.filled_quantity for o in finished) / ordered) if ordered else None
    shortfall = [(1 if f.side == "buy" else -1) * (f.price / f.reference_price - 1) * 1e4 for f in fills]
    paper_cost = sum(f.commission + f.spread_cost + f.slippage_cost for f in fills) + float(
        sum(e for e in [eq_rows[-1].holding_costs]))
    paper_total = float(equity[-1] / ps.initial_capital - 1)
    bt_total = float(bt_eq[-1] - 1)
    corr = float(np.corrcoef(paper_r, bt)[0, 1]) if len(bt) > 2 and np.std(paper_r) > 0 and np.std(bt) > 0 else None
    paper_dd = _max_dd(np.concatenate([[1.0], equity / ps.initial_capital]))
    bt_dd = _max_dd(np.concatenate([[1.0], bt_eq]))
    gross = np.array([e.gross_exposure for e in eq_rows])

    # predictive decay and regime: paper period vs validation and vs the warm-up period
    vol_paper = float(np.std(paper_r, ddof=1) * np.sqrt(ppy)) if len(paper_r) > 2 else None
    regime = []
    for sym, ir in rt.instr.items():
        f = ir.frame
        r = np.log(f["close"]).diff()
        before = r[f["ts"] < first].tail(250)
        during = r[(f["ts"] >= first) & (f["ts"] <= last)]
        if len(before) > 20 and len(during) > 20 and before.std() > 0:
            ratio = float(during.std() / before.std())
            regime.append({"instrument": sym, "vol_ratio_session_vs_prior_250": round(ratio, 3),
                           "shift": ratio > 1.5 or ratio < 0.67})
    decay = []
    for p in per_strategy:
        oos = (p["validation"] or {}).get("oos_sharpe_net")
        if oos is not None:
            decay.append({"strategy_id": p["strategy_id"], "validation_oos_sharpe": oos})
    metrics = {
        "bars": len(eq_rows), "fills": len(fills), "orders": len(ords),
        "orders_rejected": sum(o.status == "REJECTED" for o in ords),
        "orders_filled": sum(o.status == "FILLED" for o in ords),
        "orders_expired": sum(o.status == "EXPIRED" for o in ords),
        "orders_cancelled": sum(o.status == "CANCELLED" for o in ords),
        "fill_rate": fill_rate,
        "paper_return_net": paper_total, "backtest_return_net": bt_total, "return_gap": paper_total - bt_total,
        "return_correlation": corr, "tracking_error_ann": float(np.std(paper_r - bt, ddof=1) * np.sqrt(ppy))
        if len(bt) > 2 else None,
        "paper_max_drawdown": paper_dd, "backtest_max_drawdown": bt_dd,
        "drawdown_ratio": (paper_dd / bt_dd) if bt_dd < 0 else None,
        "paper_sharpe": _sharpe(paper_r, ppy), "backtest_sharpe": _sharpe(bt, ppy), "paper_vol_ann": vol_paper,
        "paper_avg_gross_exposure": float(gross.mean()), "backtest_avg_gross_exposure": float(bt_gross_expo.mean()),
        "avg_shortfall_vs_decision_close_bps": float(np.mean(shortfall)) if shortfall else None,
        "paper_cost": paper_cost, "backtest_cost": bt_cost,
        "cost_ratio": paper_cost / bt_cost if bt_cost else None,
        "signal_agreement": agree / compared if compared else None, "signals_compared": compared,
    }
    reasons, sample = [], True
    if metrics["bars"] < crit.min_bars:
        reasons.append(f"{metrics['bars']} bars < {crit.min_bars}")
        sample = False
    if metrics["fills"] < crit.min_fills:
        reasons.append(f"{metrics['fills']} fills < {crit.min_fills}")
        sample = False
    dev = []
    if abs(metrics["return_gap"]) > crit.max_return_gap:
        dev.append(f"return gap {metrics['return_gap']:+.4f} beyond ±{crit.max_return_gap}")
    if corr is not None and corr < crit.min_return_correlation:
        dev.append(f"return correlation {corr:.2f} < {crit.min_return_correlation}")
    if metrics["drawdown_ratio"] is not None and metrics["drawdown_ratio"] > crit.max_drawdown_ratio:
        dev.append(f"drawdown ratio {metrics['drawdown_ratio']:.2f} > {crit.max_drawdown_ratio}")
    if fill_rate is not None and fill_rate < crit.min_fill_rate:
        dev.append(f"fill rate {fill_rate:.2f} < {crit.min_fill_rate}")
    if metrics["cost_ratio"] is not None and metrics["cost_ratio"] > crit.max_cost_ratio:
        dev.append(f"paper/backtest cost {metrics['cost_ratio']:.2f} > {crit.max_cost_ratio}")
    if metrics["signal_agreement"] is not None and metrics["signal_agreement"] < crit.min_signal_agreement:
        dev.append(f"signal agreement {metrics['signal_agreement']:.2f} < {crit.min_signal_agreement}")
    verdict = "insufficient_sample" if not sample else "deviates_from_backtest" if dev else "consistent_with_backtest"
    return {"verdict": verdict, "reasons": reasons + dev, "metrics": metrics, "per_strategy": per_strategy,
            "regime": regime, "predictive_decay": decay, "criteria": crit.model_dump(),
            "criteria_hash": ps.criteria_hash, "is_synthetic": ps.is_synthetic, "is_demo": ps.is_demo,
            "mode": ps.mode, "note": NOT_APPROVAL,
            "period": {"first_bar": str(first), "last_bar": str(last)},
            "curves": {"ts": [str(t.date()) for t in ts], "paper": (equity / ps.initial_capital).round(6).tolist(),
                       "backtest": bt_eq.round(6).tolist()}}
