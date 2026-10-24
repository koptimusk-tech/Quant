"""Model Lab: compare anomaly detectors on the same datasets, keeping rarity and predictive value apart.

For every dataset x model the comparison records, from one stored experiment (kind ``model_comparison``):

* what the model flags: number of flagged bars, episodes (flags at most ``EPISODE_GAP`` bars apart count as
  one event), flagged share vs the nominal rate, Benjamini-Hochberg significant bars within the run;
* when: flag rate per calendar year (and its dispersion across years) and per causal volatility regime
  (``rel_vol`` > 0, i.e. 20-bar volatility above the 250-bar one, known at the bar's close);
* how stable it is: overlap of flagged bars (+-1 bar) across seeds (stochastic models only) and across
  shorter / longer training windows;
* how much it agrees with the other models on the same dataset (overlap matrix);
* whether its events anticipate returns: the event study of ``qal.anomaly.event_study`` (chronological
  split, walk-forward, causal benchmark, estimated costs), with a Benjamini-Hochberg correction across all
  out-of-sample tests of the run. Future returns are used here only, as the evaluation target;
* runtime of the main fit (seeds and variants are extra runs of the same cost).

These numbers are reported side by side. There is no aggregate score: a model that flags rare bars is not
better for it, and one with an out-of-sample effect has still to pass a registered hypothesis and Gates A-D.

The synthetic known-anomaly benchmark (``synthetic_benchmark``) runs on generated series with injected events
and a control series. It measures recall and false alarms; it says nothing about real markets.

Resource limits: at most ``MAX_DATASETS`` datasets, ``MAX_MODELS`` models and ``MAX_ROWS`` bars per dataset
(the most recent ones), a wall-clock budget checked between units of work, and cooperative cancellation
through ``should_stop``. A unit already running finishes before the budget or a cancellation takes effect.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal import audit
from qal.anomaly import event_study
from qal.anomaly.base import DetectorOutput
from qal.anomaly.detectors import DETECTORS
from qal.anomaly.engine import benjamini_hochberg
from qal.data.adapters import FetchRequest
from qal.data.adapters.synthetic import SyntheticAdapter, injected_events
from qal.data.schema import InstrumentSpec, normalize_bars
from qal.data.store import ParquetStore
from qal.features.catalog import build
from qal.models import Dataset, Experiment
from qal.versioning import code_version, stable_hash

VERSION = "1.0.0"
MAX_DATASETS = 20
MAX_MODELS = 8
MAX_ROWS = 200_000
EPISODE_GAP = 5
MATCH_TOL = 1
BH_ALPHA = 0.05
MIN_YEAR_BARS = 100
CHART_POINTS = 1500
DEFAULT_MODELS = ({"detector": "isolation_forest"}, {"detector": "local_outlier_factor"},
                  {"detector": "return_shock"}, {"detector": "volatility_regime"})
WINDOW_PARAMS = ("train_window", "window", "long_window")


class Cancelled(Exception):
    pass


@dataclass
class ModelSpec:
    detector: str
    params: dict[str, Any] = field(default_factory=dict)
    label: str = ""

    @property
    def key(self) -> str:
        return self.label or self.detector


@dataclass
class ComparisonConfig:
    dataset_ids: list[str]
    models: list[ModelSpec] = field(default_factory=lambda: [ModelSpec(**m) for m in DEFAULT_MODELS])
    seeds: list[int] = field(default_factory=lambda: [0, 1, 2])
    window_variants: list[float] = field(default_factory=lambda: [2 / 3, 4 / 3])
    predictive: bool = True
    horizons: list[int] = field(default_factory=lambda: [1, 5, 20])
    max_seconds: float = 1800.0
    benchmark: bool = False  # also run the synthetic known-anomaly benchmark
    force: bool = False
    label: str = ""

    def validate(self) -> None:
        if not self.dataset_ids and not self.benchmark:
            raise ValueError("select at least one dataset (or run the synthetic benchmark)")
        if len(self.dataset_ids) > MAX_DATASETS:
            raise ValueError(f"at most {MAX_DATASETS} datasets per comparison")
        if not self.models or len(self.models) > MAX_MODELS:
            raise ValueError(f"select between 1 and {MAX_MODELS} models")
        keys = [m.key for m in self.models]
        if len(set(keys)) != len(keys):
            raise ValueError("two models have the same label; give each configuration its own label")
        for m in self.models:
            if m.detector not in DETECTORS:
                raise ValueError(f"unknown detector {m.detector!r}; available: {list(DETECTORS)}")
            if DETECTORS[m.detector].pairwise:
                raise ValueError(f"{m.detector} needs a pair of datasets; compare it in the scanner instead")
            DETECTORS[m.detector](**m.params)  # parameter errors surface before anything runs
        if not 1 <= len(self.seeds) <= 5:
            raise ValueError("between 1 and 5 seeds")
        if any(not 0.25 <= v <= 4 for v in self.window_variants) or len(self.window_variants) > 4:
            raise ValueError("window variants are up to 4 factors in [0.25, 4]")
        if not self.horizons or any(int(h) < 1 for h in self.horizons):
            raise ValueError("horizons must be positive bar counts")
        if not 10 <= self.max_seconds <= 6 * 3600:
            raise ValueError("max_seconds must be between 10 and 21600")


# ------------------------------------------------------------------ measurements


def episodes(idx: np.ndarray, gap: int = EPISODE_GAP) -> int:
    idx = np.sort(np.asarray(idx))
    return 0 if not len(idx) else int(1 + (np.diff(idx) > gap).sum())


def _matched(a: np.ndarray, b: np.ndarray, tol: int) -> int:
    if not len(a) or not len(b):
        return 0
    b = np.sort(b)
    k = np.searchsorted(b, a)
    lo = np.abs(a - b[np.clip(k - 1, 0, len(b) - 1)])
    hi = np.abs(a - b[np.clip(k, 0, len(b) - 1)])
    return int((np.minimum(lo, hi) <= tol).sum())


def overlap(a: np.ndarray, b: np.ndarray, tol: int = MATCH_TOL) -> dict[str, float | None]:
    """Jaccard-type overlap of two sets of flagged bar positions, a match being within ``tol`` bars."""
    a, b = np.asarray(a), np.asarray(b)
    if not len(a) and not len(b):
        return {"jaccard": None, "share_a_in_b": None, "share_b_in_a": None}
    ma, mb = _matched(a, b, tol), _matched(b, a, tol)
    inter = (ma + mb) / 2
    return {"jaccard": round(inter / (len(a) + len(b) - inter), 4),
            "share_a_in_b": round(ma / len(a), 4) if len(a) else None,
            "share_b_in_a": round(mb / len(b), 4) if len(b) else None}


def _positions(df: pd.DataFrame, out: DetectorOutput) -> np.ndarray:
    pos = pd.Index(df["event_ts"])
    return np.sort(pos.get_indexer([d.event_ts for d in out.detections]))


def _scored_mask(df: pd.DataFrame, out: DetectorOutput) -> np.ndarray:
    """Bars with a finite p-value. Detectors return p-values either per bar or per evaluated bar."""
    p = np.asarray(out.pvalues, dtype=float)
    if len(p) == len(df):
        return np.isfinite(p)
    mask = np.zeros(len(df), bool)
    if out.series is not None and len(out.series) == len(p):
        idx = pd.Index(df["event_ts"]).get_indexer(out.series.index)
        ok = (idx >= 0) & np.isfinite(p)
        mask[idx[ok]] = True
    return mask


def _variants(spec: ModelSpec, factors: list[float]) -> list[tuple[str, dict[str, Any]]]:
    base = DETECTORS[spec.detector](**spec.params).params
    out = []
    for name in WINDOW_PARAMS:
        if name in base:
            for f in factors:
                v = max(2, int(round(base[name] * f)))
                params = {**spec.params, name: v}
                if name == "train_window" and v < base.get("min_train", 0):
                    params["min_train"] = max(50, v // 2)
                try:
                    DETECTORS[spec.detector](**params)
                except ValueError:
                    continue
                out.append((f"{name}={v}", params))
            break
    return out


def _run(det, df: pd.DataFrame) -> tuple[DetectorOutput, float]:
    t0 = time.perf_counter()
    out = det.run(df)
    return out, round(time.perf_counter() - t0, 3)


def _rates(df: pd.DataFrame, scored: np.ndarray, flagged: np.ndarray) -> dict[str, Any]:
    years = pd.DatetimeIndex(df["event_ts"]).year.to_numpy()
    flag = np.zeros(len(df), bool)
    flag[flagged] = True
    per_year = []
    for y in np.unique(years[scored]):
        m = scored & (years == y)
        per_year.append({"year": int(y), "bars": int(m.sum()), "flags": int(flag[m].sum()),
                         "rate": round(float(flag[m].mean()), 5)})
    full = [r["rate"] for r in per_year if r["bars"] >= MIN_YEAR_BARS]
    cv = float(np.std(full) / np.mean(full)) if len(full) >= 3 and np.mean(full) > 0 else None
    rel_vol = build(df, ["rel_vol"])["rel_vol"].to_numpy()
    regimes = {}
    for name, m in (("high_vol", rel_vol > 0), ("low_vol", rel_vol <= 0)):
        m = m & scored & np.isfinite(rel_vol)
        regimes[name] = {"bars": int(m.sum()), "flags": int(flag[m].sum()),
                         "rate": round(float(flag[m].mean()), 5) if m.any() else None}
    hi, lo = regimes["high_vol"]["rate"], regimes["low_vol"]["rate"]
    regimes["high_to_low_ratio"] = round(hi / lo, 3) if hi is not None and lo else None
    return {"per_year": per_year, "rate_cv_across_years": None if cv is None else round(cv, 3),
            "per_regime": regimes}


def _bh_significant(out: DetectorOutput) -> int:
    p = np.asarray(out.pvalues, float)
    p = p[np.isfinite(p)]
    return int((benjamini_hochberg(p) <= BH_ALPHA).sum()) if len(p) else 0


def _chart(df: pd.DataFrame) -> dict[str, Any]:
    step = max(1, len(df) // CHART_POINTS)
    d = df.iloc[::step]
    return {"ts": [str(t) for t in d["event_ts"]], "close": [round(float(c), 6) for c in d["close"]], "step": step}


def _predictive(df: pd.DataFrame, spec: ModelSpec, out: DetectorOutput, horizons: list[int],
                asset_class: str) -> dict[str, Any]:
    r = event_study.evaluate(df, spec.detector, spec.params, horizons, asset_class, precomputed=out)
    oos = r.get("out_of_sample") or {}
    return {"verdict": r.get("verdict"), "verdict_text": r.get("verdict_text"), "n_events": r.get("n_events"),
            "selected": r.get("selected"), "split_ts": r.get("split_ts"),
            "out_of_sample": {k: oos.get(k) for k in ("n", "mean_excess_bps", "ci95_excess_bps",
                                                       "pvalue_one_sided", "mean_net_bps")},
            "survives_costs_oos": r.get("survives_costs_oos"),
            "walk_forward": r.get("walk_forward"), "cost_model": r.get("cost_model")}


def compare_frame(df: pd.DataFrame, models: list[ModelSpec], *, seeds: list[int], window_variants: list[float],
                  predictive: bool, horizons: list[int], asset_class: str,
                  check: Callable[[], None] = lambda: None) -> dict[str, Any]:
    """All measurements for one dataset. ``check`` raises to stop (budget or cancellation) between units."""
    df = df.sort_values("ts").reset_index(drop=True)
    rows, flags = [], {}
    for spec in models:
        check()
        det = DETECTORS[spec.detector](**spec.params)
        row: dict[str, Any] = {"model": spec.key, "detector": spec.detector, "params": det.params,
                               "anomaly_type": det.anomaly_type}
        try:
            out, secs = _run(det, df)
        except Exception as exc:  # recorded as a failed row, the comparison goes on
            rows.append({**row, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            continue
        row.update(runtime_s=secs)
        if not out.applicable:
            rows.append({**row, "status": "not_applicable", "reason": out.reason})
            continue
        pos = _positions(df, out)
        scored = _scored_mask(df, out)
        flags[spec.key] = pos
        nominal = det.params.get("contamination")
        row.update(status="ok", n_scored=int(scored.sum()), n_flagged=len(pos), n_episodes=episodes(pos),
                   flag_rate=round(len(pos) / scored.sum(), 5) if scored.any() else None,
                   nominal_rate=nominal, n_bh_significant=_bh_significant(out), **_rates(df, scored, pos))
        tops = Counter(d.metrics["top_features"][0]["feature"] for d in out.detections
                       if d.metrics.get("top_features"))
        row["top_feature_counts"] = dict(tops.most_common())
        dropped = out.detections[0].metrics.get("features_dropped") if out.detections else None
        if dropped:
            row["features_dropped"] = dropped
        row["flags"] = [{"ts": str(d.event_ts), "close": round(float(df["close"].iloc[i]), 6),
                         "p": round(d.empirical_pvalue, 6), "stat": round(d.statistic, 3)}
                        for i, d in zip(pos, sorted(out.detections, key=lambda d: d.event_ts), strict=True)][-500:]
        stab: dict[str, Any] = {}
        if getattr(det, "stochastic", False):
            alt = []
            for s in seeds[1:]:
                check()
                o2 = DETECTORS[spec.detector](**{**spec.params, "seed": s}).run(df)
                alt.append({"seed": s, **overlap(pos, _positions(df, o2))})
            js = [a["jaccard"] for a in alt if a["jaccard"] is not None]
            stab["seeds"] = {"runs": alt, "jaccard_mean": round(float(np.mean(js)), 4) if js else None}
        else:
            stab["seeds"] = {"note": "deterministic: the seed does not change the result"}
        var = []
        for name, params in _variants(spec, window_variants):
            check()
            o2 = DETECTORS[spec.detector](**params).run(df)
            if o2.applicable:
                var.append({"variant": name, "n_flagged": len(o2.detections), **overlap(pos, _positions(df, o2))})
            else:
                var.append({"variant": name, "not_applicable": o2.reason})
        stab["window_variants"] = var
        row["stability"] = stab
        if predictive:
            check()
            try:
                row["predictive"] = _predictive(df, spec, out, horizons, asset_class)
            except Exception as exc:
                row["predictive"] = {"error": f"{type(exc).__name__}: {exc}"}
        rows.append(row)
    keys = list(flags)
    matrix = [[overlap(flags[a], flags[b]) if a != b else None for b in keys] for a in keys]
    return {"rows": rows, "overlap": {"models": keys, "matrix": matrix}, "chart": _chart(df), "n_bars": len(df)}


LIMITATIONS = [
    "Flag counts measure rarity under each model's own baseline; they are not evidence that the bars matter.",
    "Multivariate models flag clusters after one event because rolling features (kurtosis, autocorrelation, "
    "relative volatility) keep its trace; compare episodes, not flagged bars.",
    "Local Outlier Factor flags about twice its nominal rate on control series (synthetic benchmark); its "
    "distances lose contrast as the number of features grows.",
    "Predictive results use estimated costs (asset-class defaults), not broker quotes, and a 70/30 "
    "chronological split of each model's own events; an out-of-sample effect still needs a registered "
    "hypothesis and Gates A-D.",
    "Each extra model, seed or window variant is another trial on the same data; the run-wide "
    "Benjamini-Hochberg correction covers the out-of-sample tests of this comparison only.",
    "Tick volume (FX from MT5) is not traded size; volume features are dropped when coverage is below 80%.",
]


def _apply_run_wide_bh(results: dict[str, Any]) -> None:
    refs = [(d, i) for d in results["datasets"] for i, r in enumerate(d.get("rows", []))
            if (r.get("predictive") or {}).get("out_of_sample", {}).get("pvalue_one_sided") is not None]
    if not refs:
        return
    p = np.array([d["rows"][i]["predictive"]["out_of_sample"]["pvalue_one_sided"] for d, i in refs])
    for (d, i), a in zip(refs, benjamini_hochberg(p), strict=True):
        d["rows"][i]["predictive"]["oos_run_wide_adjusted_pvalue"] = round(float(a), 6)


def run_comparison(session: Session, cfg: ComparisonConfig, *, store: ParquetStore, actor: str = "api",
                   should_stop: Callable[[], bool] = lambda: False,
                   checkpoint: Callable[[], None] | None = None) -> Experiment:
    cfg.validate()
    datasets = []
    for ds_id in cfg.dataset_ids:
        ds = session.get(Dataset, ds_id)
        if ds is None:
            raise ValueError(f"unknown dataset {ds_id}")
        datasets.append(ds)
    if len({bool(d.is_synthetic) for d in datasets}) > 1:
        raise ValueError("refusing to mix synthetic and real datasets in one comparison")
    config = {"dataset_ids": list(cfg.dataset_ids),
              "models": [{"key": m.key, "detector": m.detector, "params": DETECTORS[m.detector](**m.params).params}
                         for m in cfg.models],
              "seeds": cfg.seeds, "window_variants": cfg.window_variants, "predictive": cfg.predictive,
              "horizons": cfg.horizons, "max_seconds": cfg.max_seconds, "benchmark": cfg.benchmark,
              "label": cfg.label}
    algo = f"model_lab@{VERSION}+" + "+".join(sorted({f"{m.detector}@{DETECTORS[m.detector].version}"
                                                      for m in cfg.models}))
    config_hash = stable_hash({"config": {k: v for k, v in config.items() if k not in ("label", "max_seconds")},
                               "data": [d.content_hash for d in datasets], "algo": algo})
    if not cfg.force:
        cached = session.scalar(select(Experiment).where(Experiment.config_hash == config_hash,
                                                         Experiment.kind == "model_comparison",
                                                         Experiment.status == "succeeded"))
        if cached is not None:
            audit.record(session, actor, "model_comparison_cache_hit", "experiment", cached.id)
            return cached
    exp = Experiment(kind="model_comparison", status="running", algorithm_version=algo,
                     code_version=code_version(), config=config, config_hash=config_hash, seed=cfg.seeds[0],
                     data_refs=[{"dataset_id": d.id, "content_hash": d.content_hash, "rows": d.rows,
                                 "symbol": d.instrument.symbol, "frequency": d.frequency,
                                 "is_synthetic": d.is_synthetic, "quality_status": d.quality_status}
                                for d in datasets])
    session.add(exp)
    session.flush()
    if checkpoint:
        checkpoint()  # nothing holds the database while the models run
    t0 = time.perf_counter()
    state = {"stopped": None}

    def check() -> None:
        if should_stop():
            state["stopped"] = "cancelled"
            raise Cancelled
        if time.perf_counter() - t0 > cfg.max_seconds:
            state["stopped"] = "budget"
            raise Cancelled

    results: dict[str, Any] = {"version": VERSION, "datasets": [],
                               "is_synthetic": bool(datasets[0].is_synthetic) if datasets else None,
                               "limitations": LIMITATIONS}
    try:
        if cfg.benchmark:
            try:
                results["synthetic_benchmark"] = synthetic_benchmark(cfg.models, check=check)
            except Cancelled:
                results["synthetic_benchmark"] = {"synthetic": True, "skipped": state["stopped"]}
        for ds in datasets:
            entry = {"dataset_id": ds.id, "symbol": ds.instrument.symbol, "frequency": ds.frequency,
                     "quality_status": ds.quality_status, "is_synthetic": ds.is_synthetic}
            if state["stopped"]:
                results["datasets"].append({**entry, "status": "skipped", "reason": state["stopped"]})
                continue
            if ds.quality_status == "fail":
                results["datasets"].append({**entry, "status": "skipped", "reason": "data quality: fail"})
                continue
            df = store.read(ds.path).sort_values("ts").reset_index(drop=True)
            if len(df) > MAX_ROWS:
                entry["truncated_to_last_bars"] = MAX_ROWS
                df = df.iloc[-MAX_ROWS:].reset_index(drop=True)
            try:
                entry.update(status="ok", **compare_frame(
                    df, cfg.models, seeds=cfg.seeds, window_variants=cfg.window_variants,
                    predictive=cfg.predictive, horizons=cfg.horizons, asset_class=ds.instrument.asset_class,
                    check=check))
            except Cancelled:
                entry.update(status="skipped", reason=state["stopped"])
            results["datasets"].append(entry)
        _apply_run_wide_bh(results)
        exp.status = "cancelled" if state["stopped"] == "cancelled" else "succeeded"
        if state["stopped"] == "budget":
            results["budget_exhausted"] = True
    except Exception as exc:  # recorded, not hidden
        exp.status, exp.error = "failed", f"{type(exc).__name__}: {exc}"
    results["duration_s"] = round(time.perf_counter() - t0, 3)
    exp.results = results
    exp.n_tests = sum(len(d.get("rows", [])) for d in results["datasets"])
    exp.finished_at = datetime.now(UTC)
    audit.record(session, actor, "model_comparison_finished", "experiment", exp.id, status=exp.status,
                 stopped=state["stopped"])
    return exp


# ------------------------------------------------------------------ synthetic benchmark


def _synthetic(symbol: str, n_bars: int, seed: int) -> pd.DataFrame:
    spec = InstrumentSpec(symbol, "other")
    raw = SyntheticAdapter().fetch(FetchRequest(spec, "1d", options={"seed": seed, "n_bars": n_bars}))
    df, _ = normalize_bars(raw, provider="synthetic", spec=spec, frequency="1d", availability_delay=pd.Timedelta(0),
                           is_synthetic=True, source_ref="model-lab-benchmark")
    return df.sort_values("ts").reset_index(drop=True)


WINDOWS = {"return_shock": 3, "volume_spike": 3, "volatility_regime": 80, "pair_divergence": 10}


def synthetic_benchmark(models: list[ModelSpec], n_bars: int = 3000, seed: int = 7,
                        check: Callable[[], None] = lambda: None) -> dict[str, Any]:
    """Recall of injected events and false-alarm rate on the control series. Synthetic data only."""
    events = injected_events(n_bars)
    symbols = sorted({e.symbol for e in events} | {"SYN-D"})
    frames = {s: _synthetic(s, n_bars, seed) for s in symbols}
    rows = []
    for spec in models:
        det = DETECTORS[spec.detector](**spec.params)
        if det.pairwise:
            continue
        outs = {}
        for s in symbols:
            check()
            outs[s] = det.run(frames[s])
        found = []
        for e in events:
            o = outs[e.symbol]
            pos = _positions(frames[e.symbol], o) if o.applicable else np.array([], int)
            w = WINDOWS[e.kind]
            hit = pos[(pos >= e.bar_index) & (pos <= e.bar_index + w)]
            scored = _scored_mask(frames[e.symbol], o) if o.applicable else np.zeros(n_bars, bool)
            found.append({"symbol": e.symbol, "kind": e.kind, "bar": e.bar_index, "description": e.detail,
                          "scored": bool(scored[e.bar_index]), "detected": bool(len(hit)),
                          "delay_bars": int(hit[0] - e.bar_index) if len(hit) else None,
                          "flags_in_window": int(len(hit)), "window_bars": w + 1})
        ctrl = outs["SYN-D"]
        if ctrl.applicable:
            sc = _scored_mask(frames["SYN-D"], ctrl)
            fa = {"scored": int(sc.sum()), "flags": len(ctrl.detections),
                  "rate": round(len(ctrl.detections) / max(sc.sum(), 1), 5),
                  "episodes": episodes(_positions(frames["SYN-D"], ctrl))}
        else:
            fa = {"not_applicable": ctrl.reason}
        scored_events = [f for f in found if f["scored"]]
        rows.append({"model": spec.key, "detector": spec.detector, "events": found,
                     "recall_scored": round(sum(f["detected"] for f in scored_events) / len(scored_events), 3)
                     if scored_events else None,
                     "control_false_alarms": fa, "nominal_rate": det.params.get("contamination")})
    return {"synthetic": True, "n_bars": n_bars, "seed": seed, "rows": rows,
            "note": "Known-anomaly benchmark on generated data. It checks that a model can find injected events "
                    "and how often it flags a series without them; it is not evidence about real markets. "
                    "Events inside a model's warm-up are not scored and are left out of its recall."}


def as_config(body: dict[str, Any]) -> ComparisonConfig:
    models = [ModelSpec(**m) for m in body.get("models") or DEFAULT_MODELS]
    kw = {k: body[k] for k in ("seeds", "window_variants", "predictive", "horizons", "max_seconds", "benchmark",
                               "force", "label") if k in body and body[k] is not None}
    return ComparisonConfig(dataset_ids=list(body.get("dataset_ids") or []), models=models, **kw)


__all__ = ["ComparisonConfig", "ModelSpec", "as_config", "compare_frame", "overlap", "run_comparison",
           "synthetic_benchmark"]
