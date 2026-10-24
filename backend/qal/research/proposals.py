"""Deterministic hypothesis proposals built from the data and anomalies already in the lab.

Proposals are drafts, not experiments: nothing is backtested here and nothing counts as a trial until a
person registers the contract and runs its validation. Each proposal says why it was suggested (the
evidence it is based on), how many variants it would add to the trial count, and whether an equivalent
hypothesis already exists, so the list does not invite re-testing the same idea under a new name.

Templates (each one a falsifiable contract with success criteria and an isolated holdout fixed up front):
* Time-series momentum on daily/weekly series (documented trend persistence in FX, futures, indices, crypto).
* Reversal or continuation after detected return shocks, when the scans found enough of them.
* Mean reversion of the spread between two strongly correlated instruments of the same bar size.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from itertools import combinations
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qal.data.store import ParquetStore
from qal.models import AnomalyDetection, Dataset, Hypothesis
from qal.research.contract import HypothesisContract

HOLDOUT_FRACTION = 0.2
MIN_PAIR_CORR = 0.5
MAX_PAIR_DATASETS = 12
MIN_EVENTS = 10
MOMENTUM_PREMISE = {
    "fx": "Exchange rates show trend persistence over one to six months because carry flows and expectations "
          "about interest-rate differentials adjust slowly.",
    "crypto": "Crypto assets show trend persistence because flows from retail and momentum funds chase recent "
              "returns and information diffuses slowly.",
}
DEFAULT_MOMENTUM_PREMISE = ("Prices of this asset class show trend persistence over weeks to months because "
                            "investors under-react to news and institutional flows are slow to adjust.")
HOLD_GRID = {"1h": [3, 6, 12], "4h": [2, 4, 8], "1d": [1, 3, 5], "1wk": [1, 2, 4]}


def _iso(ts: datetime) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").isoformat() if pd.Timestamp(ts).tzinfo else \
        pd.Timestamp(ts).tz_localize("UTC").isoformat()


def _design(ds: Dataset, horizon: int) -> dict[str, Any] | None:
    """Walk-forward sizes and holdout start from the length of the data; None if it is too short."""
    research = int(ds.rows * (1 - HOLDOUT_FRACTION))
    train = int(min(max(research * 0.4, 50), 2000))
    test = int(min(max(research * 0.1, 10), 500))
    embargo = max(horizon, 1)
    if research < train + embargo + 2 * test or ds.start_ts is None or ds.end_ts is None:
        return None
    start, end = pd.Timestamp(ds.start_ts), pd.Timestamp(ds.end_ts)
    holdout = start + (end - start) * (1 - HOLDOUT_FRACTION)
    return {"train_bars": train, "test_bars": test, "embargo_bars": embargo, "holdout_start": _iso(holdout)}


def _key(strategy: str, instrument_ids: list[str]) -> str:
    return strategy + ":" + ",".join(sorted(instrument_ids))


def _proposal(kind: str, datasets: list[Dataset], contract: dict[str, Any], priority: float,
              evidence: dict[str, Any], why: str, existing: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    HypothesisContract.model_validate(contract)  # a proposal is always a valid contract
    key = _key(contract["strategy"]["type"], [d.instrument_id for d in datasets])
    n_var = 1
    for v in contract["strategy"].get("param_grid", {}).values():
        n_var *= len(v)
    return {
        "id": hashlib.sha256((key + str(sorted(d.id for d in datasets))).encode()).hexdigest()[:16],
        "kind": kind,
        "title": contract["title"],
        "why": why,
        "priority": round(float(priority), 3),
        "evidence": evidence,
        "variants": n_var,
        "is_synthetic": any(d.is_synthetic for d in datasets),
        "existing_hypotheses": existing.get(key, []),
        "contract": contract,
    }


def _common(title: str, ds_ids: list[str], features: list[str], strategy: dict[str, Any], entry: str, exit_: str,
            horizon: int, premise: str, mechanism: str, risks: list[str], invalidation: list[str],
            design: dict[str, Any], sources: list[str] | None = None) -> dict[str, Any]:
    return {
        "title": title, "premise": premise, "mechanism": mechanism, "source_anomaly_ids": sources or [],
        "dataset_ids": ds_ids, "features": features, "strategy": strategy, "entry_rule": entry,
        "exit_rule": exit_, "horizon_bars": horizon, "benchmark": "cash", "risks": risks,
        "invalidation": invalidation, "validation": design,
    }


def _label(ds: Dataset) -> str:
    return f"{'SYNTHETIC ' if ds.is_synthetic else ''}{ds.instrument.symbol} {ds.frequency}"


def _momentum(ds: Dataset, existing) -> dict[str, Any] | None:
    if ds.frequency not in ("1d", "1wk"):
        return None
    design = _design(ds, 1)
    if design is None:
        return None
    grid = [20, 60, 120] if ds.frequency == "1d" else [4, 12, 26]
    contract = _common(
        f"{_label(ds)}: time-series momentum", [ds.id], [f"{g}-bar trailing return sign" for g in grid],
        {"type": "ts_momentum", "params": {}, "param_grid": {"lookback": grid}},
        "Hold the sign of the trailing lookback return from the next open",
        "Re-evaluated every bar; position flips when the trailing return changes sign", 1,
        MOMENTUM_PREMISE.get(ds.instrument.asset_class, DEFAULT_MOMENTUM_PREMISE),
        "Slow-moving flows and gradual repricing of fundamentals keep returns positively autocorrelated at "
        "multi-week horizons, so the recent trend predicts the next bar's sign slightly better than chance.",
        ["Sharp trend reversals around policy surprises", "Long flat periods where costs dominate"],
        ["Net out-of-sample Sharpe at or below zero", "Edge disappears when costs are doubled"], design)
    return _proposal("momentum", [ds], contract, 0.6, {"bars": ds.rows, "lookbacks": grid},
                     "Trend persistence is one of the best documented effects across asset classes; this tests "
                     f"it on {ds.instrument.symbol} with three lookbacks.", existing)


def _event(ds: Dataset, stats: dict[str, Any], existing) -> dict[str, Any] | None:
    n, n_sig = stats.get("n", 0), stats.get("n_sig", 0)
    if n < MIN_EVENTS:
        return None
    hold = HOLD_GRID.get(ds.frequency, [2, 5, 10])
    design = _design(ds, max(hold))
    if design is None:
        return None
    contract = _common(
        f"{_label(ds)}: reaction after return shocks", [ds.id], ["return_shock robust z-score"],
        {"type": "event", "params": {"detector": "return_shock", "threshold": 4.0},
         "param_grid": {"mode": ["reversal", "continuation"], "hold_bars": hold}},
        "At the next open after a bar with robust |z| >= 4, trade against (reversal) or with (continuation) it",
        f"Exit after the fixed holding period ({', '.join(map(str, hold))} bars, chosen in-sample)", max(hold),
        "Extreme one-bar moves are partly liquidity-driven, so prices tend to react predictably in the bars "
        "that follow, either correcting the overshoot or continuing as information spreads.",
        "Market orders hitting thin liquidity overshoot fair value and are later absorbed (reversal); or the "
        "move reflects new information that slower participants trade on afterwards (continuation).",
        ["Too few shock events for a reliable estimate", "Spreads widen exactly when shocks happen"],
        ["Net out-of-sample Sharpe at or below zero", "Fewer than 30 trades out of sample"],
        design, stats.get("ids", [])[:20])
    priority = 0.25 + 0.35 * min(n / 100, 1.0) + (0.3 if n_sig else 0.0)
    why = (f"The scans found {n} return shocks on {ds.instrument.symbol}"
           f"{f', {n_sig} of them significant after multiple-testing control' if n_sig else ', none significant after multiple-testing control'}"
           f"{'. Few events: the 30-trade minimum may not be reached' if n < 30 else ''}.")
    return _proposal("event", [ds], contract, priority, {"shocks": n, "significant": n_sig}, why, existing)


def _pairs(datasets: list[Dataset], existing) -> list[dict[str, Any]]:
    out = []
    groups: dict[tuple[str, str], list[Dataset]] = {}
    for d in datasets[:MAX_PAIR_DATASETS]:
        groups.setdefault((d.frequency, d.instrument.asset_class), []).append(d)
    for (freq, _), group in groups.items():
        rets = {}
        for d in group:
            try:
                bars = ParquetStore.read(d.path)
            except OSError:
                continue
            rets[d.id] = np.log(bars.set_index("event_ts")["close"]).diff()
        for a, b in combinations([d for d in group if d.id in rets], 2):
            joined = pd.concat([rets[a.id], rets[b.id]], axis=1, join="inner").dropna()
            if len(joined) < 300:
                continue
            corr = float(joined.corr().iloc[0, 1])
            if corr < MIN_PAIR_CORR:
                continue
            shorter = a if a.rows <= b.rows else b
            design = _design(shorter, 20)
            if design is None:
                continue
            contract = _common(
                f"{_label(a)} vs {b.instrument.symbol}: spread mean reversion", [a.id, b.id],
                ["rolling OLS spread z-score"],
                {"type": "pair_mean_reversion", "params": {"exit_z": 0.5, "max_hold": 20},
                 "param_grid": {"entry_z": [2.0, 2.5]}},
                "Open the spread when its rolling z-score passes the entry level, at the next open",
                "Close when the z-score is back inside 0.5 or after 20 bars", 20,
                f"{a.instrument.symbol} and {b.instrument.symbol} share common drivers (return correlation "
                f"{corr:.2f}), so temporary divergences in their relative price tend to close.",
                "Arbitrageurs and relative-value funds trade the two legs against each other, which pulls the "
                "spread back towards its rolling equilibrium after idiosyncratic shocks.",
                ["The relationship breaks permanently (structural change)", "Two legs double the costs"],
                ["Net out-of-sample Sharpe at or below zero", "Spread fails to revert within 20 bars on average"],
                design)
            out.append(_proposal("pair", [a, b], contract, 0.3 + 0.4 * (corr - MIN_PAIR_CORR) / (1 - MIN_PAIR_CORR),
                                 {"return_correlation": round(corr, 3), "overlap_bars": len(joined), "bar": freq},
                                 f"Returns of {a.instrument.symbol} and {b.instrument.symbol} are correlated "
                                 f"({corr:.2f} over {len(joined)} bars).", existing))
    return out


def build_proposals(session: Session) -> list[dict[str, Any]]:
    datasets = [d for d in session.scalars(select(Dataset).where(Dataset.is_latest.is_(True))
                                           .order_by(Dataset.is_synthetic, Dataset.ingested_at.desc())).all()
                if d.quality_status != "fail"]
    by_ds = {d.id: d for d in datasets}
    stats: dict[str, dict[str, Any]] = {}
    rows = session.execute(
        select(AnomalyDetection.dataset_id, AnomalyDetection.id, AnomalyDetection.adjusted_pvalue)
        .where(AnomalyDetection.detector == "return_shock", AnomalyDetection.related_dataset_id.is_(None),
               AnomalyDetection.dataset_id.in_(list(by_ds)))).all()
    for ds_id, aid, adj in rows:
        st = stats.setdefault(ds_id, {"n": 0, "n_sig": 0, "ids": []})
        st["n"] += 1
        st["n_sig"] += int(adj is not None and adj < 0.05)
        st["ids"].append(aid)

    existing: dict[str, list[dict[str, Any]]] = {}
    ds_instrument = dict(session.execute(select(Dataset.id, Dataset.instrument_id)).all())
    trials = dict(session.execute(select(Hypothesis.family_id, func.count()).group_by(Hypothesis.family_id)).all())
    for h in session.scalars(select(Hypothesis)).all():
        c = h.contract or {}
        inst = [ds_instrument.get(i, i) for i in c.get("dataset_ids", [])]
        existing.setdefault(_key(c.get("strategy", {}).get("type", ""), inst), []).append(
            {"id": h.id, "title": h.title, "status": h.status, "versions_in_family": trials.get(h.family_id, 1)})

    out: list[dict[str, Any]] = []
    for d in datasets:
        for p in (_momentum(d, existing), _event(d, stats.get(d.id, {}), existing)):
            if p:
                out.append(p)
    out += _pairs(datasets, existing)
    # Untested ideas on real data first; already-registered ones sink to the bottom.
    out.sort(key=lambda p: (bool(p["existing_hypotheses"]), p["is_synthetic"], -p["priority"]))
    return out
