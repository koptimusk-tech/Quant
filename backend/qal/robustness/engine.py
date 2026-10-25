"""Robustness analyses of one Experiment Lab candidate: markets, periods, parameters, costs, capacity, stress.

Every analysis starts from a COMPLETED lab experiment and its frozen spec. Selection stays where the lab put it:
parameters are chosen on training windows only, and the analyses below either re-run that same selection on
another asset (generalisation) or evaluate *fixed* rules on the walk-forward test windows (diagnostics). Nothing
here chooses a configuration from test data, nothing touches the final reserved period, and nothing can place an
order.

What each number is:

* cross-asset: the same rule and the same parameter-selection procedure on compatible datasets, plus the design
  asset's selected parameters transferred without re-selection. Correlated targets are flagged and the effective
  number of independent assets is reported, so ten near-identical ETFs do not count as ten markets.
* temporal: all predefined walk-forward windows, calendar years and causal regime labels (volatility, trend vs
  range, drawdown stress), the unfavourable ones included.
* parameters: one-at-a-time perturbations (x0.75, x1.25) around the selected parameters, each held fixed over
  every test window; the number of configurations is recorded.
* costs and execution: base (declared costs), adverse (x2 costs, +1 bar delay, half the participation limit) and
  severe (x3 costs, +2 bars, a quarter of the participation limit). Market orders at the next open; limit orders
  are simulated only in paper trading and are not evaluated here.
* capacity: observed bar volume plus an *assumed* square-root impact model. OHLCV has no spread or depth, so the
  estimate is approximate by construction; without usable volume it is UNKNOWN.
* stress: deterministic perturbations of the data (synthetic stress tests, not forecasts) and the drawdown
  episodes actually observed in the evaluated period (historical).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from qal.data.store import ParquetStore
from qal.lab.pipeline import MIN_OOS_BARS, Pipeline, _series_metrics
from qal.lab.spec import ExperimentSpec
from qal.models import Dataset, Experiment
from qal.research.backtest import align, run_backtest
from qal.research.costs import CostModel, default_cost_model
from qal.research.metrics import summary
from qal.research.validation import Validator

ROBUSTNESS_VERSION = "robustness@1.0.0"

EXECUTION_SCENARIOS: dict[str, dict[str, float]] = {
    "base": {"cost_multiplier": 1.0, "delay_bars": 0, "participation_factor": 1.0},
    "adverse": {"cost_multiplier": 2.0, "delay_bars": 1, "participation_factor": 0.5},
    "severe": {"cost_multiplier": 3.0, "delay_bars": 2, "participation_factor": 0.25},
}
PERTURB = (0.75, 1.25)
CORRELATED = 0.8  # |daily return correlation| above which two assets are flagged as near-duplicates
MIN_REGIME_BARS = 120


class Cancelled(RuntimeError):
    pass


@dataclass
class Context:
    """A lab candidate rebuilt from its stored spec: frames, costs, validator and the walk-forward selection."""

    exp: Experiment
    spec: ExperimentSpec
    datasets: list[Dataset]
    frames: list[pd.DataFrame]
    names: list[str]
    asset_class: str
    costs: list[CostModel]
    pipe: Pipeline

    @property
    def v(self) -> Validator:
        return self.pipe.v

    @property
    def wf(self) -> dict[str, Any]:
        return self.pipe.wf

    @property
    def ppy(self) -> float:
        return float(self.wf["ppy"])

    def chosen(self) -> list[dict[str, Any]]:
        return [self.wf["variants"][i] for i in self.wf["chosen_idx"]]

    def oos_index(self) -> np.ndarray:
        return np.concatenate([np.arange(w["test_start"], w["test_end"]) for w in self.wf["wins"]])


def build_context(s: Session, exp: Experiment, store: ParquetStore,
                  frames: list[pd.DataFrame] | None = None) -> Context:
    """Rebuild a candidate. ``frames`` replaces the stored data (tests use it to perturb the final period)."""
    spec = ExperimentSpec.model_validate(exp.config["spec"])
    dss = [s.get(Dataset, d) for d in spec.contract.dataset_ids]
    for d, ref in zip(dss, exp.data_refs, strict=True):
        if d is None or d.content_hash != ref["content_hash"]:
            raise ValueError("a dataset of the experiment changed or disappeared since it ran")
    frames = frames or [store.read(d.path) for d in dss]
    names = [d.instrument.symbol for d in dss]
    ac = dss[0].instrument.asset_class
    costs = [spec.contract.costs.get(d.id) or default_cost_model(d.instrument.asset_class) for d in dss]
    pipe = Pipeline(spec, frames, names, ac, costs, {n: d.quality_status for n, d in zip(names, dss, strict=True)})
    checks = pipe.integrity_pre()
    bad = [c["check"] for c in checks if c["critical"] and not c["passed"]]
    if bad:
        raise ValueError(f"critical integrity checks fail now: {bad}")
    pipe.wf = pipe.v.walk_forward()
    return Context(exp, spec, dss, frames, names, ac, costs, pipe)


# -- shared evaluation -----------------------------------------------------------------------------------
def evaluate_fixed(ctx: Context, per_window: list[dict[str, Any]], *, frames: list[pd.DataFrame] | None = None,
                   cost_multiplier: float = 1.0, delay: int = 0, participation_factor: float = 1.0,
                   capital: float | None = None) -> dict[str, Any]:
    """Run fixed per-window parameters on the research period (optionally perturbed data or execution) and keep
    the walk-forward test windows. Returns net/gross/cost series, positions and capacity breaches."""
    research = frames or ctx.v.research
    costs = ctx.v.costs if participation_factor == 1.0 else [
        c.model_copy(update={"max_participation": max(1e-6, c.max_participation * participation_factor)})
        for c in ctx.v.costs]
    cache: dict[str, Any] = {}
    nets, gross, cost, pos, breaches = [], [], [], [], 0
    for wd, params in zip(ctx.wf["wins"], per_window, strict=True):
        key = repr(sorted(params.items()))
        if key not in cache:
            W = ctx.pipe.cls(**params).weights(research)
            if delay:
                W = W.shift(delay).fillna(0.0)
            cache[key] = run_backtest(research, W, costs, capital=capital or ctx.spec.contract.capital,
                                      asset_class=ctx.asset_class, names=ctx.names, cost_multiplier=cost_multiplier)
        bt = cache[key]
        sl = slice(wd["test_start"], wd["test_end"])
        nets.append(bt.bars["net"].to_numpy()[sl])
        gross.append(bt.bars["gross"].to_numpy()[sl])
        cost.append(bt.bars["cost"].to_numpy()[sl])
        pos.append(bt.positions[ctx.names].to_numpy()[sl])
    breaches = sum(b.capacity_breaches for b in cache.values())
    return {"net": np.concatenate(nets), "gross": np.concatenate(gross), "cost": np.concatenate(cost),
            "positions": np.vstack(pos), "capacity_breaches": int(breaches)}


def _metrics(r: np.ndarray, ppy: float) -> dict[str, Any]:
    return _series_metrics(np.asarray(r, float), ppy)


# -- 1. cross-asset ------------------------------------------------------------------------------------------
def compatible_targets(s: Session, ctx: Context, ids: list[str] | None, limit: int) -> list[Dataset]:
    from sqlalchemy import select

    if ctx.pipe.cls.n_assets != 1:
        return []
    design = ctx.datasets[0]
    q = select(Dataset).where(Dataset.is_latest.is_(True), Dataset.frequency == design.frequency)
    rows = [d for d in s.scalars(q).all() if d.id != design.id and d.quality_status != "fail"
            and bool(d.is_synthetic) == bool(design.is_synthetic)]
    if ids is not None:
        rows = [d for d in rows if d.id in set(ids)]
    rows.sort(key=lambda d: (d.instrument.asset_class != design.instrument.asset_class, d.instrument.symbol))
    return rows[:limit]


def _daily_returns(f: pd.DataFrame) -> pd.Series:
    px = f["adj_close"].where(f["adj_close"].notna(), f["close"]) if "adj_close" in f else f["close"]
    r = pd.Series(np.log(px.to_numpy(float)), index=pd.to_datetime(f["ts"], utc=True).dt.normalize()).diff()
    return r.groupby(level=0).sum().dropna()


def effective_n(corr: np.ndarray) -> float:
    """n^2 / sum of squared correlations: n for independent assets, 1 for identical ones."""
    c = np.nan_to_num(np.asarray(corr, float))
    return float(len(c) ** 2 / max((c ** 2).sum(), 1e-12)) if len(c) else 0.0


def cross_asset(s: Session, ctx: Context, store: ParquetStore, *, ids: list[str] | None, limit: int,
                check: Callable[[], None]) -> dict[str, Any]:
    design = ctx.datasets[0]
    base_row = _matrix_row(ctx, design, "design", ctx.wf, ctx.v, ctx.costs[0], explicit=design.id in ctx.spec.contract.costs)
    if ctx.pipe.cls.n_assets != 1:
        return {"rows": [base_row], "not_applicable": "cross-asset evaluation of pair and multi-asset universes is not "
                "implemented in this version: only the design universe is shown", "n_effective": None}
    rows = [base_row]
    rets = {design.instrument.symbol: _daily_returns(ctx.frames[0])}
    transfer = ctx.chosen()[-1]
    for d in compatible_targets(s, ctx, ids, limit):
        check()
        cat = "same_market" if d.instrument.asset_class == design.instrument.asset_class else "other_market"
        f = store.read(d.path)
        cm = default_cost_model(d.instrument.asset_class)
        c2 = ctx.spec.contract.model_copy(update={"dataset_ids": [d.id], "costs": {}})
        try:
            v2 = Validator(c2, [f], d.instrument.asset_class, [d.instrument.symbol], [cm])
            wf2 = v2.walk_forward()
        except ValueError as exc:
            rows.append(_skeleton(d, cat, cm) | {"status": "insufficient_data", "reason": str(exc)[:300]})
            continue
        row = _matrix_row(ctx, d, cat, wf2, v2, cm, explicit=False)
        # transfer: the design asset's last selected parameters, no re-selection on the target
        bt = v2.backtest(v2.research, transfer)
        idx = np.concatenate([np.arange(w["test_start"], w["test_end"]) for w in wf2["wins"]])
        row["transfer_fixed_params"] = {"params": transfer, **_metrics(bt.bars["net"].to_numpy()[idx], wf2["ppy"])}
        rows.append(row)
        rets[d.instrument.symbol] = _daily_returns(f)
    syms = [r["symbol"] for r in rows if r["status"] == "evaluated"]
    R = pd.DataFrame({k: rets[k] for k in syms}).dropna()
    corr = R.corr().to_numpy() if len(R) > 20 and len(syms) > 1 else np.eye(len(syms))
    d0 = syms.index(design.instrument.symbol) if design.instrument.symbol in syms else 0
    for r in rows:
        if r["status"] == "evaluated" and r["symbol"] in syms:
            rho = float(corr[d0, syms.index(r["symbol"])])
            r["corr_with_design"] = round(rho, 3)
            r["near_duplicate_of_design"] = r["category"] != "design" and abs(rho) >= CORRELATED
    return {"rows": rows, "n_effective": round(effective_n(corr), 2), "correlation": {"symbols": syms,
            "matrix": np.round(corr, 3).tolist(), "days": len(R)},
            "transfer_params": transfer,
            "note": "Re-selected = the same grid and walk-forward selection on the target; transfer = the design "
                    "asset's selected parameters applied unchanged. Target costs are asset-class defaults (estimates)."}


def _skeleton(d: Dataset, cat: str, cm: CostModel) -> dict[str, Any]:
    return {"dataset_id": d.id, "symbol": d.instrument.symbol, "asset_class": d.instrument.asset_class,
            "market": d.instrument.market, "timeframe": d.frequency, "category": cat, "provider": d.provider_id,
            "quality_status": d.quality_status, "is_synthetic": d.is_synthetic,
            "period": [str(d.start_ts)[:10], str(d.end_ts)[:10]], "costs": cm.model_dump(),
            "cost_source": "asset-class default (estimate)"}


def _matrix_row(ctx: Context, d: Dataset, cat: str, wf: dict[str, Any], v: Validator, cm: CostModel, *,
                explicit: bool) -> dict[str, Any]:
    from qal.research.backtest import extract_trades

    oos, pos = wf["oos"], wf["oos_pos"]
    trades = extract_trades(pos.reset_index(drop=True), oos.reset_index(drop=True), v.names)
    m = summary(oos, trades, wf["ppy"])
    row = _skeleton(d, cat, cm) | {
        "status": "evaluated", "validation": f"walk-forward train={v.c.validation.train_bars} test="
                                             f"{v.c.validation.test_bars} embargo={v.c.validation.embargo_bars}, "
                                             "selection on training windows only; final period untouched",
        "oos_from": str(oos["ts"].iloc[0])[:10], "oos_to": str(oos["ts"].iloc[-1])[:10],
        "bars": m.get("bars"), "n_trades": m.get("n_trades"), "mean_net": m.get("mean_net"),
        "sharpe_gross": m.get("sharpe_gross"), "sharpe_net": m.get("sharpe_net"),
        "total_return_gross": m.get("total_return_gross"), "total_return_net": m.get("total_return_net"),
        "max_drawdown": m.get("max_drawdown"), "turnover_ann": m.get("turnover_ann"),
        "sample": "sufficient" if (m.get("bars") or 0) >= MIN_OOS_BARS and (m.get("n_trades") or 0) >= 20 else "small"}
    if explicit:
        row["cost_source"] = "declared in the experiment contract"
    return row


# -- 2. temporal ---------------------------------------------------------------------------------------------
def regime_labels(f: pd.DataFrame) -> pd.DataFrame:
    """Causal labels (each uses data up to the previous bar): volatility, trend vs range, drawdown stress."""
    c = f["close"].astype(float)
    r = np.log(c).diff()
    vol = r.rolling(20).std().shift(1)
    vol_med = vol.expanding(60).median()
    move = (c - c.shift(60)).abs()
    path = c.diff().abs().rolling(60).sum()
    er = (move / path).shift(1)
    dd = (c / c.rolling(252, min_periods=60).max() - 1).shift(1)
    return pd.DataFrame({
        "volatility": np.where(vol.isna() | vol_med.isna(), "unknown", np.where(vol > vol_med, "high_vol", "low_vol")),
        "trend": np.where(er.isna(), "unknown", np.where(er > 0.3, "trending", "range_bound")),
        "stress": np.where(dd.isna(), "unknown", np.where(dd < -0.15, "drawdown_stress", "normal"))})


def temporal(ctx: Context) -> dict[str, Any]:
    oos = ctx.wf["oos"]["net"].to_numpy()
    ppy = ctx.ppy
    pipeline = ctx.exp.results["pipeline"]
    windows, at = [], 0
    for w, wd in zip(pipeline["D_robustness"]["windows"], ctx.wf["wins"], strict=True):
        n = wd["test_end"] - wd["test_start"]
        windows.append({**w, **_window_uncertainty(oos[at: at + n], ppy)})  # oos = the windows concatenated
        at += n
    years = [y for y in pipeline["D_robustness"]["years"]]
    lab_frames = ctx.v.research
    ref = lab_frames[0] if len(lab_frames) == 1 else _equal_weight_frame(lab_frames)
    labels = regime_labels(ref).iloc[ctx.oos_index()].reset_index(drop=True)
    regimes = {}
    for col in labels.columns:
        regimes[col] = {}
        for lab in sorted(set(labels[col])):
            m = (labels[col] == lab).to_numpy()
            regimes[col][lab] = _metrics(oos[m], ppy)
    eligible_years = [y for y in years if (y.get("bars") or 0) >= 60]
    out = {"windows": windows, "years": years, "regimes": regimes,
           "training_length": pipeline["D_robustness"].get("training_length", []),
           "n_windows": len(windows),
           "positive_window_frac": _frac([w.get("mean_net") for w in windows]),
           "positive_year_frac": _frac([y.get("mean_net") for y in eligible_years]),
           "regime_dependence": [f"{col}={lab}: mean net {v['mean_net']:.2e} over {v['bars']} bars"
                                 for col, d in regimes.items() for lab, v in d.items()
                                 if lab != "unknown" and v.get("bars", 0) >= MIN_REGIME_BARS
                                 and (v.get("mean_net") or 0) < 0],
           "note": "Every predefined window and year is shown, the unfavourable ones included. Regime labels use "
                   "only data before each bar."}
    return out


def _window_uncertainty(r: np.ndarray, ppy: float) -> dict[str, Any]:
    """Sample size and an approximate standard error of the annualised Sharpe (Lo 2002, iid returns)."""
    n = len(r)
    sd = float(np.std(r, ddof=1)) if n > 1 else 0.0
    sr = float(np.mean(r)) / sd if sd > 0 else 0.0
    return {"bars": n, "trading_bars": int((np.abs(r) > 0).sum()),
            "sharpe_se": round(float(np.sqrt((1 + 0.5 * sr ** 2) / n) * np.sqrt(ppy)), 4) if n > 1 else None}


def _equal_weight_frame(frames: list[pd.DataFrame]) -> pd.DataFrame:
    rets = np.mean([np.log(f["close"].astype(float)).diff().fillna(0).to_numpy() for f in frames], axis=0)
    return pd.DataFrame({"close": np.exp(np.cumsum(rets)) * 100.0})


def _frac(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(v > 0 for v in vals) / len(vals), 4) if vals else None


# -- 3. parameters -------------------------------------------------------------------------------------------
def neighbours(center: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for k, v in center.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v == 0:
            continue
        for f in PERTURB:
            nv: Any = max(1, round(v * f)) if isinstance(v, int) else round(float(v) * f, 6)
            if nv != v:
                out.append((f"{k} x{f}", {**center, k: nv}))
    return out


def parameters(ctx: Context, check: Callable[[], None]) -> dict[str, Any]:
    chosen = ctx.chosen()
    counts: dict[str, int] = {}
    for p in chosen:
        counts[repr(sorted(p.items()))] = counts.get(repr(sorted(p.items())), 0) + 1
    center = max(chosen, key=lambda p: (counts[repr(sorted(p.items()))], chosen.index(p) * -1))
    n_win = len(ctx.wf["wins"])
    rows = []
    base = evaluate_fixed(ctx, [center] * n_win)
    rows.append({"config": "centre (most often selected)", "params": center, **_metrics(base["net"], ctx.ppy)})
    for label, p in neighbours(center):
        check()
        try:
            ctx.pipe.cls(**p)
            r = evaluate_fixed(ctx, [p] * n_win)
            rows.append({"config": label, "params": p, **_metrics(r["net"], ctx.ppy)})
        except (ValueError, TypeError) as exc:
            rows.append({"config": label, "params": p, "not_applicable": str(exc)[:200]})
    ev = [r for r in rows[1:] if "mean_net" in r]
    c_sharpe = rows[0].get("sharpe_net") or 0.0
    return {"centre": center, "rows": rows, "n_configs": len(rows),
            "neighbour_positive_frac": _frac([r["mean_net"] for r in ev]),
            "neighbour_sharpe_median": float(np.median([r.get("sharpe_net") or 0.0 for r in ev])) if ev else None,
            "centre_sharpe": c_sharpe,
            "note": "Each configuration is held fixed over every walk-forward test window. These are diagnostics: "
                    "no configuration is selected from them, and the final period is not used."}


# -- 4. execution scenarios ---------------------------------------------------------------------------------
def execution(ctx: Context) -> dict[str, Any]:
    chosen = ctx.chosen()
    out = {}
    curves = {}
    for name, sc in EXECUTION_SCENARIOS.items():
        r = evaluate_fixed(ctx, chosen, cost_multiplier=sc["cost_multiplier"], delay=int(sc["delay_bars"]),
                           participation_factor=sc["participation_factor"])
        trading = int((np.abs(np.diff(r["positions"], axis=0)) > 1e-12).any(axis=1).sum())
        out[name] = {**sc, **_metrics(r["net"], ctx.ppy), "gross_mean": float(r["gross"].mean()),
                     "cost_mean": float(r["cost"].mean()), "partial_fill_events": r["capacity_breaches"],
                     "trading_bars": trading}
        eq = np.cumprod(1 + r["net"])
        step = max(1, len(eq) // 300)
        curves[name] = eq[::step].round(6).tolist()
    ts = [str(t)[:10] for t in ctx.wf["oos"]["ts"].to_numpy()[:: max(1, len(ctx.wf["oos"]) // 300)]]
    lim = [f"{n}: {'declared in the contract' if d.id in ctx.spec.contract.costs else 'asset-class defaults (estimates)'}"
           f" (commission {m.commission_bps} bps, half-spread {m.half_spread_bps} bps, slippage {m.slippage_bps} bps"
           f" + {m.slippage_range_frac} of the bar range per side)"
           for n, m, d in zip(ctx.names, ctx.costs, ctx.datasets, strict=True)]
    return {"scenarios": out, "curves": {"ts": ts, **curves}, "cost_sources": lim,
            "not_modelled": ["limit orders (simulated only in paper trading)", "historical spreads (not in OHLCV)",
                             "orders rejected by the venue"],
            "note": "Partial fills: trades above the participation limit of the previous bar's dollar volume are "
                    "clipped and counted. Costs are assumptions, not observed fills."}


# -- 5. capacity --------------------------------------------------------------------------------------------
def capacity(ctx: Context, levels: list[float], impact_coefficient: float, min_capacity: float) -> dict[str, Any]:
    r = evaluate_fixed(ctx, ctx.chosen())
    idx = ctx.oos_index()
    vols = []
    for f in ctx.v.research:
        v = f["volume"].to_numpy(float) if "volume" in f else np.full(len(f), np.nan)
        vols.append(v)
    usable = [np.isfinite(v[idx]).mean() >= 0.9 and (v[idx] > 0).mean() >= 0.9 for v in vols]
    base_mean = float(r["net"].mean())
    common = {"assumptions": {"impact_model": "cost = Y x sigma x sqrt(participation) per unit traded, Y = "
                              f"{impact_coefficient} (assumed)", "sigma": "20-bar volatility before the bar",
                              "volume": "previous bar's dollar volume (observed OHLCV)",
                              "participation_limit": [c.max_participation for c in ctx.v.costs]},
              "base_mean_net": base_mean}
    if not all(usable):
        return {**common, "status": "UNKNOWN", "estimate": None,
                "reason": "volume missing or zero on more than 10% of bars for "
                          f"{[n for n, u in zip(ctx.names, usable, strict=True) if not u]}: capacity cannot be estimated"}
    if base_mean <= 0:
        return {**common, "status": "APPROXIMATE", "estimate": 0.0, "reason": "no positive net edge at the base "
                "scenario: there is no capacity to estimate"}
    P = r["positions"]
    trade = np.abs(np.diff(np.vstack([np.zeros((1, P.shape[1])), P]), axis=0))
    rows = []
    sig, dv = [], []
    for j, f in enumerate(ctx.v.research):
        c = f["close"].to_numpy(float)
        sig.append(pd.Series(np.log(c)).diff().rolling(20).std().shift(1).to_numpy()[idx])
        dv.append(np.roll(c * vols[j], 1)[idx])
    sig, dv = np.nan_to_num(np.array(sig).T, nan=0.0), np.array(dv).T
    low_liq = dv <= np.nanquantile(dv, 0.2, axis=0)
    for cap in levels:
        notional = trade * cap
        part = np.where(dv > 0, notional / dv, np.inf)
        impact = (trade * impact_coefficient * sig * np.sqrt(np.where(np.isfinite(part), part, 0.0))).sum(axis=1)
        net = r["net"] - impact
        active = trade > 1e-12
        over = (part > np.array([c.max_participation for c in ctx.v.costs])) & active
        rows.append({"capital": cap, "mean_net_after_impact": float(net.mean()),
                     "sharpe_after_impact": _metrics(net, ctx.ppy).get("sharpe_net"),
                     "participation_p95": float(np.quantile(part[active], 0.95)) if active.any() else 0.0,
                     "trades_over_limit_frac": float(over.sum() / max(1, active.sum())),
                     "notional_in_lowest_liquidity_quintile": float((notional * low_liq).sum() / max(notional.sum(), 1e-12))})
    ok = [x["capital"] for x in rows if x["mean_net_after_impact"] >= 0.5 * base_mean and x["trades_over_limit_frac"] <= 0.05]
    est = max(ok) if ok else 0.0
    return {**common, "status": "APPROXIMATE", "estimate": est, "levels": rows, "min_capacity": min_capacity,
            "rule": "largest tested capital keeping at least half the base net mean after impact with at most 5% of "
                    "trades above the participation limit",
            "note": "Approximate: observed bar volume with an assumed impact model. OHLCV has no spread or depth, so "
                    "this is not a measurement of real market impact."}


# -- 6. stress ----------------------------------------------------------------------------------------------
def _rebuild(f: pd.DataFrame, logret: np.ndarray) -> pd.DataFrame:
    """New close path from log returns, keeping each bar's open/high/low relative to its close."""
    g = f.copy()
    c0 = float(f["close"].iloc[0])
    new_c = c0 * np.exp(np.concatenate([[0.0], np.cumsum(logret[1:])]))
    k = new_c / f["close"].to_numpy(float)
    for col in ("open", "high", "low", "close"):
        g[col] = f[col].to_numpy(float) * k
    if "adj_close" in g:
        g["adj_close"] = g["close"]
    g["high"] = g[["open", "high", "close"]].max(axis=1)
    g["low"] = g[["open", "low", "close"]].min(axis=1)
    return g


def stress_frames(frames: list[pd.DataFrame], idx: np.ndarray) -> dict[str, tuple[str, list[pd.DataFrame] | None]]:
    lo, hi = int(idx.min()), int(idx.max())
    gaps = np.linspace(lo, hi, 7, dtype=int)[1:-1]
    mid = lo + (hi - lo) // 2

    def lr(f: pd.DataFrame) -> np.ndarray:
        return np.log(f["close"].to_numpy(float)) - np.log(f["close"].shift(1).bfill().to_numpy(float))

    def vol2(f):
        r = lr(f)
        return _rebuild(f, r * 2.0)

    def gapped(f):
        g = f.copy()
        k = np.ones(len(f))
        for b in gaps:
            k[b:] *= 0.92
        for col in ("open", "high", "low", "close"):
            g[col] = f[col].to_numpy(float) * k
        if "adj_close" in g:
            g["adj_close"] = g["close"]
        g["high"] = g[["open", "high", "close"]].max(axis=1)
        g["low"] = g[["open", "low", "close"]].min(axis=1)
        return g

    def liquidity(f):
        g = f.copy()
        g["volume"] = f["volume"].astype(float) * 0.1
        return g

    def stale(f):
        g = f.copy()
        for b in range(lo, hi + 1, 20):
            if b > 0:
                pc = float(g["close"].iloc[b - 1])
                for col in ("open", "high", "low", "close"):
                    g.loc[g.index[b], col] = pc
                if "adj_close" in g:
                    g.loc[g.index[b], "adj_close"] = pc
                g.loc[g.index[b], "volume"] = 0.0
        return g

    def regime_break(f):
        r = lr(f)
        r[mid:] = -r[mid:]
        return _rebuild(f, r)

    out: dict[str, tuple[str, list[pd.DataFrame] | None]] = {
        "volatility_x2": ("every log return doubled (volatility shock)", [vol2(f) for f in frames]),
        "price_gaps": ("five -8% gaps at evenly spaced bars of the evaluated span", [gapped(f) for f in frames]),
        "liquidity_drop": ("volume x0.1: more trades clipped by the participation limit", [liquidity(f) for f in frames]),
        "stale_data": ("every 20th bar frozen at the previous close with zero volume", [stale(f) for f in frames]),
        "regime_break": ("returns of the second half of the evaluated span reversed", [regime_break(f) for f in frames]),
    }
    if len(frames) >= 2:
        R = np.array([lr(f) for f in frames])
        m = R.mean(axis=0)
        out["correlation_spike"] = ("each asset's returns blended 50/50 with the cross-sectional mean",
                                    [_rebuild(f, 0.5 * R[j] + 0.5 * m) for j, f in enumerate(frames)])
    else:
        out["correlation_spike"] = ("not applicable: single instrument", None)
    return out


def stress(ctx: Context, check: Callable[[], None]) -> dict[str, Any]:
    chosen = ctx.chosen()
    base = evaluate_fixed(ctx, chosen)
    bm = _metrics(base["net"], ctx.ppy)
    synthetic = [{"scenario": "base", "description": "unperturbed research data", **bm}]
    for name, (desc, frames) in stress_frames(ctx.v.research, ctx.oos_index()).items():
        check()
        if frames is None:
            synthetic.append({"scenario": name, "description": desc, "not_applicable": True})
            continue
        r = evaluate_fixed(ctx, chosen, frames=align(frames))
        synthetic.append({"scenario": name, "description": desc, **_metrics(r["net"], ctx.ppy),
                          "partial_fill_events": r["capacity_breaches"]})
    r5 = evaluate_fixed(ctx, chosen, cost_multiplier=5.0)
    synthetic.append({"scenario": "cost_spike_x5", "description": "every cost component x5", **_metrics(r5["net"], ctx.ppy)})
    subsets = ctx.exp.results["pipeline"]["D_robustness"].get("universe_subsets", [])
    return {"synthetic": synthetic, "historical": _drawdown_episodes(ctx, base["net"]),
            "source_loss": subsets, "risk_controls": risk_control_checks(ctx),
            "note": "Synthetic scenarios are stress tests, not predictions; parameters stay as selected (no "
                    "re-selection on perturbed data). Historical episodes are the drawdowns actually observed in the "
                    "evaluated period."}


def _drawdown_episodes(ctx: Context, net: np.ndarray, k: int = 3) -> list[dict[str, Any]]:
    """The k deepest distinct drawdowns (peak to trough, each ending at a new high) of the reference, deeper than 5%."""
    ref = ctx.v.research[0] if len(ctx.v.research) == 1 else _equal_weight_frame(ctx.v.research)
    c = ref["close"].to_numpy(float)[ctx.oos_index()]
    ts = ctx.wf["oos"]["ts"].to_numpy()
    dd = c / np.maximum.accumulate(c) - 1
    episodes, start = [], None
    for i in range(len(dd) + 1):
        if i < len(dd) and dd[i] < 0:
            start = i - 1 if start is None else start
        elif start is not None:
            t = start + 1 + int(np.argmin(dd[start + 1: i]))
            episodes.append((float(dd[t]), max(start, 0), t))
            start = None
    out = []
    for depth, p, t in sorted(episodes)[:k]:
        if depth >= -0.05:
            break
        seg = net[p: t + 1]
        out.append({"from": str(ts[p])[:10], "to": str(ts[t])[:10], "reference_drawdown": depth,
                    "candidate_return": float(np.prod(1 + seg) - 1), "bars": int(t - p + 1)})
    return out


def risk_control_checks(ctx: Context) -> list[dict[str, Any]]:
    """Live checks that unreliable data stops simulated orders (the paper feed gate the engine relies on)."""
    from qal.paper import feed

    f = ctx.frames[0]
    d = ctx.datasets[0]
    instr = feed.Instr(d.instrument.symbol, d.id, d.instrument_id, d.provider_id, d.instrument.asset_class,
                       d.instrument.currency, d.frequency, {}, d.is_synthetic, d.content_hash, d.quality_status)
    last, prev = f.iloc[[-1]].copy(), f.iloc[-2]
    out = []
    for name, mutate in (("non-positive price", lambda x: x.assign(close=-1.0)),
                         ("price missing", lambda x: x.assign(close=np.nan)),
                         ("bar published before it closed", lambda x: x.assign(available_ts=x["event_ts"] - pd.Timedelta(hours=1)))):
        chk = feed.check_bar(mutate(last), prev, instr, mode="replay", now=None, max_gap_bars=5, max_delay_hours=48,
                             max_staleness_hours=1e9)
        out.append({"check": f"{name} blocks simulated orders", "passed": chk.status == "fail",
                    "evidence": "qal.paper.feed.check_bar returns fail; the paper engine pauses the session before "
                                "any order (tests/test_paper.py)"})
    return out


# -- 7. diversification -------------------------------------------------------------------------------------
def diversification(s: Session, ctx: Context, peers: list[Experiment]) -> dict[str, Any]:
    def series(e: Experiment) -> pd.Series:
        ser = e.results["pipeline"]["oos_series"]
        r = pd.Series(ser["net"], index=pd.to_datetime(ser["ts"], utc=True).normalize())
        return (1 + r).groupby(level=0).prod() - 1

    if not peers:
        return {"status_hint": "no_peers", "reason": "no other completed, non-rejected lab experiment to combine with"}
    cand = series(ctx.exp)
    P = pd.DataFrame({e.id: series(e) for e in peers})
    df = pd.concat([cand.rename("candidate"), P], axis=1).dropna(how="all").fillna(0.0)
    df = df[(df != 0).any(axis=1)]
    if len(df) < 60:
        return {"status_hint": "too_short", "reason": f"only {len(df)} common days with returns (minimum 60)"}
    ref = df[P.columns].mean(axis=1)
    comb = df.mean(axis=1)
    stress_days = ref <= ref.quantile(0.1)

    def st(r: pd.Series) -> dict[str, Any]:
        return _metrics(r.to_numpy(), 252.0)

    eq = (1 + ref).cumprod()
    trough = int(np.argmin((eq / eq.cummax() - 1).to_numpy()))
    peak = int(np.argmax(eq.to_numpy()[: trough + 1])) if trough > 0 else 0
    active_c = df["candidate"] != 0
    active_r = (df[P.columns] != 0).any(axis=1)
    roll = df["candidate"].rolling(126).corr(ref).dropna()
    return {"peers": [{"id": e.id, "title": e.config.get("title")} for e in peers], "days": len(df),
            "reference": st(ref), "combined": st(comb), "candidate": st(df["candidate"]),
            "corr_with_reference": float(df["candidate"].corr(ref)),
            "corr_on_stress_days": float(df["candidate"][stress_days].corr(ref[stress_days])),
            "rolling_corr_126d": {"min": float(roll.min()) if len(roll) else None,
                                  "max": float(roll.max()) if len(roll) else None},
            "active_day_overlap": float((active_c & active_r).sum() / max(1, active_c.sum())),
            "candidate_return_in_reference_max_drawdown": float(np.prod(1 + df["candidate"].iloc[peak: trough + 1]) - 1),
            "note": "Equal weights over the same days and methodology. A low historical correlation is not a "
                    "guarantee of diversification in stress. Nothing here changes paper trading allocations."}


__all__ = ["EXECUTION_SCENARIOS", "ROBUSTNESS_VERSION", "Cancelled", "Context", "build_context", "capacity",
           "cross_asset", "diversification", "effective_n", "evaluate_fixed", "execution", "neighbours", "parameters",
           "regime_labels", "risk_control_checks", "stress", "stress_frames", "temporal"]
