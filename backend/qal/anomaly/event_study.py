"""Event study: do detected events of one type anticipate returns on this dataset, after costs, out of sample?

Design (fixed before looking at results):
* Events come from re-running the detector on the stored snapshot. Detectors are causal
  (``tests/test_no_lookahead.py``), so every event uses only bars up to its own close.
* Entry is at the OPEN of the first bar whose start ``ts`` is at or after the event's ``available_ts``:
  the event could not have been acted on earlier. Exit is at the close of the h-th bar held.
  Events whose holding period runs past the end of the data are dropped, never truncated.
* Two rules per horizon: continuation (trade in the direction of the event bar's return) and reversal.
* Costs from the asset-class cost model: commission, half spread and fixed slippage per side, plus
  slippage proportional to the previous bar's high-low range at entry and exit.
* Only independent events are used: an event is skipped while the previous kept trade is still open.
* In-sample = events before the 70 % time split. All horizon x rule combinations are tested in-sample
  (Benjamini-Hochberg over them), the best one by in-sample mean net return is chosen, and only that one is
  evaluated on the remaining 30 % (out of sample). Walk-forward folds show stability across time.
* Verdicts are conservative and never mean "validated strategy": that needs a registered hypothesis and
  Gates A-D.
"""

from __future__ import annotations

import time
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
from qal.data.store import ParquetStore
from qal.models import Dataset, Experiment
from qal.research.costs import default_cost_model
from qal.versioning import code_version, stable_hash

VERSION = "1.0.0"
DEFAULT_HORIZONS = {"1m": [5, 15, 60], "5m": [3, 12, 48], "15m": [2, 8, 32], "30m": [2, 8, 16],
                    "1h": [1, 4, 12, 24], "4h": [1, 3, 6, 12], "1d": [1, 5, 10, 20], "1wk": [1, 4, 8, 12]}
IN_SAMPLE_FRACTION = 0.7
MIN_INDEPENDENT_EVENTS = 30
MIN_OOS_EVENTS = 10
WALK_FORWARD_FOLDS = 5
BOOTSTRAP_RUNS = 2000
ALPHA = 0.05
RULES = ("continuation", "reversal")

VERDICT_TEXT = {
    "insufficient_sample": "Too few independent events to say anything; any return shown is anecdotal.",
    "no_evidence": "No horizon or direction shows a net edge that survives multiple-testing control in-sample.",
    "in_sample_only": "An edge appears in-sample after multiple-testing control but is not confirmed out of "
                      "sample. Treat it as a hypothesis, not evidence.",
    "oos_supported": "The rule chosen in-sample stayed profitable after costs out of sample, with a confidence "
                     "interval above zero. This is a reason to register and validate a hypothesis, not a "
                     "validated strategy.",
}


def _stats(net: np.ndarray, gross: np.ndarray, cost: np.ndarray, rng: np.random.Generator) -> dict[str, Any]:
    n = len(net)
    out: dict[str, Any] = {"n": n}
    if n == 0:
        return out
    mean = float(net.mean())
    sd = float(net.std(ddof=1)) if n > 1 else float("nan")
    out.update({"mean_gross_bps": float(gross.mean()), "mean_cost_bps": float(cost.mean()), "mean_net_bps": mean,
                "std_net_bps": sd, "hit_rate": float((net > 0).mean()), "worst_trade_bps": float(net.min())})
    equity = np.cumsum(net)
    out["max_drawdown_bps"] = float((np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:] - equity).max())
    out["total_net_bps"] = float(equity[-1])
    if n >= 3 and sd > 0:
        t = mean / (sd / np.sqrt(n))
        out["t_stat"] = float(t)
        out["pvalue"] = float(2 * stats.t.sf(abs(t), n - 1))
        boots = rng.choice(net, size=(BOOTSTRAP_RUNS, n), replace=True).mean(axis=1)
        out["ci95_net_bps"] = [float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))]
    else:
        out["pvalue"] = None
        out["ci95_net_bps"] = None
    return out


def _independent(entry: np.ndarray, exit_: np.ndarray) -> np.ndarray:
    keep, last_exit = [], -1
    for k in range(len(entry)):
        if entry[k] > last_exit:
            keep.append(k)
            last_exit = exit_[k]
    return np.array(keep, dtype=int)


def evaluate(df: pd.DataFrame, detector: str, params: dict[str, Any], horizons: list[int], asset_class: str,
             seed: int = 0) -> dict[str, Any]:
    det = DETECTORS[detector](**params)
    if det.pairwise:
        raise ValueError("Event studies of pair detectors are not supported yet")
    df = df.sort_values("ts").reset_index(drop=True)
    res = det.run(df)
    base: dict[str, Any] = {"detector": detector, "detector_version": det.version, "params": det.params,
                            "horizons": horizons, "asset_class": asset_class,
                            "entry_rule": "Open of the first bar starting at or after available_ts",
                            "exit_rule": "Close of the h-th bar held", "rules": list(RULES),
                            "thresholds": {"min_independent_events": MIN_INDEPENDENT_EVENTS,
                                           "min_oos_events": MIN_OOS_EVENTS, "alpha": ALPHA,
                                           "in_sample_fraction": IN_SAMPLE_FRACTION}}
    if not res.applicable:
        return {**base, "verdict": "insufficient_sample", "verdict_text": res.reason, "n_events": 0, "table": []}

    ts = df["ts"].to_numpy(dtype="datetime64[ns]")
    o, h, lo, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    rng_bps = np.where(c > 0, (h - lo) / c * 1e4, np.nan)
    ev_idx = np.searchsorted(df["event_ts"].to_numpy(dtype="datetime64[ns]"),
                             np.array([d.event_ts.to_datetime64() for d in res.detections], dtype="datetime64[ns]"))
    entry = np.searchsorted(ts, np.array([d.available_ts.tz_convert("UTC").tz_localize(None).to_datetime64()
                                          for d in res.detections], dtype="datetime64[ns]"), side="left")
    prev_c = np.r_[np.nan, c[:-1]]
    sign = np.sign(np.log(c[ev_idx] / prev_c[ev_idx])) if len(ev_idx) else np.array([])
    event_times = np.array([d.event_ts for d in res.detections])

    cm = default_cost_model(asset_class)
    fixed_side = cm.commission_bps + cm.half_spread_bps + cm.slippage_bps
    split_ts = event_times[int(len(event_times) * IN_SAMPLE_FRACTION)] if len(event_times) else None

    rng = np.random.default_rng(seed)
    rows, dropped = [], {}
    per_combo: dict[tuple[int, str], dict[str, np.ndarray]] = {}
    for hz in horizons:
        exit_ = entry + hz - 1
        ok = (exit_ < len(df)) & (entry >= 1) & np.isfinite(sign) & (sign != 0)
        dropped[str(hz)] = int((~ok).sum())
        idx = np.flatnonzero(ok)
        keep = idx[_independent(entry[idx], exit_[idx])]
        fwd = np.log(c[exit_[keep]] / o[entry[keep]]) * 1e4
        slip = cm.slippage_range_frac * (np.nan_to_num(rng_bps[entry[keep] - 1]) + np.nan_to_num(
            rng_bps[np.maximum(exit_[keep] - 1, entry[keep] - 1)]))
        cost = 2 * fixed_side + slip
        is_mask = np.array([t < split_ts for t in event_times[keep]]) if len(keep) else np.array([], dtype=bool)
        for rule in RULES:
            gross = sign[keep] * fwd * (1 if rule == "continuation" else -1)
            net = gross - cost
            per_combo[(hz, rule)] = {"net": net, "gross": gross, "cost": cost, "is": is_mask,
                                     "times": event_times[keep]}
            rows.append({"horizon": hz, "rule": rule, "all": _stats(net, gross, cost, rng),
                         "in_sample": _stats(net[is_mask], gross[is_mask], cost[is_mask], rng)})

    pvals = np.array([r["in_sample"].get("pvalue") if r["in_sample"].get("pvalue") is not None else 1.0 for r in rows])
    adj = benjamini_hochberg(pvals) if len(pvals) else pvals
    for r, a in zip(rows, adj, strict=True):
        r["in_sample"]["adjusted_pvalue"] = float(a)

    # Unconditional drift for context: mean h-bar open-to-close log return over every bar.
    baseline = {str(hz): float(np.nanmean(np.log(c[hz - 1:] / o[: len(o) - hz + 1])) * 1e4) for hz in horizons}

    candidates = [r for r in rows if r["in_sample"]["n"] >= 5 and "mean_net_bps" in r["in_sample"]]
    best = max(candidates, key=lambda r: r["in_sample"]["mean_net_bps"]) if candidates else None
    oos: dict[str, Any] = {"n": 0}
    wf: list[dict[str, Any]] = []
    if best is not None:
        d = per_combo[(best["horizon"], best["rule"])]
        m = ~d["is"]
        oos = _stats(d["net"][m], d["gross"][m], d["cost"][m], rng)
        if oos.get("t_stat") is not None:
            oos["pvalue_one_sided"] = float(stats.t.sf(oos["t_stat"], oos["n"] - 1))
        for k, chunk in enumerate(np.array_split(np.arange(len(d["net"])), WALK_FORWARD_FOLDS)):
            if len(chunk):
                wf.append({"fold": k + 1, "n": int(len(chunk)), "start": str(d["times"][chunk[0]]),
                           "end": str(d["times"][chunk[-1]]), "mean_net_bps": float(d["net"][chunk].mean())})

    n_indep = best["all"]["n"] if best else max((r["all"]["n"] for r in rows), default=0)
    if best is None or n_indep < MIN_INDEPENDENT_EVENTS or oos.get("n", 0) < MIN_OOS_EVENTS:
        verdict = "insufficient_sample"
    elif (oos.get("mean_net_bps", 0) > 0 and oos.get("ci95_net_bps") and oos["ci95_net_bps"][0] > 0
          and best["in_sample"]["adjusted_pvalue"] < ALPHA):
        verdict = "oos_supported"
    elif best["in_sample"]["adjusted_pvalue"] < ALPHA and best["in_sample"]["mean_net_bps"] > 0:
        verdict = "in_sample_only"
    else:
        verdict = "no_evidence"
    return {**base, "n_events": len(res.detections), "events_dropped_by_horizon": dropped,
            "split_ts": None if split_ts is None else str(split_ts), "baseline_mean_bps": baseline,
            "cost_model": {"fixed_per_side_bps": fixed_side, "slippage_range_frac": cm.slippage_range_frac,
                           "note": "Placeholder costs per asset class; holding costs (financing, borrow) are not "
                                   "included at these horizons."},
            "table": rows, "n_combinations_tested": len(rows),
            "selected": None if best is None else {"horizon": best["horizon"], "rule": best["rule"],
                                                   "in_sample": best["in_sample"]},
            "out_of_sample": oos, "walk_forward": wf, "verdict": verdict, "verdict_text": VERDICT_TEXT[verdict],
            "multiple_testing": f"Benjamini-Hochberg over the {len(rows)} horizon x rule combinations in-sample; "
                                "the selected rule is then tested once out of sample."}


def run_event_study(session: Session, dataset_id: str, detector: str, *, store: ParquetStore,
                    params: dict[str, Any] | None = None, horizons: list[int] | None = None, seed: int = 0,
                    actor: str = "api") -> Experiment:
    ds = session.get(Dataset, dataset_id)
    if ds is None:
        raise ValueError(f"Unknown dataset {dataset_id}")
    if detector not in DETECTORS:
        raise ValueError(f"Unknown detector {detector!r}")
    if params is None:  # the parameters of the most recent scan that ran this detector on the dataset
        from qal.models import AnomalyDetection

        exp_id = session.scalar(select(AnomalyDetection.experiment_id).where(
            AnomalyDetection.dataset_id == dataset_id, AnomalyDetection.detector == detector)
            .order_by(AnomalyDetection.created_at.desc()).limit(1))
        params = (session.get(Experiment, exp_id).config.get("detectors", {}).get(detector, {})
                  if exp_id else {})
    horizons = sorted({int(h) for h in (horizons or DEFAULT_HORIZONS.get(ds.frequency, [1, 5, 10])) if int(h) >= 1})
    config = {"dataset_id": dataset_id, "detector": detector, "params": DETECTORS[detector](**params).params,
              "horizons": horizons, "seed": seed}
    algo = f"event_study@{VERSION}+{detector}@{DETECTORS[detector].version}"
    config_hash = stable_hash({"config": config, "data": ds.content_hash, "algo": algo})
    cached = session.scalar(select(Experiment).where(Experiment.config_hash == config_hash,
                                                     Experiment.status == "succeeded"))
    if cached is not None:
        return cached
    prior = sum(1 for c in session.scalars(select(Experiment.config).where(Experiment.kind == "event_study"))
                if c.get("dataset_id") == dataset_id)
    exp = Experiment(kind="event_study", status="running", algorithm_version=algo, code_version=code_version(),
                     config=config, config_hash=config_hash, seed=seed,
                     data_refs=[{"dataset_id": ds.id, "content_hash": ds.content_hash, "rows": ds.rows,
                                 "is_synthetic": ds.is_synthetic, "quality_status": ds.quality_status}])
    session.add(exp)
    session.flush()
    t0 = time.perf_counter()
    try:
        results = evaluate(store.read(ds.path), detector, params, horizons, ds.instrument.asset_class, seed)
        results["prior_event_studies_on_dataset"] = int(prior)
        results["is_synthetic"] = ds.is_synthetic
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
