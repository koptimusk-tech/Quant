"""Systematic relationship search (route C) on the research period only.

Every pair of comparable datasets (same bar size, asset class and synthetic flag) is tested, and every test
is stored, not only the ones that look good:

* Engle-Granger cointegration of log prices (residual of an OLS hedge regression);
* correlation of bar returns;
* stability of that correlation (first half vs second half of the research period, Fisher z);
* half-life of the hedge residual, from an AR(1) fit.

The three p-values of all pairs are corrected together (Benjamini-Hochberg), so scanning more pairs makes
each one harder to pass. Data from the last 20% of the pair's common bars, the future holdout of any
hypothesis built on it, is never read here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import combinations
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import select
from sqlalchemy.orm import Session
from statsmodels.tsa.stattools import coint

from qal import audit
from qal.anomaly.engine import benjamini_hochberg
from qal.data.store import ParquetStore
from qal.models import Dataset, Experiment
from qal.versioning import code_version, stable_hash

VERSION = "1.0.0"
HOLDOUT_FRACTION = 0.2
MAX_DATASETS_PER_GROUP = 12
MIN_OVERLAP = 500
ALPHA = 0.05
HALF_LIFE_RANGE = (1.0, 60.0)


def _half_life(resid: np.ndarray) -> float | None:
    e = resid[:-1] - resid[:-1].mean()
    d = np.diff(resid)
    denom = float(e @ e)
    if denom <= 0:
        return None
    b = float(e @ (d - d.mean())) / denom
    if not -1 < b < 0:
        return None  # not mean reverting
    return float(-np.log(2) / np.log1p(b))


def pair_tests(a: pd.Series, b: pd.Series) -> dict[str, Any] | None:
    """a, b: close prices indexed by bar start, already cut to the research period."""
    j = pd.concat([a, b], axis=1, join="inner").dropna()
    j = j[(j > 0).all(axis=1)]
    if len(j) < MIN_OVERLAP:
        return None
    ly, lx = np.log(j.iloc[:, 0].to_numpy()), np.log(j.iloc[:, 1].to_numpy())
    beta, alpha = np.polyfit(lx, ly, 1)
    resid = ly - (alpha + beta * lx)
    coint_p = float(coint(ly, lx, trend="c")[1])
    ry, rx = np.diff(ly), np.diff(lx)
    corr, corr_p = stats.pearsonr(ry, rx)
    h = len(ry) // 2
    c1, c2 = np.corrcoef(ry[:h], rx[:h])[0, 1], np.corrcoef(ry[h:], rx[h:])[0, 1]
    z = (np.arctanh(np.clip(c1, -0.999999, 0.999999)) - np.arctanh(np.clip(c2, -0.999999, 0.999999))) \
        / np.sqrt(1 / (h - 3) + 1 / (len(ry) - h - 3))
    return {"bars": len(j), "from": str(j.index[0]), "to": str(j.index[-1]),
            "hedge_beta": float(beta), "coint_pvalue": coint_p, "return_corr": float(corr),
            "corr_pvalue": float(corr_p), "corr_first_half": float(c1), "corr_second_half": float(c2),
            "corr_change_pvalue": float(2 * stats.norm.sf(abs(z))), "half_life_bars": _half_life(resid)}


def _groups(session: Session, include_synthetic: bool) -> dict[tuple[str, str, bool], list[Dataset]]:
    q = select(Dataset).where(Dataset.is_latest.is_(True), Dataset.quality_status != "fail")
    if not include_synthetic:
        q = q.where(Dataset.is_synthetic.is_(False))
    groups: dict[tuple[str, str, bool], list[Dataset]] = {}
    for d in session.scalars(q.order_by(Dataset.id)).all():
        groups.setdefault((d.frequency, d.instrument.asset_class, bool(d.is_synthetic)), []).append(d)
    return {k: sorted(v, key=lambda d: d.instrument.symbol)[:MAX_DATASETS_PER_GROUP]
            for k, v in sorted(groups.items()) if len(v) >= 2}


def run_relationship_scan(session: Session, *, include_synthetic: bool = False, actor: str = "api") -> Experiment:
    """Test every comparable pair; cached by the content of the data (same data -> same experiment)."""
    groups = _groups(session, include_synthetic)
    data = sorted((d.id, d.content_hash) for g in groups.values() for d in g)
    config = {"version": VERSION, "include_synthetic": include_synthetic, "alpha": ALPHA,
              "half_life_range": list(HALF_LIFE_RANGE), "holdout_fraction": HOLDOUT_FRACTION,
              "min_overlap": MIN_OVERLAP}
    config_hash = stable_hash({"config": config, "data": data, "algo": "relationship_scan"})
    cached = session.scalar(select(Experiment).where(Experiment.config_hash == config_hash,
                                                     Experiment.kind == "relationship_scan",
                                                     Experiment.status == "succeeded"))
    if cached is not None:
        return cached
    exp = Experiment(kind="relationship_scan", status="running", algorithm_version=f"relationship_scan@{VERSION}",
                     code_version=code_version(), config=config, config_hash=config_hash, seed=0,
                     data_refs=[{"dataset_id": i, "content_hash": h} for i, h in data])
    session.add(exp)
    session.flush()
    rows: list[dict[str, Any]] = []
    closes: dict[str, pd.Series] = {}
    for (freq, ac, syn), group in groups.items():
        for d in group:
            try:
                bars = ParquetStore.read(d.path)
            except OSError:
                continue
            closes[d.id] = bars.set_index("ts")["close"]
        for a, b in combinations([d for d in group if d.id in closes], 2):
            common = closes[a.id].index.intersection(closes[b.id].index).sort_values()
            row: dict[str, Any] = {"dataset_a": a.id, "dataset_b": b.id, "symbol_a": a.instrument.symbol,
                                   "symbol_b": b.instrument.symbol, "frequency": freq, "asset_class": ac,
                                   "is_synthetic": syn}
            if len(common) < MIN_OVERLAP / (1 - HOLDOUT_FRACTION):
                rows.append({**row, "status": "skipped", "reason": f"{len(common)} common bars"})
                continue
            cut = common[int(len(common) * (1 - HOLDOUT_FRACTION))]
            row["research_end"] = str(cut)  # bars from here on are left for the holdout
            res = pair_tests(closes[a.id][closes[a.id].index < cut], closes[b.id][closes[b.id].index < cut])
            rows.append({**row, "status": "ok", **res} if res else {**row, "status": "skipped",
                                                                  "reason": "too few research bars"})
    ok = [r for r in rows if r["status"] == "ok"]
    pv = np.array([r[k] for r in ok for k in ("coint_pvalue", "corr_pvalue", "corr_change_pvalue")])
    adj = benjamini_hochberg(pv) if len(pv) else pv
    for i, r in enumerate(ok):
        r["coint_adj_pvalue"], r["corr_adj_pvalue"], r["corr_change_adj_pvalue"] = (float(x) for x in adj[3 * i: 3 * i + 3])
        hl = r["half_life_bars"]
        r["cointegrated"] = r["coint_adj_pvalue"] < ALPHA
        r["tradable_reversion"] = bool(r["cointegrated"] and hl is not None
                                       and HALF_LIFE_RANGE[0] <= hl <= HALF_LIFE_RANGE[1])
        r["correlation_changed"] = r["corr_change_adj_pvalue"] < ALPHA
    exp.n_tests = len(pv)
    exp.results = {
        "pairs": rows, "n_pairs": len(rows), "n_tested": len(ok), "n_tests": len(pv),
        "n_cointegrated": sum(r["cointegrated"] for r in ok),
        "n_tradable_reversion": sum(r["tradable_reversion"] for r in ok),
        "n_correlation_changed": sum(r["correlation_changed"] for r in ok),
        "correction": "Benjamini-Hochberg over every p-value of every pair in this scan",
        "note": "Research period only (first 80% of each pair's common bars). Passing here is a reason to "
                "formulate a hypothesis, not evidence that a trading rule works.",
    }
    exp.status = "succeeded"
    exp.finished_at = datetime.now(UTC)
    audit.record(session, actor, "relationship_scan_finished", "experiment", exp.id, n_tests=exp.n_tests)
    return exp
