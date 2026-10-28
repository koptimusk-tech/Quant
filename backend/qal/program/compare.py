"""Comparison of a program's paper results with the backtest of the same candidate version on the same bars.

First the conventions are checked (same version, parameters, universe, timeframe, costs and allocation); a
comparison whose conventions differ is reported as not comparable instead of being summarised. Then returns,
volatility, drawdown, signal frequency, the per-trade distribution, costs, exposure, per asset, per volatility
regime and fills are set side by side.

Differences are only attributed to a component that is measured on both sides (costs). Whatever remains is
listed as *unreconciled*, with its size, and no cause is asserted for it.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.models import PaperFill, PaperOrder, PaperSession, PaperSignal
from qal.paper.evaluation import backtest_on_bars, evaluate
from qal.program import tracking
from qal.robustness.engine import regime_labels


def conventions(ps: PaperSession, rt: Any, cand: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    plan = rt.plans[0]
    syms = [rt.by_dataset[d] for d in plan.dataset_ids]
    checks = [
        {"check": "candidate_version", "passed": plan.id == f"P:{ps.config.get('candidate_id')}",
         "detail": f"session strategy {plan.id}"},
        {"check": "strategy_and_parameters", "passed": plan.type == cand["strategy"]["type"]
         and plan.params == cand["strategy"]["params"], "detail": f"{plan.type} {plan.params}"},
        {"check": "universe", "passed": syms == [a["symbol"] for a in cand["assets"]], "detail": ", ".join(syms)},
        {"check": "timeframe", "passed": all(r.info.frequency == cand["timeframe"] for r in rt.instr.values()),
         "detail": cand["timeframe"]},
        {"check": "costs", "passed": all(plan.costs.get(a["dataset_id"]) == {k: v for k, v in cand["costs"][
            a["dataset_id"]].items() if k != "source"} for a in cand["assets"]), "detail": "candidate cost models"},
        {"check": "cost_scenario", "passed": rt.cfg.cost_scenario == "base", "detail": rt.cfg.cost_scenario},
        {"check": "allocation", "passed": abs(plan.allocation - cand["allocation"]) < 1e-12,
         "detail": f"{plan.allocation} (candidate {cand['allocation']})"},
    ]
    hashes = {r.info.symbol: r.info.content_hash for r in rt.instr.values()}
    same_data = all(hashes.get(a["symbol"]) == a["content_hash"] for a in cand["assets"])
    if mode == "B":
        checks.append({"check": "data_snapshot", "passed": True,
                       "detail": "new data: the latest snapshot of the same instruments" + (
                           "" if not same_data else " (still the admission snapshot)")})
    else:
        checks.append({"check": "data_snapshot", "passed": same_data,
                       "detail": "the admission snapshot (content hashes)" if same_data else "data changed since "
                       "admission"})
    return checks


def _utc(x: Any) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _quantiles(x: list[float]) -> dict[str, Any] | None:
    if not x:
        return None
    a = np.array(x)
    return {"n": len(a), "mean": float(a.mean()), "p10": float(np.quantile(a, 0.1)), "p50": float(np.median(a)),
            "p90": float(np.quantile(a, 0.9)), "win_rate": float((a > 0).mean())}


def compare(s: Session, ps: PaperSession, rt: Any, cand: dict[str, Any], proto: dict[str, Any],
            trk: dict[str, Any] | None = None) -> dict[str, Any]:
    checks = conventions(ps, rt, cand, proto["mode"])
    out: dict[str, Any] = {"conventions": checks, "comparable": all(c["passed"] for c in checks)}
    if not out["comparable"]:
        out["note"] = "Not comparable: the conventions differ, so no comparison is summarised."
        return out
    eq = tracking.equity_frame(s, ps)
    if eq.empty:
        out["note"] = "no bars processed"
        return out
    trk = trk or tracking.track(s, ps, rt, proto)
    ts = pd.DatetimeIndex(eq["ts"])
    bt = backtest_on_bars(rt, ps, ts)
    ev = evaluate(s, ps)
    m = ev["metrics"]
    part = bt["plans"][0]
    plan, syms, res = part["plan"], part["syms"], part["result"]
    n = len(ts)

    # signal frequency: decisions that change vs backtest weight changes on the same bars
    sigs = s.scalars(select(PaperSignal).where(PaperSignal.session_id == ps.id)).all()
    w = part["weights"].copy()
    w.index = pd.to_datetime(part["aligned"][0]["ts"], utc=True)
    wsel = w.loc[(w.index >= ts[0]) & (w.index <= ts[-1])].fillna(0.0)
    bt_changes = int((wsel.diff().abs().sum(axis=1) > 1e-12).sum())
    paper_changes = sum(1 for x in sigs if x.changed)

    # per-trade distribution, in units of strategy capital
    p_tr = [t["net_pnl"] for t in trk.get("trade_list", [])]
    b_tr = [t["net_pnl"] for t in res.trades if _utc(t["entry_ts"]) >= ts[0]]

    # costs
    fills = s.scalars(select(PaperFill).where(PaperFill.session_id == ps.id)).all()
    ords = s.scalars(select(PaperOrder).where(PaperOrder.session_id == ps.id)).all()
    paper_cost = {"commission": sum(f.commission for f in fills), "spread": sum(f.spread_cost for f in fills),
                  "slippage": sum(f.slippage_cost for f in fills),
                  "holding": float(m["paper_cost"] - sum(f.commission + f.spread_cost + f.slippage_cost
                                                         for f in fills))}
    cost_bps = [(f.commission + f.spread_cost + f.slippage_cost) / abs(f.quantity * f.price * (f.fx_rate or 1.0)) * 1e4
                for f in fills if f.quantity and f.price]

    # per asset, gross of costs, on the strategy's capital: paper realised + unrealised P&L; backtest sum of the
    # executed position of the previous bar times the close-to-close return (an approximation, labelled so)
    from qal.models import PaperPosition

    pos = s.scalars(select(PaperPosition).where(PaperPosition.session_id == ps.id)).all()
    cap = ps.initial_capital * plan.allocation
    bpos = res.positions.set_index(pd.to_datetime(res.positions["ts"], utc=True))
    per_asset = []
    for sym in syms:
        f = rt.instr[sym].frame
        ret = pd.Series(f["close"].to_numpy(dtype=float), index=pd.to_datetime(f["ts"], utc=True)).pct_change()
        contrib = (bpos[sym].shift(1) * ret.reindex(bpos.index)).reindex(ts).fillna(0.0)
        paper_gross = sum(p.realized_pnl + p.unrealized_pnl for p in pos if p.instrument == sym) / cap
        per_asset.append({"instrument": sym, "paper_gross": paper_gross, "backtest_gross_approx": float(contrib.sum()),
                          "paper_costs": sum(p.fees + p.holding_costs for p in pos if p.instrument == sym) / cap})

    # regimes: volatility regime of the first leg at each bar (labels use past data only)
    f0 = rt.instr[syms[0]].frame
    lab = regime_labels(f0)
    lab.index = pd.to_datetime(f0["ts"], utc=True)
    reg = lab.reindex(ts)
    rp, rb = eq["r"].to_numpy(), bt["returns"].to_numpy()
    regimes = []
    for name in sorted(reg["volatility"].dropna().unique()):
        mask = (reg["volatility"] == name).to_numpy()
        if mask.sum():
            regimes.append({"regime": str(name), "bars": int(mask.sum()), "paper_mean": float(rp[mask].mean()),
                            "backtest_mean": float(rb[mask].mean()),
                            "diff_bps": float((rp[mask] - rb[mask]).mean() * 1e4)})

    # reconciliation of the difference: costs are measured on both sides; the rest is unreconciled
    total_gap = float(np.prod(1 + rp) - np.prod(1 + rb))
    cost_gap = (bt["cost"] - m["paper_cost"]) / ps.initial_capital
    unreconciled = []
    tol = proto["tracking_tolerance_bps"]
    if abs(trk["diff_vs_backtest"]["mean_bps"]) > tol:
        unreconciled.append({"item": "mean return per bar", "paper_minus_backtest_bps": trk["diff_vs_backtest"][
            "mean_bps"], "explained_by_costs_bps": cost_gap / n * 1e4,
            "unreconciled_bps": trk["diff_vs_backtest"]["mean_bps"] - cost_gap / n * 1e4,
            "cause": "not asserted: no measured component explains the remainder"})
    if m["signal_agreement"] is not None and m["signal_agreement"] < 1:
        unreconciled.append({"item": "signals", "agreement": m["signal_agreement"],
                             "cause": "not asserted: decisions differ from the full-history weights on some bars"})
    out.update({
        "bars": n, "period": {"first": str(ts[0]), "last": str(ts[-1])},
        "returns": {"paper_total": float(np.prod(1 + rp) - 1), "backtest_total": float(np.prod(1 + rb) - 1),
                    "gap": total_gap, "paper_mean_bps": float(rp.mean() * 1e4),
                    "backtest_mean_bps": float(rb.mean() * 1e4),
                    "diff_ci95_bps": trk["diff_vs_backtest"]["mean_ci95_bps"],
                    "correlation": trk["diff_vs_backtest"]["correlation"],
                    "tracking_error_ann": trk["diff_vs_backtest"]["tracking_error_ann"]},
        "volatility": {"paper_ann": trk["paper"].get("vol_ann"), "backtest_ann": trk["backtest"].get("vol_ann")},
        "drawdown": {"paper": m["paper_max_drawdown"], "backtest": m["backtest_max_drawdown"],
                     "ratio": m["drawdown_ratio"]},
        "signal_frequency": {"paper_changes_per_100_bars": paper_changes / n * 100,
                             "backtest_changes_per_100_bars": bt_changes / n * 100,
                             "agreement": m["signal_agreement"], "compared": m["signals_compared"]},
        "per_trade": {"paper": _quantiles(p_tr), "backtest": _quantiles(b_tr),
                      "units": "net return per trade on the strategy's capital"},
        "costs": {"paper_total": m["paper_cost"], "backtest_total": bt["cost"], "ratio": m["cost_ratio"],
                  "paper_breakdown": paper_cost, "paper_cost_bps_per_fill": _quantiles(cost_bps),
                  "note": "configured cost models are estimates, not observed costs, unless stated otherwise"},
        "exposure": {"paper_avg_gross": m["paper_avg_gross_exposure"],
                     "backtest_avg_gross": m["backtest_avg_gross_exposure"]},
        "per_asset": per_asset, "per_asset_units": "fraction of the strategy's capital, gross of costs",
        "regimes": regimes, "regime_rule": "volatility regime of the first leg (20-bar vol vs its expanding median, "
                                           "known at the previous bar)",
        "fills": {"orders": m["orders"], "fills": m["fills"], "fill_rate": m["fill_rate"],
                  "rejected": m["orders_rejected"], "expired": m["orders_expired"], "cancelled": m["orders_cancelled"],
                  "partial": sum(1 for o in ords if 0 < o.filled_quantity < o.quantity),
                  "avg_shortfall_vs_decision_close_bps": m["avg_shortfall_vs_decision_close_bps"]},
        "unreconciled": unreconciled,
        "evaluation_verdict": ev["verdict"],
        "note": "Differences are attributed only to measured components (costs); the remainder is listed without a "
                "cause. A comparison is not an approval.",
    })
    return out
