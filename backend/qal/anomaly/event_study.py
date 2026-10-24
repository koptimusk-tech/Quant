"""Event study: do detected events of one type anticipate returns on this dataset, out of sample?

Design (fixed before looking at results; version 2):
* Events come from re-running the detector on the stored snapshot. Detectors are causal
  (``tests/test_no_lookahead.py``), so every event uses only bars up to its own close.
* Entry is at the OPEN of the first bar whose start ``ts`` is at or after the event's ``available_ts``:
  the event could not have been acted on earlier. Exit is at the close of the h-th bar held. Events whose
  holding period runs past the end of the data are dropped, never truncated.
* Two rules per horizon: continuation (trade in the direction of the event bar's return) and reversal.
  For pair divergences the position is on the spread (long Y, short beta X, beta fixed at the event):
  reversion trades against the divergence, continuation with it; both legs pay costs.
* Predictive information and profitability are kept apart:
  - excess return = gross return minus what the same direction earned on average over the same horizon,
    from every earlier bar whose holding period had closed before the entry (a causal unconditional
    benchmark; the full-sample mean is reported for context only). Events with fewer than
    MIN_BASELINE_BARS earlier windows are left out. Predictive verdicts use the excess return only.
  - net return = gross return minus costs, reported per explicit cost scenario. Costs never decide the
    predictive verdict; each scenario gets its own out-of-sample "survives costs" answer.
* Costs: explicit scenarios labelled as estimates (commission, half spread and fixed slippage per side,
  plus a fraction of the previous bar's range). When a historical spread series is supplied, the
  ``historical_spread`` scenario uses the spread of the bar BEFORE entry and before exit (known then).
* Only independent events: within a horizon an event is skipped while the previous kept trade is open.
* Split: in-sample = trades that EXIT before the time of the event at the 70th percentile of all detected
  events; out of sample = trades whose event is at or after it. Trades straddling the split are purged.
  The horizon x rule is chosen in-sample by the t-statistic of the excess return among combinations with
  at least MIN_SELECTION_EVENTS in-sample trades; only that one is evaluated out of sample.
* Walk-forward: events are cut into WALK_FORWARD_FOLDS chronological folds; for each fold from the second,
  the combination is re-selected on all earlier folds and evaluated on that fold.
* Multiple testing: Benjamini-Hochberg over all horizon x rule combinations of the study (in-sample). A
  batch of studies can add a run-wide correction on top (``qal.research.predictive``).
* Bootstraps use a generator seeded from (seed, horizon, rule), so a row does not depend on the others.
* Verdicts are conservative and never mean "validated strategy": that needs a registered hypothesis and
  Gates A-D.
"""

from __future__ import annotations

import time
import zlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal import audit
from qal.anomaly.detectors import DETECTORS
from qal.anomaly.engine import benjamini_hochberg
from qal.data.schema import total_return_index
from qal.data.store import ParquetStore
from qal.models import Dataset, Experiment
from qal.research.costs import default_cost_model
from qal.versioning import code_version, stable_hash

VERSION = "2.0.0"
DEFAULT_HORIZONS = [1, 5, 10, 20]
IN_SAMPLE_FRACTION = 0.7
MIN_INDEPENDENT_EVENTS = 30
MIN_OOS_EVENTS = 10
MIN_SELECTION_EVENTS = 10
WALK_FORWARD_FOLDS = 5
BOOTSTRAP_RUNS = 2000
ALPHA = 0.05
RULES = ("continuation", "reversal")
PAIR_RULES = ("reversion", "continuation")

VERDICT_TEXT = {
    "insufficient_sample": "Too few independent events to say anything; any return shown is anecdotal.",
    "no_evidence": "No horizon or direction shows excess returns over the unconditional drift that survive "
                   "multiple-testing control in-sample.",
    "in_sample_only": "Excess returns appear in-sample after multiple-testing control but are not confirmed out "
                      "of sample. Treat it as a hypothesis, not evidence.",
    "oos_supported": "The rule chosen in-sample kept a positive excess return out of sample, with a confidence "
                     "interval above zero. Predictive information is not profitability: check the cost "
                     "scenarios, then register and validate a hypothesis.",
}


@dataclass(frozen=True)
class CostScenario:
    """Per-side costs in bps of notional. Estimates unless ``source`` says otherwise."""

    name: str
    commission_bps: float
    half_spread_bps: float
    slippage_bps: float
    slippage_range_frac: float = 0.0
    source: str = "estimate"  # or "historical_spread"

    @property
    def fixed_side_bps(self) -> float:
        return self.commission_bps + self.half_spread_bps + self.slippage_bps


def default_cost_scenarios(asset_class: str) -> list[CostScenario]:
    """Low / base / high estimates around the asset-class cost model (``qal.research.costs``)."""
    cm = default_cost_model(asset_class)
    base = CostScenario("base_estimate", cm.commission_bps, cm.half_spread_bps, cm.slippage_bps,
                        cm.slippage_range_frac)
    return [
        CostScenario("zero_cost", 0.0, 0.0, 0.0, 0.0, source="reference"),
        CostScenario("low_estimate", base.commission_bps * 0.5, base.half_spread_bps * 0.5, base.slippage_bps * 0.5,
                     base.slippage_range_frac * 0.5),
        base,
        CostScenario("high_estimate", base.commission_bps * 2, base.half_spread_bps * 2, base.slippage_bps * 2,
                     base.slippage_range_frac * 2),
    ]


def _rng(seed: int, *key: Any) -> np.random.Generator:
    return np.random.default_rng([seed, zlib.crc32("|".join(map(str, key)).encode())])


def _stats(x: np.ndarray, rng: np.random.Generator, prefix: str = "") -> dict[str, Any]:
    """Mean, t-test and bootstrap CI of per-trade values (bps)."""
    n = len(x)
    out: dict[str, Any] = {f"{prefix}n": n}
    if n == 0:
        return out
    mean = float(x.mean())
    sd = float(x.std(ddof=1)) if n > 1 else float("nan")
    out[f"{prefix}mean"] = mean
    out[f"{prefix}std"] = sd
    if n >= 3 and sd > 0:
        t = mean / (sd / np.sqrt(n))
        out[f"{prefix}t_stat"] = float(t)
        out[f"{prefix}pvalue"] = float(2 * stats.t.sf(abs(t), n - 1))
        boots = rng.choice(x, size=(BOOTSTRAP_RUNS, n), replace=True).mean(axis=1)
        out[f"{prefix}ci95"] = [float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))]
    else:
        out[f"{prefix}t_stat"] = None
        out[f"{prefix}pvalue"] = None
        out[f"{prefix}ci95"] = None
    return out


def _trade_stats(gross: np.ndarray, excess: np.ndarray, costs: dict[str, np.ndarray], base: str,
                 rng_seed: tuple) -> dict[str, Any]:
    """Statistics of one set of trades; the ``*_net_bps`` fields use the ``base`` cost scenario."""
    n = len(gross)
    out: dict[str, Any] = {"n": n}
    if n == 0:
        return out
    ex = _stats(excess, _rng(*rng_seed, "excess"))
    out.update({"mean_gross_bps": float(gross.mean()), "mean_excess_bps": ex["mean"], "excess_t_stat": ex["t_stat"],
                "pvalue": ex["pvalue"], "ci95_excess_bps": ex["ci95"],
                "hit_rate_gross": float((gross > 0).mean())})
    net_by: dict[str, Any] = {}
    for name, cost in costs.items():
        net = gross - cost
        st = _stats(net, _rng(*rng_seed, "net", name))
        equity = np.cumsum(net)
        net_by[name] = {"mean_cost_bps": float(cost.mean()), "mean_net_bps": st["mean"], "ci95_net_bps": st["ci95"],
                        "pvalue_net": st["pvalue"], "hit_rate": float((net > 0).mean()),
                        "worst_trade_bps": float(net.min()), "total_net_bps": float(equity[-1]),
                        "max_drawdown_bps": float((np.maximum.accumulate(np.r_[0.0, equity])[1:] - equity).max())}
    out["by_cost"] = net_by
    b = net_by.get(base) or next(iter(net_by.values()))
    # Flat fields for the base scenario (used by the UI and by older readers of the results).
    out.update({"mean_cost_bps": b["mean_cost_bps"], "mean_net_bps": b["mean_net_bps"], "ci95_net_bps": b["ci95_net_bps"],
                "hit_rate": b["hit_rate"], "worst_trade_bps": b["worst_trade_bps"],
                "max_drawdown_bps": b["max_drawdown_bps"], "total_net_bps": b["total_net_bps"]})
    return out


def _independent(entry: np.ndarray, exit_: np.ndarray) -> np.ndarray:
    keep, last_exit = [], -1
    for k in range(len(entry)):
        if entry[k] > last_exit:
            keep.append(k)
            last_exit = exit_[k]
    return np.array(keep, dtype=int)


def _naive_utc(values) -> np.ndarray:
    return np.array([pd.Timestamp(v).tz_convert("UTC").tz_localize(None).to_datetime64() if pd.Timestamp(v).tzinfo
                     else pd.Timestamp(v).to_datetime64() for v in values], dtype="datetime64[ns]")


MIN_BASELINE_BARS = 100


def _causal_baseline(fwd_h: np.ndarray, entry: np.ndarray, hz: int) -> np.ndarray:
    """Mean h-bar forward return over every earlier start whose exit closed before the trade's entry.

    ``fwd_h[j]`` is the h-bar return starting at bar j. Only starts j <= entry - hz are complete before the
    entry bar opens, so the benchmark never uses returns that were not yet known. NaN with too little history.
    """
    finite = np.isfinite(fwd_h)
    csum = np.r_[0.0, np.cumsum(np.where(finite, fwd_h, 0.0))]
    cnt = np.r_[0, np.cumsum(finite)]
    last = np.clip(entry - hz + 1, 0, len(fwd_h))  # number of starts available (indices 0..entry-hz)
    n = cnt[last]
    return np.where(n >= MIN_BASELINE_BARS, csum[last] / np.maximum(n, 1), np.nan)


@dataclass
class _Trades:
    """Per-horizon trade candidates before the independence filter."""

    entry: np.ndarray  # bar index of entry
    event_time: np.ndarray  # datetime64 of the event
    direction: np.ndarray  # +1/-1 (continuation sign)
    fwd: np.ndarray  # unsigned forward log return in bps (long the instrument / long the spread)
    baseline: float  # unconditional mean of the same forward return over the whole sample, bps (context only)
    causal_baseline: np.ndarray  # per trade: mean of the same forward return known before entry, bps
    cost: dict[str, np.ndarray]  # per scenario, round-trip cost in bps
    spread_missing: int = 0


def _costs(scenarios: list[CostScenario], rng_bps: np.ndarray, spread_bps: np.ndarray | None,
           entry: np.ndarray, exit_: np.ndarray, legs_weight: np.ndarray) -> tuple[dict[str, np.ndarray], int]:
    """Round-trip cost per trade and scenario. Range and spread use the bar before entry/exit (known then)."""
    pre_in = entry - 1
    pre_out = np.maximum(exit_ - 1, entry - 1)
    rng_cost = np.nan_to_num(rng_bps[pre_in]) + np.nan_to_num(rng_bps[pre_out])
    out, missing = {}, 0
    for s in scenarios:
        if s.source == "historical_spread":
            if spread_bps is None:
                continue
            half = 0.5 * (spread_bps[pre_in] + spread_bps[pre_out]) / 2
            bad = ~np.isfinite(half)
            missing = int(bad.sum())
            # Missing spread on a bar: fall back to the scenario's fixed half spread, counted in the results.
            half = np.where(bad, s.half_spread_bps, half)
            per_side = s.commission_bps + s.slippage_bps + half
        else:
            per_side = np.full(len(entry), s.fixed_side_bps)
        out[s.name] = (2 * per_side + s.slippage_range_frac * rng_cost) * legs_weight
    return out, missing


def _single_trades(df: pd.DataFrame, res, horizons: list[int], scenarios: list[CostScenario],
                   spread_bps: np.ndarray | None) -> tuple[dict[int, _Trades], dict[str, int]]:
    ts = df["ts"].to_numpy(dtype="datetime64[ns]")
    o, h, lo, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    rng_bps = np.where(c > 0, (h - lo) / c * 1e4, np.nan)
    ev_t = _naive_utc([d.event_ts for d in res.detections])
    ev_idx = np.searchsorted(df["event_ts"].to_numpy(dtype="datetime64[ns]"), ev_t)
    entry = np.searchsorted(ts, _naive_utc([d.available_ts for d in res.detections]), side="left")
    prev_c = np.r_[np.nan, c[:-1]]
    sign = np.sign(np.log(c[ev_idx] / prev_c[ev_idx])) if len(ev_idx) else np.array([])
    out, dropped = {}, {}
    for hz in horizons:
        exit_ = entry + hz - 1
        ok = (exit_ < len(df)) & (entry >= 1) & np.isfinite(sign) & (sign != 0)
        dropped[str(hz)] = int((~ok).sum())
        idx = np.flatnonzero(ok)
        fwd = np.log(c[exit_[idx]] / o[entry[idx]]) * 1e4
        cost, miss = _costs(scenarios, rng_bps, spread_bps, entry[idx], exit_[idx], np.ones(len(idx)))
        fwd_h = np.log(c[hz - 1:] / o[: len(o) - hz + 1]) * 1e4
        out[hz] = _Trades(entry[idx], ev_t[idx], sign[idx], fwd, float(np.nanmean(fwd_h)),
                          _causal_baseline(fwd_h, entry[idx], hz), cost, miss)
    return out, dropped


def _pair_frame(df: pd.DataFrame, other: pd.DataFrame) -> pd.DataFrame:
    a = df.assign(ly=np.log(total_return_index(df)))
    b = other.assign(lx=np.log(total_return_index(other)))
    m = a[["ts", "available_ts", "event_ts", "open", "high", "low", "close", "ly"]].merge(
        b[["ts", "available_ts", "open", "high", "low", "close", "lx"]], on="ts", suffixes=("", "_x"))
    m["available_ts"] = m[["available_ts", "available_ts_x"]].max(axis=1)
    return m.sort_values("ts").reset_index(drop=True)


def _pair_trades(df: pd.DataFrame, other: pd.DataFrame, res, horizons: list[int], scenarios: list[CostScenario],
                 spread_y: np.ndarray | None) -> tuple[dict[int, _Trades], dict[str, int]]:
    """Spread trades: long 1 of Y and short beta of X (beta from the event's window), in log-price terms.

    The study measures how the RELATIONSHIP moves after the event; the spread level is never a profit.
    Both legs pay costs (weight 1 + |beta|). Prices: Y and X opens/closes on common bars.
    """
    m = _pair_frame(df, other)
    ts = m["ts"].to_numpy(dtype="datetime64[ns]")
    oy, cy, ox, cx = (m[k].to_numpy(dtype=float) for k in ("open", "close", "open_x", "close_x"))
    rng_bps = np.where(cy > 0, (m["high"] - m["low"]).to_numpy() / cy * 1e4, np.nan)
    rng_x = np.where(cx > 0, (m["high_x"] - m["low_x"]).to_numpy() / cx * 1e4, np.nan)
    ev_t = _naive_utc([d.event_ts for d in res.detections])
    entry = np.searchsorted(ts, _naive_utc([d.available_ts for d in res.detections]), side="left")
    beta = np.array([float(d.metrics.get("hedge_ratio", np.nan)) for d in res.detections])
    direction = np.array([float(np.sign(d.statistic)) for d in res.detections])  # sign of the divergence
    out, dropped = {}, {}
    for hz in horizons:
        exit_ = entry + hz - 1
        ok = (exit_ < len(m)) & (entry >= 1) & np.isfinite(beta) & (direction != 0)
        dropped[str(hz)] = int((~ok).sum())
        idx = np.flatnonzero(ok)
        e, x, b = entry[idx], exit_[idx], beta[idx]
        fwd = ((np.log(cy[x]) - np.log(oy[e])) - b * (np.log(cx[x]) - np.log(ox[e]))) * 1e4
        cost_y, miss = _costs(scenarios, rng_bps, spread_y, e, x, np.ones(len(idx)))
        cost_x, _ = _costs([s for s in scenarios if s.source != "historical_spread"], rng_x, None, e, x, np.abs(b))
        cost = {k: v + cost_x.get(k, cost_x.get("base_estimate", 0)) for k, v in cost_y.items()}
        # Drift of the same spread position known before entry: mean h-bar Y move minus beta x mean X move.
        fy = (np.log(cy[hz - 1:]) - np.log(oy[: len(oy) - hz + 1])) * 1e4
        fx = (np.log(cx[hz - 1:]) - np.log(ox[: len(ox) - hz + 1])) * 1e4
        causal = _causal_baseline(fy, e, hz) - b * _causal_baseline(fx, e, hz)
        bm = float(np.nanmedian(beta)) if np.isfinite(beta).any() else 1.0
        out[hz] = _Trades(e, ev_t[idx], direction[idx], fwd, float(np.nanmean(fy - bm * fx)), causal, cost, miss)
    return out, dropped


def _signed(tr: _Trades, rule: str) -> tuple[np.ndarray, np.ndarray]:
    """Gross and excess per trade for a rule. Excess removes the drift the same direction earned on average
    before the trade (causal benchmark); trades without enough history for the benchmark get NaN excess."""
    s = tr.direction * (1 if rule == "continuation" else -1)  # reversion/reversal trade against the event
    return s * tr.fwd, s * (tr.fwd - tr.causal_baseline)


def evaluate(df: pd.DataFrame, detector: str, params: dict[str, Any], horizons: list[int], asset_class: str,
             seed: int = 0, *, other: pd.DataFrame | None = None, cost_scenarios: list[CostScenario] | None = None,
             spread_bps: pd.Series | None = None, base_scenario: str = "base_estimate",
             split_ts: pd.Timestamp | str | None = None) -> dict[str, Any]:
    det = DETECTORS[detector](**params)
    pair = det.pairwise
    if pair and other is None:
        raise ValueError("pair_divergence needs the second dataset of the pair")
    df = df.sort_values("ts").reset_index(drop=True)
    scenarios = list(cost_scenarios or default_cost_scenarios(asset_class))
    if spread_bps is not None and not any(s.source == "historical_spread" for s in scenarios):
        b = next((s for s in scenarios if s.name == base_scenario), scenarios[-1])
        scenarios.append(CostScenario("historical_spread", b.commission_bps, b.half_spread_bps, b.slippage_bps,
                                      b.slippage_range_frac, source="historical_spread"))
    res = det.run(df, other) if pair else det.run(df)
    rules = PAIR_RULES if pair else RULES
    base: dict[str, Any] = {
        "version": VERSION, "detector": detector, "detector_version": det.version, "params": det.params,
        "horizons": horizons, "asset_class": asset_class, "seed": seed,
        "entry_rule": "Open of the first bar starting at or after available_ts",
        "exit_rule": "Close of the h-th bar held", "rules": list(rules),
        "position": "spread: long Y, short beta x X (beta at the event)" if pair else "single instrument",
        "cost_scenarios": [asdict(s) for s in scenarios], "base_cost_scenario": base_scenario,
        "thresholds": {"min_independent_events": MIN_INDEPENDENT_EVENTS, "min_oos_events": MIN_OOS_EVENTS,
                       "min_selection_events": MIN_SELECTION_EVENTS, "alpha": ALPHA,
                       "in_sample_fraction": IN_SAMPLE_FRACTION, "walk_forward_folds": WALK_FORWARD_FOLDS,
                       "bootstrap_runs": BOOTSTRAP_RUNS}}
    if not res.applicable:
        return {**base, "verdict": "insufficient_sample", "verdict_text": res.reason, "n_events": 0, "table": [],
                "not_applicable": res.reason}

    if pair:
        m = _pair_frame(df, other)
        sp = None
        if spread_bps is not None:
            sp = spread_bps.reindex(pd.DatetimeIndex(m["ts"])).to_numpy(dtype=float)
        trades, dropped = _pair_trades(df, other, res, horizons, scenarios, sp)
    else:
        sp = None
        if spread_bps is not None:
            sp = spread_bps.reindex(pd.DatetimeIndex(df["ts"])).to_numpy(dtype=float)
        trades, dropped = _single_trades(df, res, horizons, scenarios, sp)

    all_events = _naive_utc([d.event_ts for d in res.detections])
    if split_ts is not None:  # pre-registered split (fixed before seeing the later data)
        split = _naive_utc([split_ts])[0]
    else:
        split = np.sort(all_events)[int(len(all_events) * IN_SAMPLE_FRACTION)] if len(all_events) else None
    # Exit time of a trade, to purge in-sample trades that are still open at the split.
    ts_col = (_pair_frame(df, other) if pair else df)["event_ts"]
    bar_close = _naive_utc(ts_col)

    rows, combos = [], {}
    for hz in horizons:
        tr = trades[hz]
        has_base = np.isfinite(tr.causal_baseline)
        no_baseline = int((~has_base).sum())
        tr = _Trades(tr.entry[has_base], tr.event_time[has_base], tr.direction[has_base], tr.fwd[has_base],
                     tr.baseline, tr.causal_baseline[has_base], {k: v[has_base] for k, v in tr.cost.items()},
                     tr.spread_missing)
        keep = _independent(tr.entry, tr.entry + hz - 1)
        exit_t = bar_close[tr.entry[keep] + hz - 1] if len(keep) else np.array([], dtype="datetime64[ns]")
        ev = tr.event_time[keep]
        is_mask = exit_t < split if split is not None else np.zeros(len(keep), bool)
        oos_mask = ev >= split if split is not None else np.zeros(len(keep), bool)
        for rule in rules:
            gross, excess = _signed(tr, rule)
            g, x = gross[keep], excess[keep]
            cst = {k: v[keep] for k, v in tr.cost.items()}
            key = (seed, hz, rule)
            row = {"horizon": hz, "rule": rule,
                   "n_events_total": int(len(tr.entry) + dropped[str(hz)] + no_baseline),
                   "n_without_benchmark_history": no_baseline,
                   "n_independent": int(len(keep)), "n_in_sample": int(is_mask.sum()),
                   "n_out_of_sample": int(oos_mask.sum()), "n_purged": int(len(keep) - is_mask.sum() - oos_mask.sum()),
                   "baseline_mean_bps": tr.baseline, "spread_missing_bars": tr.spread_missing,
                   "all": _trade_stats(g, x, cst, base_scenario, key),
                   "in_sample": _trade_stats(g[is_mask], x[is_mask], {k: v[is_mask] for k, v in cst.items()},
                                             base_scenario, (*key, "is"))}
            rows.append(row)
            combos[(hz, rule)] = {"g": g, "x": x, "cost": cst, "is": is_mask, "oos": oos_mask, "ev": ev}

    pvals = np.array([r["in_sample"].get("pvalue") if r["in_sample"].get("pvalue") is not None else 1.0 for r in rows])
    adj = benjamini_hochberg(pvals) if len(pvals) else pvals
    for r, a in zip(rows, adj, strict=True):
        r["in_sample"]["adjusted_pvalue"] = float(a)

    def pick(cands):
        cands = [c for c in cands if c[1] is not None]
        return max(cands, key=lambda c: c[1]) if cands else None

    sel = pick([(r, r["in_sample"].get("excess_t_stat")) for r in rows
                if r["in_sample"]["n"] >= MIN_SELECTION_EVENTS])
    best = sel[0] if sel else None
    oos: dict[str, Any] = {"n": 0}
    survives: dict[str, Any] = {}
    if best is not None:
        d = combos[(best["horizon"], best["rule"])]
        mk = d["oos"]
        oos = _trade_stats(d["g"][mk], d["x"][mk], {k: v[mk] for k, v in d["cost"].items()}, base_scenario,
                           (seed, best["horizon"], best["rule"], "oos"))
        if oos.get("excess_t_stat") is not None:
            oos["pvalue_one_sided"] = float(stats.t.sf(oos["excess_t_stat"], oos["n"] - 1))
        for name, v in (oos.get("by_cost") or {}).items():
            ci = v.get("ci95_net_bps")
            survives[name] = bool(v["mean_net_bps"] is not None and v["mean_net_bps"] > 0 and ci and ci[0] > 0)

    # True walk-forward: select on all earlier folds, evaluate on the next one.
    wf: list[dict[str, Any]] = []
    n_all = max((len(c["g"]) for c in combos.values()), default=0)
    if all_events.size and n_all >= WALK_FORWARD_FOLDS * MIN_SELECTION_EVENTS // 2:
        edges = np.quantile(all_events.astype("int64"), np.linspace(0, 1, WALK_FORWARD_FOLDS + 1)).astype("int64")
        edges = edges.astype("datetime64[ns]")
        for k in range(1, WALK_FORWARD_FOLDS):
            lo_t, hi_t = edges[k], edges[k + 1]
            cands = []
            for (hz, rule), c in combos.items():
                train = c["ev"] < lo_t
                if train.sum() >= MIN_SELECTION_EVENTS:
                    t = _stats(c["x"][train], _rng(seed, "wf", k, hz, rule))["t_stat"]
                    cands.append(((hz, rule), t))
            chosen = pick(cands)
            if chosen is None:
                wf.append({"fold": k + 1, "start": str(lo_t), "end": str(hi_t), "selected": None, "n": 0})
                continue
            c = combos[chosen[0]]
            test = (c["ev"] >= lo_t) & ((c["ev"] <= hi_t) if k == WALK_FORWARD_FOLDS - 1 else (c["ev"] < hi_t))
            base_cost = c["cost"].get(base_scenario, next(iter(c["cost"].values())))
            wf.append({"fold": k + 1, "start": str(lo_t), "end": str(hi_t),
                       "selected": {"horizon": chosen[0][0], "rule": chosen[0][1]}, "n": int(test.sum()),
                       "mean_excess_bps": float(c["x"][test].mean()) if test.any() else None,
                       "mean_net_bps": float((c["g"][test] - base_cost[test]).mean()) if test.any() else None})

    n_indep = best["n_independent"] if best else max((r["n_independent"] for r in rows), default=0)
    if best is None or n_indep < MIN_INDEPENDENT_EVENTS or oos.get("n", 0) < MIN_OOS_EVENTS:
        verdict = "insufficient_sample"
    elif (oos.get("mean_excess_bps", 0) > 0 and oos.get("ci95_excess_bps") and oos["ci95_excess_bps"][0] > 0
          and best["in_sample"]["adjusted_pvalue"] < ALPHA):
        verdict = "oos_supported"
    elif best["in_sample"]["adjusted_pvalue"] < ALPHA and best["in_sample"]["mean_excess_bps"] > 0:
        verdict = "in_sample_only"
    else:
        verdict = "no_evidence"
    return {**base, "n_events": len(res.detections), "events_dropped_by_horizon": dropped,
            "split_ts": None if split is None else str(split),
            "split_rule": "pre-registered" if split_ts is not None else f"{IN_SAMPLE_FRACTION:.0%} of detected events",
            "baseline_mean_bps": {str(hz): trades[hz].baseline for hz in horizons},
            "cost_model": {"scenarios": [s.name for s in scenarios], "base": base_scenario,
                           "note": "Estimated costs unless a scenario says historical_spread; not broker quotes. "
                                   "Holding costs (swap/financing) are not included."},
            "table": rows, "n_combinations_tested": len(rows),
            "selected": None if best is None else {"horizon": best["horizon"], "rule": best["rule"],
                                                   "in_sample": best["in_sample"]},
            "out_of_sample": oos, "survives_costs_oos": survives, "walk_forward": wf,
            "verdict": verdict, "verdict_text": VERDICT_TEXT[verdict],
            "multiple_testing": f"Benjamini-Hochberg over the {len(rows)} horizon x rule combinations in-sample; "
                                "the selected rule is then tested once out of sample."}


def run_event_study(session: Session, dataset_id: str, detector: str, *, store: ParquetStore,
                    params: dict[str, Any] | None = None, horizons: list[int] | None = None, seed: int = 0,
                    related_dataset_id: str | None = None, cost_scenarios: list[CostScenario] | None = None,
                    spread_bps: pd.Series | None = None, spread_source: str | None = None,
                    actor: str = "api") -> Experiment:
    ds = session.get(Dataset, dataset_id)
    if ds is None:
        raise ValueError(f"Unknown dataset {dataset_id}")
    if detector not in DETECTORS:
        raise ValueError(f"Unknown detector {detector!r}")
    rel = session.get(Dataset, related_dataset_id) if related_dataset_id else None
    if related_dataset_id and rel is None:
        raise ValueError(f"Unknown dataset {related_dataset_id}")
    if DETECTORS[detector].pairwise and rel is None:
        raise ValueError("pair_divergence needs related_dataset_id")
    if rel is not None and bool(rel.is_synthetic) != bool(ds.is_synthetic):
        raise ValueError("Refusing to pair synthetic and real data in one study")
    if params is None:  # the parameters of the most recent scan that ran this detector on the dataset
        from qal.models import AnomalyDetection

        exp_id = session.scalar(select(AnomalyDetection.experiment_id).where(
            AnomalyDetection.dataset_id == dataset_id, AnomalyDetection.detector == detector)
            .order_by(AnomalyDetection.created_at.desc()).limit(1))
        params = (session.get(Experiment, exp_id).config.get("detectors", {}).get(detector, {})
                  if exp_id else {})
    horizons = sorted({int(h) for h in (horizons or DEFAULT_HORIZONS) if int(h) >= 1})
    scenarios = cost_scenarios or default_cost_scenarios(ds.instrument.asset_class)
    config = {"dataset_id": dataset_id, "related_dataset_id": related_dataset_id, "detector": detector,
              "params": DETECTORS[detector](**params).params, "horizons": horizons, "seed": seed,
              "cost_scenarios": [asdict(s) for s in scenarios],
              "spread": None if spread_bps is None else {"source": spread_source,
                                                         "hash": stable_hash([[str(t), v] for t, v in
                                                                             spread_bps.round(6).items()])}}
    algo = f"event_study@{VERSION}+{detector}@{DETECTORS[detector].version}"
    data_hashes = [ds.content_hash] + ([rel.content_hash] if rel else [])
    config_hash = stable_hash({"config": config, "data": data_hashes, "algo": algo})
    cached = session.scalar(select(Experiment).where(Experiment.config_hash == config_hash,
                                                     Experiment.status == "succeeded"))
    if cached is not None:
        return cached
    prior = sum(1 for c in session.scalars(select(Experiment.config).where(Experiment.kind == "event_study"))
                if c.get("dataset_id") == dataset_id)
    refs = [ds] + ([rel] if rel else [])
    exp = Experiment(kind="event_study", status="running", algorithm_version=algo, code_version=code_version(),
                     config=config, config_hash=config_hash, seed=seed,
                     data_refs=[{"dataset_id": d.id, "content_hash": d.content_hash, "rows": d.rows,
                                 "is_synthetic": d.is_synthetic, "quality_status": d.quality_status} for d in refs])
    session.add(exp)
    session.flush()
    t0 = time.perf_counter()
    try:
        results = evaluate(store.read(ds.path), detector, params, horizons, ds.instrument.asset_class, seed,
                           other=store.read(rel.path) if rel else None, cost_scenarios=scenarios,
                           spread_bps=spread_bps)
        results["prior_event_studies_on_dataset"] = int(prior)
        results["is_synthetic"] = ds.is_synthetic
        results["spread_source"] = spread_source
        exp.results = results
        exp.n_tests = int(results.get("n_combinations_tested", 0))
        exp.status = "succeeded"
    except Exception as exc:  # recorded, not hidden
        exp.status, exp.error = "failed", f"{type(exc).__name__}: {exc}"
        exp.results = {}
    exp.results = {**exp.results, "duration_s": round(time.perf_counter() - t0, 3)}
    exp.finished_at = datetime.now(UTC)
    audit.record(session, actor, "event_study_finished", "experiment", exp.id, status=exp.status,
                 verdict=exp.results.get("verdict"))
    return exp


def latest_verdicts(session: Session, dataset_ids: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """Most recent succeeded event study per (dataset, detector)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if not dataset_ids:
        return out
    rows = session.scalars(select(Experiment).where(Experiment.kind == "event_study",
                                                    Experiment.status == "succeeded")
                           .order_by(Experiment.created_at.desc())).all()
    wanted = set(dataset_ids)
    for e in rows:
        key = (e.config.get("dataset_id"), e.config.get("detector"))
        if key[0] in wanted and key not in out:
            out[key] = {"experiment_id": e.id, "verdict": e.results.get("verdict"),
                        "verdict_text": e.results.get("verdict_text"), "created_at": e.created_at.isoformat()}
    return out
