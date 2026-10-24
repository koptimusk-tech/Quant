"""Predictive validation batch: do the detected anomalies carry out-of-sample information on real data?

Runs the v2 event study (``qal.anomaly.event_study``) for every detector on the real datasets of the
requested symbols and frequencies, audits the data first, corrects across the whole run, and writes
reproducible outputs (``results.json``, ``comparison.csv``, ``data_audit.json``, ``report.md``).

Rules this module enforces:
- only real datasets are used; synthetic datasets are never selected or mixed in;
- a missing dataset is reported with the MT5 export that would provide it, never replaced;
- historical spread is used only when it aligns with the stored bars (time and price);
- predictive evidence (excess return out of sample) is kept apart from net profitability.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qal.anomaly import event_study
from qal.anomaly.engine import benjamini_hochberg
from qal.anomaly.priority import DEFAULT_CONFIG
from qal.data.adapters.local_files import _read_any, standardize_columns
from qal.data.store import ParquetStore
from qal.models import AnomalyDetection, Dataset, Instrument
from qal.versioning import code_version, stable_hash

DEFAULT_SYMBOLS = ("EURUSD", "USDCHF")
DEFAULT_FREQUENCIES = ("1d", "1h")
SINGLE_DETECTORS = ("return_shock", "volume_anomaly", "volatility_regime")
MIN_PAIR_BARS = 500
MIN_SPREAD_COVERAGE = 0.5
MAX_PRICE_MISMATCH = 2e-4  # median relative close difference allowed between spread source and dataset
MT5_TIMEFRAME = {"1d": "D1", "1h": "H1", "15m": "M15", "1wk": "W1"}

ACTIONS = {
    "data": "Corregir los datos antes de cualquier conclusión (calidad 'fail').",
    "insufficient_sample": "Ampliar historia o añadir otro instrumento; no relajar umbrales.",
    "no_evidence": "Bajar prioridad: sin información predictiva medible tras el benchmark.",
    "in_sample_only": "No perseguir: solo aparece dentro de muestra; revisar con datos nuevos más adelante.",
    "oos_weak": "Replicar en otro periodo o par: no sobrevive a la corrección de todo el lote.",
    "oos_gross": "Información predictiva sin margen tras costes: estudiar horizontes largos o costes reales.",
    "oos_net": "Candidata a hipótesis formal y backtest con costes reales del bróker (no es estrategia validada).",
    "not_run": "No ejecutado (ver motivo).",
}


def normalize_symbol(symbol: str) -> str:
    """'EURUSD=X', 'EUR/USD', 'EURUSD.m', 'eurusd' -> 'EURUSD...' (broker suffixes kept after the root)."""
    s = symbol.upper().replace("=X", "")
    return re.sub(r"[^A-Z0-9]", "", s)


def matches(symbol: str, target: str) -> bool:
    s, t = normalize_symbol(symbol), normalize_symbol(target)
    return s == t or (s.startswith(t) and len(s) <= len(t) + 4)


def mt5_export_hint(symbol: str, frequency: str) -> str:
    tf = MT5_TIMEFRAME.get(frequency, frequency)
    return (f"Exportar desde MetaTrader 5: Ver > Símbolos > Barras > {symbol}, periodo {tf}, rango completo > "
            f"Exportar barras (CSV con <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>); "
            f"cargarlo con: qal ingest local_file {symbol} fx --frequency {frequency} "
            "--options '{\"path\": \"<archivo>.csv\", \"format\": \"mt5\", \"source_timezone\": \"<zona del servidor>\"}'")


# ---------------------------------------------------------------- spread sources


@dataclass
class SpreadSource:
    """Historical spread in bps indexed by UTC bar time, plus how it was obtained."""

    symbol: str
    frequency: str
    bps: pd.Series
    close: pd.Series
    source: str
    notes: list[str] = field(default_factory=list)


def spread_from_mt5_csv(path: str | Path, symbol: str, frequency: str, point: float,
                        source_timezone: str | None = None) -> SpreadSource:
    """MT5 'Export bars' CSV: <SPREAD> is in points; converted to bps of the bar close."""
    raw = _read_any(Path(path), "mt5")
    if "spread" not in raw.columns:
        raise ValueError(f"{path}: no <SPREAD> column in this MT5 export")
    df = standardize_columns(raw)
    ts = df["ts"]
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(source_timezone or "UTC").dt.tz_convert("UTC")
    idx = pd.DatetimeIndex(ts)
    close = pd.Series(df["close"].to_numpy(float), index=idx)
    bps = pd.Series(df["spread"].to_numpy(float) * point, index=idx) / close * 1e4
    return SpreadSource(symbol, frequency, bps, close, f"mt5_csv:{Path(path).name}",
                        ["MT5 bar spread (points x point / close); MT5 stores one spread per bar, often the "
                         "minimum seen in the bar, so it can understate the spread paid."])


def spread_from_mt5_terminal(symbol: str, frequency: str, server_utc_offset_hours: float,
                             module: Any = None, n_bars: int = 100_000) -> SpreadSource:
    """Read bar spreads from a running MT5 terminal (Windows only)."""
    mt5 = module
    if mt5 is None:
        import MetaTrader5 as mt5  # type: ignore[import-not-found,no-redef]
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
    try:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"MT5 symbol not available: {symbol}")
        info = mt5.symbol_info(symbol)
        rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, f"TIMEFRAME_{MT5_TIMEFRAME[frequency]}"), 0, n_bars)
    finally:
        mt5.shutdown()
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"MT5 returned no bars for {symbol}")
    df = pd.DataFrame(rates)
    idx = pd.DatetimeIndex(pd.to_datetime(df["time"], unit="s", utc=True) - pd.Timedelta(hours=server_utc_offset_hours))
    close = pd.Series(df["close"].to_numpy(float), index=idx)
    bps = pd.Series(df["spread"].to_numpy(float) * float(info.point), index=idx) / close * 1e4
    return SpreadSource(symbol, frequency, bps, close, f"mt5_terminal:{symbol}",
                        [f"server_utc_offset_hours={server_utc_offset_hours} applied as a fixed offset (no DST).",
                         "MT5 bar spread, often the bar minimum: can understate the spread paid."])


def align_spread(src: SpreadSource, df: pd.DataFrame) -> tuple[pd.Series | None, dict[str, Any]]:
    """Use the spread only if it covers the bars and its closes match the dataset's (time alignment check)."""
    idx = pd.DatetimeIndex(df["ts"])
    if src.bps.index.has_duplicates:
        src.bps = src.bps[~src.bps.index.duplicated()]
        src.close = src.close[~src.close.index.duplicated()]
    s = src.bps.reindex(idx)
    c = src.close.reindex(idx)
    have = s.notna() & (s >= 0)
    coverage = float(have.mean()) if len(idx) else 0.0
    mism = (c[have] / df["close"].to_numpy(float)[have.to_numpy()] - 1).abs()
    median_mismatch = float(mism.median()) if len(mism) else None
    zero_share = float((s[have] == 0).mean()) if have.any() else None
    info = {"source": src.source, "coverage": round(coverage, 4), "median_close_mismatch": median_mismatch,
            "zero_spread_share": zero_share, "notes": list(src.notes),
            "median_bps": float(s[have].median()) if have.any() else None,
            "p5_bps": float(s[have].quantile(0.05)) if have.any() else None,
            "p95_bps": float(s[have].quantile(0.95)) if have.any() else None}
    if coverage < MIN_SPREAD_COVERAGE:
        info["used"], info["reason"] = False, f"coverage {coverage:.0%} < {MIN_SPREAD_COVERAGE:.0%}"
        return None, info
    if median_mismatch is None or median_mismatch > MAX_PRICE_MISMATCH:
        info["used"], info["reason"] = False, ("closes do not match the dataset at the same timestamps: "
                                               "probably a timezone/offset difference")
        return None, info
    if zero_share is not None and zero_share > 0.5:
        info["used"], info["reason"] = False, "more than half of the bars report a zero spread"
        return None, info
    info["used"] = True
    return s.where(have), info


# ---------------------------------------------------------------- data audit


def audit_dataset(ds: Dataset, df: pd.DataFrame) -> dict[str, Any]:
    q = ds.quality_summary or {}
    metrics = q.get("metrics", q)
    ts = pd.DatetimeIndex(df["ts"])
    step = ts.to_series().diff().dropna()
    delay = (pd.to_datetime(df["available_ts"]) - pd.to_datetime(df["event_ts"])).dt.total_seconds()
    rng = (df["high"] - df["low"]) / df["close"]
    ret = np.log(df["close"]).diff()
    z = (ret - ret.median()) / (1.4826 * (ret - ret.median()).abs().median())
    vol = df["volume"]
    vol_cov = float((vol.fillna(0) > 0).mean())
    asset_class = ds.instrument.asset_class
    vol_usable = bool(vol_cov >= 0.8 and vol.nunique() > 10)
    return {
        "dataset_id": ds.id, "symbol": ds.instrument.symbol, "asset_class": asset_class,
        "frequency": ds.frequency, "provider": ds.provider_id, "source_ref": ds.source_ref,
        "is_synthetic": bool(ds.is_synthetic), "content_hash": ds.content_hash, "rows": len(df),
        "start": str(ts.min()), "end": str(ts.max()), "timezone_stored": "UTC",
        "instrument_timezone": ds.instrument.timezone,
        "median_bar_step": str(step.median()) if len(step) else None,
        "duplicate_timestamps": int(ts.duplicated().sum()),
        "quality_status": ds.quality_status,
        "coverage": metrics.get("coverage"), "missing_bars": metrics.get("missing_bars"),
        "n_gaps": metrics.get("n_gaps"), "largest_gaps": (metrics.get("gaps") or [])[:5],
        "ohlc_inconsistent": int(((df["high"] < df[["open", "close"]].max(axis=1) - 1e-12)
                                  | (df["low"] > df[["open", "close"]].min(axis=1) + 1e-12)).sum()),
        "zero_range_bars": int((rng <= 0).sum()),
        "extreme_returns_gt_10_robust_sd": int((z.abs() > 10).sum()),
        "weekend_bars": int((ts.day_of_week >= 5).sum()),
        "availability_delay_s": {"min": float(delay.min()), "median": float(delay.median()), "max": float(delay.max())},
        "availability_rule": "event_ts = bar close; available_ts = event_ts + adapter delay; entries at the open "
                             "of the first bar starting at or after available_ts",
        "volume_coverage": round(vol_cov, 4),
        "volume_kind": ("tick volume (MT5/FX: number of price updates, not traded size)"
                        if asset_class == "fx" else "provider volume"),
        "volume_usable": vol_usable,
        "spread_in_dataset": False,
        "timestamp_notes": [
            "Stored bars are UTC. MT5 data loaded through the terminal adapter used a fixed server offset "
            "(no DST): around DST changes D1/H1 boundaries can be shifted by one hour.",
        ] if "metatrader" in ds.provider_id or "mt5" in (ds.source_ref or "").lower() else [],
    }


# ---------------------------------------------------------------- dataset selection


@dataclass
class Target:
    symbol: str
    frequency: str
    dataset: Dataset | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    rejected_synthetic: int = 0


def find_targets(session: Session, symbols: tuple[str, ...], frequencies: tuple[str, ...]) -> list[Target]:
    rows = session.execute(select(Dataset, Instrument).join(Instrument, Dataset.instrument_id == Instrument.id)
                           .where(Dataset.is_latest.is_(True))).all()
    out = []
    for sym in symbols:
        for freq in frequencies:
            t = Target(sym, freq)
            for ds, ins in rows:
                if ds.frequency != freq or not matches(ins.symbol, sym):
                    continue
                if ds.is_synthetic:
                    t.rejected_synthetic += 1
                    continue
                t.candidates.append({"dataset_id": ds.id, "symbol": ins.symbol, "provider": ds.provider_id,
                                     "rows": ds.rows, "quality": ds.quality_status})
                # Prefer the longest real history; quality 'fail' only if nothing else exists.
                rank = (ds.quality_status != "fail", ds.rows or 0)
                if t.dataset is None or rank > (t.dataset.quality_status != "fail", t.dataset.rows or 0):
                    t.dataset = ds
            out.append(t)
    return out


# ---------------------------------------------------------------- comparison helpers


def _anomaly_summary(session: Session, dataset_id: str, detector: str) -> dict[str, Any]:
    n, min_adj = session.execute(select(func.count(AnomalyDetection.id), func.min(AnomalyDetection.adjusted_pvalue))
                                 .where(AnomalyDetection.dataset_id == dataset_id,
                                        AnomalyDetection.detector == detector)).one()
    return {"persisted_anomalies": int(n or 0), "min_adjusted_pvalue": None if min_adj is None else float(min_adj)}


def _priority_summary(session: Session, dataset_id: str, detector: str, limit: int = 300) -> float | None:
    from qal.anomaly.priority import enrich

    items = session.scalars(select(AnomalyDetection).where(AnomalyDetection.dataset_id == dataset_id,
                                                           AnomalyDetection.detector == detector)
                            .order_by(AnomalyDetection.adjusted_pvalue.asc()).limit(limit)).all()
    if not items:
        return None
    vals = [v["priority"]["value"] for v in enrich(session, items).values() if v["priority"].get("value") is not None]
    return max(vals) if vals else None


def next_action(row: dict[str, Any]) -> str:
    if row.get("status") != "ok":
        return ACTIONS["not_run"]
    if row.get("data_quality") == "fail":
        return ACTIONS["data"]
    v = row["verdict"]
    if v != "oos_supported":
        return ACTIONS.get(v, ACTIONS["no_evidence"])
    if not row.get("survives_run_wide_correction"):
        return ACTIONS["oos_weak"]
    return ACTIONS["oos_net"] if row.get("survives_base_costs_oos") else ACTIONS["oos_gross"]


def _fmt_ci(ci) -> str:
    return "" if not ci else f"[{ci[0]:.2f}, {ci[1]:.2f}]"


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


# ---------------------------------------------------------------- the run


@dataclass
class RunConfig:
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    frequencies: tuple[str, ...] = DEFAULT_FREQUENCIES
    horizons: tuple[int, ...] = tuple(event_study.DEFAULT_HORIZONS)
    seed: int = 0
    pair: tuple[str, str] | None = ("EURUSD", "USDCHF")
    spreads: list[SpreadSource] = field(default_factory=list)


def run(session: Session, store: ParquetStore, cfg: RunConfig, out_dir: str | Path,
        log: Callable[[str], None] = print) -> dict[str, Any]:
    started = datetime.now(UTC)
    targets = find_targets(session, cfg.symbols, cfg.frequencies)
    audit: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    frames: dict[tuple[str, str], tuple[Dataset, pd.DataFrame, pd.Series | None, dict | None]] = {}
    for t in targets:
        if t.dataset is None:
            missing.append({"symbol": t.symbol, "frequency": t.frequency,
                            "synthetic_datasets_ignored": t.rejected_synthetic,
                            "how_to_provide": mt5_export_hint(t.symbol, t.frequency)})
            log(f"missing real dataset: {t.symbol} {t.frequency}")
            continue
        df = store.read(t.dataset.path)
        if df["is_synthetic"].any():  # belt and braces: the bars themselves must be real
            missing.append({"symbol": t.symbol, "frequency": t.frequency,
                            "reason": "dataset marked real contains synthetic bars; refused",
                            "how_to_provide": mt5_export_hint(t.symbol, t.frequency)})
            continue
        a = audit_dataset(t.dataset, df)
        a["candidates"] = t.candidates
        a["synthetic_datasets_ignored"] = t.rejected_synthetic
        spread, sinfo = None, None
        for src in cfg.spreads:
            if matches(src.symbol, t.symbol) and src.frequency == t.frequency:
                spread, sinfo = align_spread(src, df)
        a["historical_spread"] = sinfo or {"used": False, "reason": "no spread source given for this dataset"}
        audit.append(a)
        frames[(t.symbol, t.frequency)] = (t.dataset, df, spread, sinfo)

    studies: list[dict[str, Any]] = []

    def study(key, detector, related=None, spread=None, sinfo=None):
        ds, _, _, _ = frames[key]
        row = {"symbol": key[0], "frequency": key[1], "dataset_id": ds.id, "detector": detector,
               "related_dataset_id": related.id if related else None,
               "related_symbol": related.instrument.symbol if related else None,
               "data_quality": ds.quality_status if not related else
               ("fail" if "fail" in (ds.quality_status, related.quality_status) else
                "warn" if "warn" in (ds.quality_status, related.quality_status) else ds.quality_status)}
        log(f"event study {detector} on {key[0]} {key[1]}" + (f" vs {row['related_symbol']}" if related else ""))
        exp = event_study.run_event_study(session, ds.id, detector, store=store, horizons=list(cfg.horizons),
                                          seed=cfg.seed, related_dataset_id=related.id if related else None,
                                          spread_bps=spread, spread_source=(sinfo or {}).get("source"),
                                          actor="predictive-study")
        session.flush()
        row.update(experiment_id=exp.id, status="ok" if exp.status == "succeeded" else "failed", error=exp.error,
                   result=exp.results)
        studies.append(row)

    for key, (ds, df, spread, sinfo) in frames.items():
        for det in SINGLE_DETECTORS:
            if det == "volume_anomaly" and not next(a for a in audit if a["dataset_id"] == ds.id)["volume_usable"]:
                studies.append({"symbol": key[0], "frequency": key[1], "dataset_id": ds.id, "detector": det,
                                "status": "skipped", "reason": "volume not usable (coverage < 80% or constant)",
                                "data_quality": ds.quality_status})
                continue
            study(key, det, spread=spread, sinfo=sinfo)
    if cfg.pair:
        y_sym, x_sym = cfg.pair
        for freq in cfg.frequencies:
            ky, kx = (y_sym, freq), (x_sym, freq)
            if ky not in frames or kx not in frames:
                studies.append({"symbol": y_sym, "frequency": freq, "detector": "pair_divergence",
                                "related_symbol": x_sym, "status": "skipped",
                                "reason": "one leg of the pair has no real dataset at this frequency"})
                continue
            common = pd.DatetimeIndex(frames[ky][1]["ts"]).intersection(pd.DatetimeIndex(frames[kx][1]["ts"]))
            if len(common) < MIN_PAIR_BARS:
                studies.append({"symbol": y_sym, "frequency": freq, "detector": "pair_divergence",
                                "related_symbol": x_sym, "status": "skipped",
                                "reason": f"only {len(common)} synchronised bars (< {MIN_PAIR_BARS})"})
                continue
            # Two-leg spread costs need both legs; with one leg only, the fixed scenarios are used.
            study(ky, "pair_divergence", related=frames[kx][0])

    # Run-wide multiple-testing correction over every in-sample combination of every study.
    flat = [(i, j, r["in_sample"].get("pvalue")) for i, s in enumerate(studies) if s.get("status") == "ok"
            for j, r in enumerate(s["result"].get("table", []))]
    adj = benjamini_hochberg(np.array([p if p is not None else 1.0 for _, _, p in flat])) if flat else []
    for (i, j, _), a in zip(flat, adj, strict=True):
        studies[i]["result"]["table"][j]["in_sample"]["run_wide_adjusted_pvalue"] = float(a)
    oos_p = [(i, s["result"]["out_of_sample"].get("pvalue_one_sided")) for i, s in enumerate(studies)
             if s.get("status") == "ok" and s["result"].get("selected")]
    oos_adj = benjamini_hochberg(np.array([p if p is not None else 1.0 for _, p in oos_p])) if oos_p else []
    for (i, _), a in zip(oos_p, oos_adj, strict=True):
        studies[i]["oos_run_wide_adjusted_pvalue"] = float(a)

    comparison = []
    for s in studies:
        row: dict[str, Any] = {"dataset": f"{s['symbol']} {s['frequency']}", "detector": s["detector"],
                               "pair_with": s.get("related_symbol"), "status": s.get("status"),
                               "reason": s.get("reason") or s.get("error"), "data_quality": s.get("data_quality")}
        if s.get("status") == "ok":
            res = s["result"]
            sel = res.get("selected") or {}
            sel_row = next((r for r in res.get("table", []) if sel and r["horizon"] == sel["horizon"]
                            and r["rule"] == sel["rule"]), None)
            oos = res.get("out_of_sample") or {}
            surv = res.get("survives_costs_oos") or {}
            row.update(_anomaly_summary(session, s["dataset_id"], s["detector"]))
            row["max_investigation_priority"] = _r(_priority_summary(session, s["dataset_id"], s["detector"]), 3)
            row.update({
                "events": res.get("n_events"),
                "independent_events": sel_row["n_independent"] if sel_row else
                max((t["n_independent"] for t in res.get("table", [])), default=0),
                "oos_events": oos.get("n"),
                "selected_horizon": sel.get("horizon"), "selected_rule": sel.get("rule"),
                "is_mean_gross_bps": _r((sel.get("in_sample") or {}).get("mean_gross_bps")),
                "is_mean_excess_bps": _r((sel.get("in_sample") or {}).get("mean_excess_bps")),
                "is_adj_p_study": _r((sel.get("in_sample") or {}).get("adjusted_pvalue"), 4),
                "is_adj_p_run_wide": _r(sel_row["in_sample"].get("run_wide_adjusted_pvalue"), 4) if sel_row else None,
                "oos_mean_gross_bps": _r(oos.get("mean_gross_bps")),
                "oos_mean_excess_bps": _r(oos.get("mean_excess_bps")),
                "oos_ci95_excess_bps": _fmt_ci(oos.get("ci95_excess_bps")),
                "oos_p_one_sided": _r(oos.get("pvalue_one_sided"), 4),
                "oos_p_run_wide": _r(s.get("oos_run_wide_adjusted_pvalue"), 4),
                "oos_hit_rate": _r(oos.get("hit_rate_gross"), 3),
                "oos_mean_net_base_bps": _r(((oos.get("by_cost") or {}).get("base_estimate") or {}).get("mean_net_bps")),
                "oos_worst_trade_base_bps": _r(((oos.get("by_cost") or {}).get("base_estimate") or {}).get("worst_trade_bps")),
                "oos_max_dd_base_bps": _r(((oos.get("by_cost") or {}).get("base_estimate") or {}).get("max_drawdown_bps")),
                "survives_costs_oos": ";".join(k for k, v in surv.items() if v and k != "zero_cost") or "none",
                "survives_base_costs_oos": bool(surv.get("base_estimate")),
                "survives_run_wide_correction": bool(sel_row and (sel_row["in_sample"].get("run_wide_adjusted_pvalue")
                                                                  or 1) < event_study.ALPHA
                                                     and (s.get("oos_run_wide_adjusted_pvalue") or 1)
                                                     < event_study.ALPHA),
                "costs_used": ",".join(res.get("cost_model", {}).get("scenarios", [])),
                "walk_forward_folds_positive": sum(1 for f in res.get("walk_forward", [])
                                                   if (f.get("mean_excess_bps") or 0) > 0),
                "walk_forward_folds": len(res.get("walk_forward", [])),
                "verdict": res.get("verdict"),
                "experiment_id": s.get("experiment_id"),
            })
        row["next_action"] = next_action(row)
        comparison.append(row)

    result = {
        "kind": "predictive_validation", "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(), "code_version": code_version(),
        "event_study_version": event_study.VERSION,
        "config": {"symbols": list(cfg.symbols), "frequencies": list(cfg.frequencies),
                   "horizons": list(cfg.horizons), "seed": cfg.seed, "pair": list(cfg.pair) if cfg.pair else None,
                   "spread_sources": [s.source for s in cfg.spreads]},
        "data_hashes": {a["dataset_id"]: a["content_hash"] for a in audit},
        "priority_note": f"INVESTIGATION PRIORITY weights {DEFAULT_CONFIG.weights} are heuristic; they were "
                         "not calibrated on future returns. Rarity, significance, economic relevance, predictive "
                         "capacity and net profitability are reported separately.",
        "missing_datasets": missing, "data_audit": audit, "studies": studies, "comparison": comparison,
    }
    result["run_hash"] = stable_hash({"config": result["config"], "data": result["data_hashes"],
                                      "code": result["code_version"]})
    write_outputs(result, out_dir)
    return result


# ---------------------------------------------------------------- outputs


COMPARISON_COLUMNS = [
    "dataset", "detector", "pair_with", "status", "events", "independent_events", "oos_events",
    "persisted_anomalies", "min_adjusted_pvalue", "max_investigation_priority", "selected_horizon",
    "selected_rule", "is_mean_gross_bps", "is_mean_excess_bps", "is_adj_p_study", "is_adj_p_run_wide",
    "oos_mean_gross_bps", "oos_mean_excess_bps", "oos_ci95_excess_bps", "oos_p_one_sided", "oos_p_run_wide",
    "oos_hit_rate", "oos_mean_net_base_bps", "oos_worst_trade_base_bps", "oos_max_dd_base_bps",
    "survives_costs_oos", "survives_run_wide_correction", "walk_forward_folds_positive", "walk_forward_folds",
    "verdict", "costs_used", "data_quality", "reason", "next_action", "experiment_id",
]

VERDICT_ES = {"oos_supported": "apoyada fuera de muestra", "in_sample_only": "solo dentro de muestra",
              "no_evidence": "sin evidencia", "insufficient_sample": "muestra insuficiente", None: "-"}


def write_outputs(result: dict[str, Any], out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    (out / "data_audit.json").write_text(json.dumps({"datasets": result["data_audit"],
                                                     "missing": result["missing_datasets"]}, indent=2, default=str),
                                         encoding="utf-8")
    with open(out / "comparison.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COMPARISON_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in result["comparison"]:
            w.writerow(row)
    (out / "report.md").write_text(render_report(result), encoding="utf-8")
    return out


def render_report(r: dict[str, Any]) -> str:
    r = {**r, "comparison": [{k: ("-" if v is None or v == "" else v) for k, v in c.items()}
                             for c in r["comparison"]]}
    L = [f"# Validación predictiva — {r['started_at'][:19]} UTC", "",
         f"Código `{r['code_version']}` · event study v{r['event_study_version']} · semilla {r['config']['seed']} · "
         f"horizontes {r['config']['horizons']} · run `{r['run_hash'][:12]}`", "",
         "Datos reales únicamente. Ningún dato sintético se ha usado ni mezclado. Los costes son **estimaciones** "
         "salvo el escenario `historical_spread`, que viene del spread histórico de MT5 si se alineó con las barras.", ""]
    L += ["## Datos", "", "| Dataset | Filas | Periodo | Calidad | Cobertura | Huecos | Volumen | Spread histórico |",
          "|---|---|---|---|---|---|---|---|"]
    for a in r["data_audit"]:
        hs = a["historical_spread"]
        L.append(f"| {a['symbol']} {a['frequency']} ({a['provider']}) | {a['rows']} | {a['start'][:10]} → "
                 f"{a['end'][:10]} | {a['quality_status']} | {a['coverage']} | {a['n_gaps']} | "
                 f"{'usable' if a['volume_usable'] else 'no usable'} ({a['volume_kind'].split(' (')[0]}) | "
                 f"{'sí, ' + str(hs.get('coverage')) if hs.get('used') else 'no: ' + str(hs.get('reason'))} |")
    if r["missing_datasets"]:
        L += ["", "**Datasets que faltan (no se han inventado):**", ""]
        for m in r["missing_datasets"]:
            L.append(f"- {m['symbol']} {m['frequency']}: {m.get('reason', 'no hay dataset real')}. "
                     f"{m['how_to_provide']}")
    L += ["", "## Resultados por dataset y tipo de anomalía", "",
          "Exceso = rendimiento futuro menos el rendimiento incondicional causal del mismo horizonte (solo historia "
          "anterior a la entrada). Selección (horizonte × regla) en el primer 70 % de eventos; evaluación una sola "
          "vez en el 30 % final. bps.", "",
          "| Dataset | Anomalía | Eventos (indep. / OOS) | Selección | Exceso IS | p adj (lote) | Exceso OOS [IC95] | "
          "p OOS (lote) | Neto OOS base | Sobrevive costes | Veredicto | Siguiente acción |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in r["comparison"]:
        if c["status"] != "ok":
            L.append(f"| {c['dataset']} | {c['detector']}{' / ' + c['pair_with'] if c.get('pair_with') not in (None, '-') else ''} | - | - "
                     f"| - | - | - | - | - | - | no ejecutado: {c.get('reason')} | {c['next_action']} |")
            continue
        L.append(f"| {c['dataset']} | {c['detector']}{' / ' + c['pair_with'] if c.get('pair_with') not in (None, '-') else ''} | "
                 f"{c['events']} ({c['independent_events']} / {c['oos_events']}) | "
                 f"{c['selected_horizon'] or '-'} {c['selected_rule'] or ''} | {c['is_mean_excess_bps']} | "
                 f"{c['is_adj_p_run_wide']} | {c['oos_mean_excess_bps']} {c['oos_ci95_excess_bps']} | "
                 f"{c['oos_p_run_wide']} | {c['oos_mean_net_base_bps']} | {c['survives_costs_oos']} | "
                 f"{VERDICT_ES.get(c['verdict'], c['verdict'])} | {c['next_action']} |")
    L += ["", "## Método", "",
          "- Entrada en la apertura de la primera barra que empieza en o después de `available_ts`; salida al cierre "
          "de la barra h. Dirección: continuación o reversión del movimiento de la barra del evento (pares: "
          "reversión o continuación del spread largo Y / corto β·X, β del momento del evento, costes de las dos patas).",
          "- Eventos solapados en el mismo horizonte se filtran para contar solo eventos independientes.",
          "- Trades dentro de muestra que siguen abiertos en el corte se purgan.",
          "- Benjamini-Hochberg dentro de cada estudio y otra vez sobre todas las combinaciones del lote; la p fuera "
          "de muestra (unilateral) también se corrige sobre el lote.",
          "- IC por bootstrap (2000 remuestreos, semilla fija por fila).",
          "- Walk-forward real: en cada pliegue la regla se elige solo con pliegues anteriores.",
          "- volatility_regime se evalúa con rendimientos futuros, nunca con el nivel de volatilidad.",
          "", "## Limitaciones", "",
          "- Costes estimados (escenarios bajo/base/alto) salvo spread histórico alineado; no son costes del bróker. "
          "Swap/financiación no incluido (importa en D1 con horizontes de 10–20 días).",
          "- El spread de barra de MT5 suele ser el mínimo de la barra: puede infraestimar el coste.",
          "- Volumen FX = tick volume (número de cotizaciones), no volumen negociado.",
          "- MT5 vía terminal usa un desfase horario fijo (sin horario de verano).",
          f"- {r['priority_note']}",
          "- Evidencia predictiva no es una estrategia rentable ni validada.", ""]
    return "\n".join(L)
