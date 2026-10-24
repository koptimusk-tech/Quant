"""Portfolio backtest over strategy return streams, with the risk manager in the loop.

Timing (no look-ahead)
----------------------
* Every strategy series is indexed by the UTC date on which each bar's return became *available*.
* At the close of a rebalance date d the engine estimates risk from returns available up to d (a trailing
  window), builds weights, applies volatility targeting and the HRM, and the new weights are held from
  d + 1 + ``execution_delay_days``. Nothing from after d enters the decision.
* Between rebalances allocations drift with each sleeve's return. Unallocated capital earns nothing.
* Rebalancing is charged on the look-through change in instrument exposure times each instrument's
  per-side cost (commission + half spread + slippage), times ``cost_multiplier``, plus extra slippage.
  The strategies' own trading costs are already inside their net returns.
* Daily risk checks run at each close: a one-day loss above the limit halves the next day's allocation; a
  drawdown beyond the limit halves or flattens allocations for ``cooldown_days`` and the drawdown reference
  restarts after the cooldown (so a stopped portfolio can resume).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator

from qal.portfolio import construction
from qal.portfolio.risk import covariance, exposures
from qal.portfolio.risk_manager import HRM, Context, PolicyLimits, StrategyState
from qal.portfolio.strategies import StrategyRecord, StrategySeries
from qal.research.metrics import summary

PPY = 252.0


class PortfolioConfig(BaseModel):
    strategy_ids: list[str] = Field(min_length=1)
    methods: list[str] = Field(default_factory=lambda: ["risk_budget", "inverse_vol", "equal_weight"])
    rebalance: str = Field("monthly", description="daily, weekly, monthly, or every N days as '20d'")
    lookback_days: int = Field(252, ge=30)
    cov_method: str = "ledoit_wolf"
    budgets: dict[str, float] = Field(default_factory=dict)
    capital: float = Field(1_000_000.0, gt=0)
    cost_multiplier: float = Field(1.0, ge=0)
    extra_slippage_bps: float = Field(0.0, ge=0)
    execution_delay_days: int = Field(0, ge=0, le=20)
    execution_failure_prob: float = Field(0.0, ge=0, le=1)
    interruptions: list[tuple[str, str]] = Field(default_factory=list,
                                                 description="Date ranges with no trading (positions frozen)")
    oos_fraction: float = Field(0.3, gt=0, lt=1)
    seed: int = 7

    @field_validator("methods")
    @classmethod
    def _methods(cls, v: list[str]) -> list[str]:
        bad = [m for m in v if m not in construction.METHODS]
        if bad or not v:
            raise ValueError(f"unknown method(s) {bad}; use {list(construction.METHODS)}")
        return v

    @field_validator("cov_method")
    @classmethod
    def _cov(cls, v: str) -> str:
        from qal.portfolio.risk import COV_METHODS

        if v not in COV_METHODS:
            raise ValueError(f"cov_method must be one of {COV_METHODS}")
        return v


@dataclass
class Panel:
    ids: list[str]
    dates: pd.DatetimeIndex
    net: pd.DataFrame
    gross: pd.DataFrame
    cost: pd.DataFrame
    expo: dict[str, pd.DataFrame]  # sid -> exposures aligned to dates (forward-filled, last known)
    cost_bps: dict[str, float]
    asset_class: dict[str, str]
    dollar_volume: dict[str, float | None]
    max_participation: dict[str, float]

    def __post_init__(self) -> None:
        self._expo_np = {s: (list(df.columns), df.to_numpy(dtype=float)) for s, df in self.expo.items()}

    def expo_at(self, t: int) -> dict[str, dict[str, float]]:
        """Last known position per instrument of every strategy at day t (empty before the first day)."""
        if t < 0:
            return {s: {} for s in self.ids}
        out = {}
        for s, (cols, A) in self._expo_np.items():
            row = A[t]
            out[s] = {c: float(v) for c, v in zip(cols, row, strict=True) if np.isfinite(v)}
        return out


def make_panel(series: dict[str, StrategySeries], records: dict[str, StrategyRecord], ids: list[str]) -> Panel:
    dates = pd.DatetimeIndex(sorted(set().union(*[set(series[s].net.index) for s in ids])))
    net = pd.DataFrame({s: series[s].net.reindex(dates) for s in ids})
    gross = pd.DataFrame({s: series[s].gross.reindex(dates) for s in ids})
    cost = pd.DataFrame({s: series[s].cost.reindex(dates) for s in ids})
    expo = {s: series[s].exposure.reindex(dates).ffill() for s in ids}
    cost_bps, ac, dv, mp = {}, {}, {}, {}
    for s in ids:
        cost_bps.update(series[s].cost_bps)
        ac.update(series[s].asset_class)
        dv.update(series[s].dollar_volume)
        mp.update(records[s].liquidity.get("max_participation") or {})
    return Panel(ids, dates, net, gross, cost, expo, cost_bps, ac, dv, mp)


def rebalance_dates(dates: pd.DatetimeIndex, rule: str) -> set[pd.Timestamp]:
    if rule == "daily":
        return set(dates)
    if rule in ("weekly", "monthly"):
        key = dates.tz_localize(None).to_period("W" if rule == "weekly" else "M")
        s = pd.Series(dates, index=dates)
        return set(s.groupby(key).max())  # last available day of each period
    if rule.endswith("d") and rule[:-1].isdigit():
        n = int(rule[:-1])
        return set(dates[n - 1::n])
    raise ValueError("rebalance must be daily, weekly, monthly or '<N>d'")


def _strategy_dd(r: pd.Series) -> float:
    r = r.dropna()
    if len(r) < 2:
        return 0.0
    eq = (1 + r).cumprod()
    return float(eq.iloc[-1] / eq.max() - 1)


def run(panel: Panel, records: dict[str, StrategyRecord], cfg: PortfolioConfig, method: str, limits: PolicyLimits,
        policy_version: str, *, use_hrm: bool = True, vol_target: bool = True, until: pd.Timestamp | None = None
        ) -> dict[str, Any]:
    """Simulate one construction method. ``until`` truncates the data (used by the look-ahead check)."""
    dates = panel.dates if until is None else panel.dates[panel.dates <= until]
    ids = panel.ids
    n = len(ids)
    hrm = HRM(limits, policy_version)
    P = limits.portfolio
    rb = rebalance_dates(dates, cfg.rebalance)
    rng = np.random.default_rng(cfg.seed)
    frozen = [(pd.Timestamp(a, tz="UTC"), pd.Timestamp(b, tz="UTC")) for a, b in cfg.interruptions]

    def is_frozen(d: pd.Timestamp) -> bool:
        return any(a <= d <= b for a, b in frozen)

    cm = cfg.cost_multiplier
    netR = (panel.gross - cm * panel.cost) if cm != 1.0 else panel.net
    netA, grossA = netR.to_numpy(dtype=float), panel.gross.to_numpy(dtype=float)
    w = np.zeros(n)
    pending: list[tuple[int, np.ndarray, int]] = []  # (effective index, weights, decision index)
    executed: list[tuple[int, int]] = []  # (decision index, execution index)
    rows, wlog, decisions, daily_blocks = [], [], [], []
    peak, equity = 1.0, 1.0
    derisk_until, derisk_factor, one_day = -1, 1.0, False
    attempts, failures = [], []
    for t, d in enumerate(dates):
        # 1. Execute any decision that becomes effective today (decided on an earlier close).
        cost_t = 0.0
        turnover = 0.0
        if pending and pending[0][0] == t and not is_frozen(d):
            _, new_w, decided = pending.pop(0)
            executed.append((decided, t))
            prev_ex = exposures(dict(zip(ids, w, strict=True)), panel.expo_at(t - 1), panel.asset_class)["instrument"]
            new_ex = exposures(dict(zip(ids, new_w, strict=True)), panel.expo_at(t - 1), panel.asset_class)["instrument"]
            for sym in set(prev_ex) | set(new_ex):
                dx = abs(new_ex.get(sym, 0.0) - prev_ex.get(sym, 0.0))
                turnover += dx
                cost_t += dx * (panel.cost_bps.get(sym, 2.0) * cm + cfg.extra_slippage_bps) / 1e4
            w = new_w.copy()
        elif pending and pending[0][0] == t:
            pending[0] = (t + 1, pending[0][1], pending[0][2])  # market closed: the order waits
        scale = derisk_factor if t <= derisk_until else 1.0
        if one_day:
            scale *= 0.5
        held = w * scale
        r = netA[t]
        g = grossA[t]
        r0 = np.nan_to_num(r)
        rp = float(held @ r0) - cost_t
        gp = float(held @ np.nan_to_num(g))
        ex = exposures(dict(zip(ids, held, strict=True)), panel.expo_at(t), panel.asset_class)
        rows.append({"ts": d, "gross": gp, "cost": gp - rp, "net": rp,
                     "rebalance_cost": cost_t, "leverage": float(held.sum()), "gross_exposure": ex["gross"],
                     "net_exposure": ex["net"], "turnover": turnover})
        # drift
        denom = 1 + float(w @ r0)
        if denom > 0:
            w = w * (1 + r0) / denom
        equity *= 1 + rp
        peak = max(peak, equity)
        dd = equity / peak - 1
        one_day = False
        # 2. Daily checks at the close.
        if use_hrm:
            if rp < -P.max_daily_loss:
                one_day = True
                daily_blocks.append(_daily(d, "max_daily_loss", rp, -P.max_daily_loss,
                                           "one-day loss beyond limit: next day's allocation halved", policy_version))
            if dd < -P.max_drawdown and t > derisk_until:
                derisk_until = t + P.cooldown_days
                derisk_factor = 0.5 if P.drawdown_action == "halve" else 0.0
                daily_blocks.append(_daily(d, "max_drawdown", dd, -P.max_drawdown,
                                           f"portfolio drawdown beyond limit: allocation {P.drawdown_action} for "
                                           f"{P.cooldown_days} days, then the drawdown reference restarts",
                                           policy_version))
            if t == derisk_until:
                peak = equity
        # 3. Rebalance decision at the close of a rebalance date.
        if d in rb and not is_frozen(d):
            dec = _decide(panel, records, cfg, method, hrm, t, dates, w, use_hrm, vol_target, netR,
                          failures[-12:] if failures else [], limits)
            if dec is None:
                continue
            attempts.append(t)
            failed = cfg.execution_failure_prob > 0 and rng.random() < cfg.execution_failure_prob
            failures.append(bool(failed))
            wlog.append({**dec["log"], "execution_failed": bool(failed)})
            decisions.extend(dec["decisions"])
            if not failed:
                eff = t + 1 + cfg.execution_delay_days
                if eff < len(dates):
                    pending = [p for p in pending if p[0] < eff] + [(eff, dec["weights"], t)]
    bars = pd.DataFrame(rows)
    return {"bars": bars, "weights": wlog, "decisions": decisions + daily_blocks, "daily_blocks": daily_blocks,
            "final_weights": dict(zip(ids, w.tolist(), strict=True)), "executed": executed,
            "last_target": wlog[-1]["final"] if wlog else {}}


def _daily(d: pd.Timestamp, rule: str, obs: float, lim: float, reason: str, version: str) -> dict[str, Any]:
    return {"ts": str(d), "level": "portfolio", "scope": "portfolio", "rule": rule, "observed": round(obs, 6),
            "limit": lim, "decision": "blocked", "action": "de-risk", "reason": reason, "policy_version": version}


def _decide(panel: Panel, records: dict[str, StrategyRecord], cfg: PortfolioConfig, method: str, hrm: HRM, t: int,
            dates: pd.DatetimeIndex, w_now: np.ndarray, use_hrm: bool, vol_target: bool, netR: pd.DataFrame,
            recent_failures: list[bool], limits: PolicyLimits) -> dict[str, Any] | None:
    d = dates[t]
    lo = max(0, t + 1 - cfg.lookback_days)
    window = netR.iloc[lo:t + 1]  # returns available up to the close of d
    hist = netR.iloc[:t + 1]
    counts = window.notna().sum()
    min_hist = limits.strategy.min_history_days
    active = [s for s in panel.ids if counts[s] >= min(min_hist, cfg.lookback_days // 2)]
    if not active:
        return None
    try:
        C = covariance(window[active], cfg.cov_method, min_obs=20)
    except ValueError:
        return None
    wa = construction.build(method, window[active], C, cap=limits.strategy.max_weight, budgets=cfg.budgets)
    scale, vol0 = construction.vol_target_scale(wa, C, limits.portfolio.target_vol if vol_target else None,
                                                limits.portfolio.max_leverage)
    proposed = {s: 0.0 for s in panel.ids}
    for s, v in zip(active, wa * scale, strict=True):
        proposed[s] = float(v)
    decisions: list[dict[str, Any]] = []
    final = dict(proposed)
    if use_hrm:
        states = {}
        expo_now = panel.expo_at(t)
        for s in panel.ids:
            col = hist[s]
            last = col.last_valid_index()
            states[s] = StrategyState(group=records[s].group, counterparty=records[s].counterparty,
                                      quality=records[s].data_quality, last_available=last,
                                      history_days=int(col.notna().sum()), drawdown=_strategy_dd(col.tail(cfg.lookback_days)),
                                      exposure=expo_now[s])
        # covariance used by the HRM covers the active strategies; inactive ones are zeroed by its rules
        Cfull = C.reindex(index=panel.ids, columns=panel.ids).fillna(0.0)
        ctx = Context(as_of=d, strategies=states, cov=Cfull, asset_class=panel.asset_class,
                      dollar_volume=panel.dollar_volume, max_participation=panel.max_participation,
                      capital=cfg.capital,
                      execution_failure_frac=float(np.mean(recent_failures)) if recent_failures else 0.0,
                      previous=dict(zip(panel.ids, w_now.tolist(), strict=True)))
        final, decisions = hrm.enforce(proposed, ctx)
    vec = np.array([final.get(s, 0.0) for s in panel.ids])
    Cf = C.reindex(index=panel.ids, columns=panel.ids).fillna(0.0).to_numpy()
    vol_after = float(np.sqrt(max(vec @ Cf @ vec, 0.0)))
    return {"weights": vec, "decisions": decisions,
            "log": {"ts": str(d.date()), "proposed": {k: round(v, 6) for k, v in proposed.items()},
                    "final": {k: round(v, 6) for k, v in final.items()}, "ex_ante_vol_before_target": vol0,
                    "vol_target_scale": scale, "ex_ante_vol_final": vol_after, "active": active,
                    "window": [str(window.index[0].date()), str(d.date())],
                    "blocked_rules": sorted({x["rule"] for x in decisions if x["decision"] == "blocked"})}}


def metrics(bars: pd.DataFrame) -> dict[str, Any]:
    if len(bars) < 2:
        return {"bars": len(bars)}
    m = summary(bars, [], PPY)
    for k in ("n_trades", "hit_rate", "profit_factor", "avg_trade_net", "cost_per_trade", "top5_trades_share",
              "largest_trade_share_abs", "capacity_breaches"):
        m.pop(k, None)
    m["rebalance_cost_total"] = float(bars["rebalance_cost"].sum()) if "rebalance_cost" in bars else None
    m["avg_leverage"] = float(bars["leverage"].mean()) if "leverage" in bars else None
    return m


def weight_stability(wlog: list[dict[str, Any]]) -> dict[str, Any]:
    if len(wlog) < 2:
        return {"rebalances": len(wlog)}
    keys = list(wlog[0]["final"])
    W = np.array([[x["final"].get(k, 0.0) for k in keys] for x in wlog])
    ch = np.abs(np.diff(W, axis=0)).sum(axis=1)
    return {"rebalances": len(wlog), "mean_abs_change": float(ch.mean()), "max_abs_change": float(ch.max()),
            "mean_weights": dict(zip(keys, W.mean(axis=0).tolist(), strict=True))}


def segments(bars: pd.DataFrame, oos_fraction: float) -> dict[str, Any]:
    cut = int(len(bars) * (1 - oos_fraction))
    a, b = bars.iloc[:cut], bars.iloc[cut:]
    return {"first": {"from": str(a["ts"].iloc[0].date()) if len(a) else None, **metrics(a)},
            "last": {"from": str(b["ts"].iloc[0].date()) if len(b) else None, **metrics(b)},
            "note": "Weights are re-estimated causally at every rebalance, so both segments are out of sample for "
                    "the allocation; the split shows whether results hold up in the later period."}


def lookahead_check(panel: Panel, records: dict[str, StrategyRecord], cfg: PortfolioConfig, method: str,
                    limits: PolicyLimits, version: str, full: dict[str, Any]) -> dict[str, Any]:
    """Re-run with data truncated at 60% of the calendar: every decision before the cut must be identical."""
    cut = panel.dates[int(len(panel.dates) * 0.6)]
    part = run(panel, records, cfg, method, limits, version, until=cut)
    a = [x for x in full["weights"] if pd.Timestamp(x["ts"], tz="UTC") < cut]
    b = [x for x in part["weights"] if pd.Timestamp(x["ts"], tz="UTC") < cut]
    same = len(a) == len(b) and all(
        x["ts"] == y["ts"] and all(abs(x["final"][k] - y["final"][k]) < 1e-12 for k in x["final"])
        for x, y in zip(a, b, strict=False))
    return {"check": "truncation_invariance", "passed": bool(same), "cut": str(cut.date()), "decisions_compared": len(a),
            "description": "Weights decided before a date do not change when later data is removed"}
