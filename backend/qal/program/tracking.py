"""Statistical tracking of a program: cumulative and rolling metrics with their uncertainty.

Everything is computed from what the paper session stored (equity per bar, signals, fills) and from the
same-bar backtest; nothing is estimated from future data. Intervals:

* mean returns and differences: 95% moving-block bootstrap (block = the candidate's horizon in bars, at least 5),
  fixed seed, so bars are not treated as independent;
* Sharpe: the i.i.d. standard error of Lo (2002), sqrt((1 + SR^2 / 2) / n), annualised.

Results are ``provisional`` until the protocol's horizon and minimum trades are reached. An interval is a
statement about this sample under these assumptions; it is not the probability that the strategy is profitable.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.models import PaperEquity, PaperSession, PaperSignal
from qal.paper.evaluation import backtest_on_bars
from qal.research.backtest import extract_trades

N_BOOT = 1000
SEED = 20240601


def block_bootstrap_ci(x: np.ndarray, block: int, *, n_boot: int = N_BOOT, seed: int = SEED) -> list[float] | None:
    n = len(x)
    if n < 10:
        return None
    b = max(1, min(block, n // 2))
    rng = np.random.default_rng(seed)
    k = math.ceil(n / b)
    starts = rng.integers(0, n - b + 1, size=(n_boot, k))
    idx = (starts[:, :, None] + np.arange(b)[None, None, :]).reshape(n_boot, -1)[:, :n]
    means = x[idx].mean(axis=1)
    return [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))]


def max_drawdown(r: np.ndarray) -> float:
    if len(r) == 0:
        return 0.0
    eq = np.cumprod(1 + r)
    eq = np.concatenate([[1.0], eq])
    return float((eq / np.maximum.accumulate(eq) - 1).min())


def stats(r: np.ndarray, ppy: float, block: int) -> dict[str, Any]:
    n = len(r)
    out: dict[str, Any] = {"n": n}
    if n == 0:
        return out
    sd = float(np.std(r, ddof=1)) if n > 1 else 0.0
    sr = float(np.mean(r) / sd * math.sqrt(ppy)) if sd > 0 else None
    se = math.sqrt((1 + (sr / math.sqrt(ppy)) ** 2 / 2) / n) * math.sqrt(ppy) if sr is not None and n > 1 else None
    out.update({"mean": float(np.mean(r)), "mean_ci95": block_bootstrap_ci(r, block),
                "vol_ann": sd * math.sqrt(ppy) if n > 1 else None, "sharpe_ann": sr, "sharpe_se": se,
                "sharpe_ci95": [sr - 1.96 * se, sr + 1.96 * se] if se is not None else None,
                "total_return": float(np.prod(1 + r) - 1), "max_drawdown": max_drawdown(r),
                "hit_rate": float((r > 0).mean())})
    return out


def equity_frame(s: Session, ps: PaperSession) -> pd.DataFrame:
    rows = s.scalars(select(PaperEquity).where(PaperEquity.session_id == ps.id).order_by(PaperEquity.bar_ts)).all()
    if not rows:
        return pd.DataFrame(columns=["ts", "equity", "r", "gross", "drawdown"])
    eq = np.array([e.equity for e in rows])
    prev = np.concatenate([[ps.initial_capital], eq[:-1]])
    return pd.DataFrame({"ts": pd.to_datetime([pd.Timestamp(e.bar_ts) for e in rows], utc=True), "equity": eq,
                         "r": eq / prev - 1, "gross": [e.gross_exposure for e in rows],
                         "drawdown": [e.drawdown for e in rows]})


def paper_positions(s: Session, ps: PaperSession, plan: Any, syms: list[str], ts: pd.DatetimeIndex) -> pd.DataFrame:
    """Targets decided at the close of bar t are held from bar t+1 (orders fill at the next bar at the earliest)."""
    sigs = s.scalars(select(PaperSignal).where(PaperSignal.session_id == ps.id, PaperSignal.strategy_id == plan.id)
                     .order_by(PaperSignal.bar_ts)).all()
    tgt = pd.DataFrame([{x: sg.targets.get(x, 0.0) for x in syms} for sg in sigs],
                       index=pd.to_datetime([pd.Timestamp(sg.bar_ts) for sg in sigs], utc=True), columns=syms)
    held = tgt.reindex(ts).ffill().shift(1).fillna(0.0)
    held.insert(0, "ts", ts)
    return held.reset_index(drop=True)


def paper_trades(s: Session, ps: PaperSession, rt: Any, eq: pd.DataFrame) -> list[dict[str, Any]]:
    """Trades segmented like the backtest's (``extract_trades``), on the paper's held targets and its net returns per
    unit of strategy capital (session return / allocation)."""
    out = []
    ts = pd.DatetimeIndex(eq["ts"])
    for plan in rt.plans:
        syms = [rt.by_dataset[d] for d in plan.dataset_ids]
        pos = paper_positions(s, ps, plan, syms, ts)
        r = eq["r"].to_numpy() / plan.allocation  # a program session trades one candidate
        bars = pd.DataFrame({"net": r, "gross": r})
        for t in extract_trades(pos, bars, syms):
            out.append({**t, "strategy_id": plan.id})
    return out


def baseline_returns(rt: Any, ts: pd.DatetimeIndex, baseline: str) -> np.ndarray:
    if baseline == "no_trade":
        return np.zeros(len(ts))
    parts = []
    for plan in rt.plans:  # buy and hold the first leg with the strategy's allocation (single entry cost ignored)
        sym = rt.by_dataset[plan.dataset_ids[0]]
        f = rt.instr[sym].frame
        px = f["adj_close"] if "adj_close" in f and f["adj_close"].notna().all() else f["close"]
        ret = pd.Series(px.to_numpy(), index=pd.to_datetime(f["ts"], utc=True)).pct_change()
        parts.append(ret.reindex(ts).fillna(0.0).to_numpy() * plan.allocation)
    return np.sum(parts, axis=0)


def track(s: Session, ps: PaperSession, rt: Any, proto: dict[str, Any]) -> dict[str, Any]:
    eq = equity_frame(s, ps)
    hz = proto["horizon"]
    ppy = float(hz["periods_per_year"])
    block = max(5, int(proto.get("block_bars") or 5))
    n = len(eq)
    base = {"bars": n, "trades": 0, "horizon_bars": hz["horizon_bars"], "min_trades": proto["min_trades"],
            "provisional": True, "baseline": proto["baseline"]}
    if n == 0:
        return {**base, "provisional_reasons": ["no bars processed"]}
    ts = pd.DatetimeIndex(eq["ts"])
    r = eq["r"].to_numpy()
    bt = backtest_on_bars(rt, ps, ts)["returns"].to_numpy()
    bl = baseline_returns(rt, ts, proto["baseline"])
    trades = paper_trades(s, ps, rt, eq)
    reasons = []
    if n < hz["horizon_bars"]:
        reasons.append(f"{n} of {hz['horizon_bars']} planned bars")
    if len(trades) < proto["min_trades"]:
        reasons.append(f"{len(trades)} of {proto['min_trades']} trades")
    d_bt, d_bl = r - bt, r - bl
    w = int(proto["rolling_window"])
    rolling = None
    if n >= w:
        rs = pd.Series(r)
        sd = rs.rolling(w).std(ddof=1)
        rolling = {"window": w, "ts": [str(t.date()) for t in ts],
                   "mean_ann": (rs.rolling(w).mean() * ppy).round(6).where(lambda x: x.notna(), None).tolist(),
                   "sharpe": (rs.rolling(w).mean() / sd * math.sqrt(ppy)).replace([np.inf, -np.inf], np.nan)
                   .round(4).where(lambda x: x.notna(), None).tolist(),
                   "diff_vs_backtest_bps": (pd.Series(d_bt).rolling(w).mean() * 1e4).round(4)
                   .where(lambda x: x.notna(), None).tolist()}
    return {**base, "trades": len(trades), "provisional": bool(reasons), "provisional_reasons": reasons,
            "first_bar": str(ts[0]), "last_bar": str(ts[-1]),
            "paper": stats(r, ppy, block), "backtest": stats(bt, ppy, block), "baseline_stats": stats(bl, ppy, block),
            "excess_vs_baseline": {"mean": float(d_bl.mean()), "mean_ci95": block_bootstrap_ci(d_bl, block)},
            "diff_vs_backtest": {"mean_bps": float(d_bt.mean() * 1e4),
                                 "mean_ci95_bps": [x * 1e4 for x in ci] if (ci := block_bootstrap_ci(d_bt, block))
                                 else None,
                                 "correlation": float(np.corrcoef(r, bt)[0, 1]) if n > 2 and np.std(r) > 0
                                 and np.std(bt) > 0 else None,
                                 "tracking_error_ann": float(np.std(d_bt, ddof=1) * math.sqrt(ppy)) if n > 2 else None},
            "trade_list": trades[-200:],
            "curves": {"ts": [str(t.date()) for t in ts], "paper": np.cumprod(1 + r).round(6).tolist(),
                       "backtest": np.cumprod(1 + bt).round(6).tolist(), "baseline": np.cumprod(1 + bl).round(6).tolist(),
                       "drawdown": eq["drawdown"].round(6).tolist(), "gross": eq["gross"].round(4).tolist()},
            "rolling": rolling, "block_bars": block,
            "note": "Intervals describe this sample under the stated assumptions; they are not a probability of "
                    "future profit. Provisional results must not be read as a conclusion."}
