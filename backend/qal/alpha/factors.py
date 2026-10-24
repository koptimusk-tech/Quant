"""Factor modules (route B): what each factor needs, and whether the lab has the data to test it.

A factor is only testable when its inputs exist *as they were known at each date*. Momentum and low
volatility are built from prices, which the lab stores with an availability time per bar. Value and Quality
need point-in-time fundamentals (book value, earnings, accruals with their publication dates); the lab has
none, so those modules report themselves unavailable instead of being approximated with today's figures.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.data.store import ParquetStore
from qal.models import Dataset

HOLDOUT_FRACTION = 0.2
FACTOR_FREQUENCIES = ("1d",)
MIN_UNIVERSE = 3
MIN_COMMON_BARS = 1500  # ~6 years of daily bars: 1 year of warm-up plus room for walk-forward and holdout


@dataclass(frozen=True)
class FactorSpec:
    name: str
    title: str
    data: str  # "prices" or "fundamentals_pit"
    universe: str
    variables: list[str]
    formula: str
    availability: str
    rebalance: str
    benchmark: str
    costs: str
    validation: list[str]
    strategy_params: dict[str, Any] = field(default_factory=dict)
    param_grid: dict[str, list[Any]] = field(default_factory=dict)
    premise: str = ""
    mechanism: str = ""
    refutation: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_VALIDATION = [
    "Walk-forward on the research period, embargo >= rebalance horizon, isolated 20% holdout",
    "Net out-of-sample Sharpe >= 0.5 (pre-registered) and mean net return > 0 at 2x costs",
    "Deflated Sharpe probability >= 0.9 counting every variant of the family",
    "Placebo (shuffled positions) p-value <= 0.1; at least half of the test windows positive",
]

FACTORS: dict[str, FactorSpec] = {
    "momentum": FactorSpec(
        name="momentum", title="Cross-sectional momentum", data="prices",
        universe=f"At least {MIN_UNIVERSE} real instruments of one asset class, daily bars, common history "
                 f">= {MIN_COMMON_BARS} bars",
        variables=["log return per bar", "trailing return from t-lookback to t-skip"],
        formula="score_i(t) = sum of log returns of i over (t-lookback, t-skip]; long the top quantile, short "
                "the bottom quantile, equal weights, dollar neutral, gross exposure 1",
        availability="Uses closes up to bar t (available at t's close); trades at the next open",
        rebalance="Every 21 bars", benchmark="cash (the portfolio is dollar neutral)",
        costs="Per-instrument cost model of the contract on every leg and every rebalance",
        validation=_VALIDATION,
        strategy_params={"factor": "momentum", "skip": 21, "rebalance_bars": 21, "quantile": 0.34},
        param_grid={"lookback": [126, 252]},
        premise="Instruments that outperformed their peers over the past six to twelve months tend to keep "
                "outperforming over the next month, because investors under-react to persistent information.",
        mechanism="Slow diffusion of information and flows that chase recent winners keep relative returns "
                  "positively autocorrelated at multi-month horizons; skipping the last month avoids the "
                  "short-term reversal.",
        refutation=["Net OOS Sharpe of the long-short portfolio at or below zero",
                    "Edge comes from one instrument only (per-asset OOS contribution)",
                    "Edge disappears at 2x costs"],
    ),
    "low_vol": FactorSpec(
        name="low_vol", title="Cross-sectional low volatility", data="prices",
        universe=f"At least {MIN_UNIVERSE} real instruments of one asset class, daily bars, common history "
                 f">= {MIN_COMMON_BARS} bars",
        variables=["log return per bar", "trailing standard deviation over vol_window bars"],
        formula="score_i(t) = -std of i's log returns over (t-vol_window, t]; long the least volatile quantile, "
                "short the most volatile, equal weights, dollar neutral",
        availability="Uses closes up to bar t; trades at the next open",
        rebalance="Every 21 bars", benchmark="cash (the portfolio is dollar neutral)",
        costs="Per-instrument cost model of the contract on every leg and every rebalance",
        validation=_VALIDATION,
        strategy_params={"factor": "low_vol", "rebalance_bars": 21, "quantile": 0.34},
        param_grid={"vol_window": [63, 126]},
        premise="Less volatile instruments have delivered higher risk-adjusted returns than more volatile ones, "
                "because leverage-constrained investors bid up high-volatility assets.",
        mechanism="Investors who cannot lever buy volatile assets to reach return targets, which lowers their "
                  "future returns relative to calm assets; in FX the effect may instead reflect carry.",
        refutation=["Net OOS Sharpe at or below zero", "Returns explained by one instrument or one regime",
                    "Edge disappears at 2x costs"],
    ),
    "value": FactorSpec(
        name="value", title="Value", data="fundamentals_pit",
        universe="Equities with point-in-time book value and market capitalisation (or PPP / real exchange "
                 "rates for FX value)",
        variables=["book-to-market as published", "publication date of each filing"],
        formula="score_i(t) = book value last published before t / market value at t; long cheap, short "
                "expensive",
        availability="Fundamentals usable only from their publication date (plus a reporting lag)",
        rebalance="Monthly", benchmark="equal-weight universe", costs="Per-instrument cost model",
        validation=_VALIDATION,
    ),
    "quality": FactorSpec(
        name="quality", title="Quality", data="fundamentals_pit",
        universe="Equities with point-in-time profitability, leverage and accruals",
        variables=["gross profitability", "leverage", "accruals", "publication dates"],
        formula="score_i(t) = z(profitability) - z(leverage) - z(accruals) from filings published before t",
        availability="Fundamentals usable only from their publication date (plus a reporting lag)",
        rebalance="Monthly", benchmark="equal-weight universe", costs="Per-instrument cost model",
        validation=_VALIDATION,
    ),
}

FUNDAMENTALS_GAP = ("No point-in-time fundamentals are stored in the lab. Using today's figures for past dates "
                    "would leak future information, so this factor is not tested.")


def factor_status(session: Session, include_synthetic: bool = False) -> list[dict[str, Any]]:
    """Every factor module with the universes that can test it, or the reason none can."""
    universes = price_universes(session, include_synthetic)
    out = []
    for f in FACTORS.values():
        if f.data != "prices":
            out.append({**f.as_dict(), "available": False, "reason": FUNDAMENTALS_GAP, "universes": []})
            continue
        reason = "" if universes else (f"Needs {MIN_UNIVERSE}+ real daily datasets of one asset class with "
                                       f">= {MIN_COMMON_BARS} common bars; the lab has none")
        out.append({**f.as_dict(), "available": bool(universes), "reason": reason,
                    "universes": [{k: v for k, v in u.items() if k != "datasets"} for u in universes]})
    return out


def price_universes(session: Session, include_synthetic: bool = False) -> list[dict[str, Any]]:
    """Groups of latest datasets (same frequency, asset class and synthetic flag) with enough common bars.

    Only metadata and timestamps are read; the holdout start is placed at 80% of the *common* bars so every
    instrument shares the same research period.
    """
    q = select(Dataset).where(Dataset.is_latest.is_(True), Dataset.frequency.in_(FACTOR_FREQUENCIES),
                              Dataset.quality_status != "fail")
    if not include_synthetic:
        q = q.where(Dataset.is_synthetic.is_(False))
    groups: dict[tuple[str, str, bool], list[Dataset]] = {}
    for d in session.scalars(q.order_by(Dataset.id)).all():
        groups.setdefault((d.frequency, d.instrument.asset_class, bool(d.is_synthetic)), []).append(d)
    out = []
    for (freq, ac, syn), ds in sorted(groups.items()):
        ds = sorted({d.instrument_id: d for d in ds}.values(), key=lambda d: d.instrument.symbol)
        if len(ds) < MIN_UNIVERSE:
            continue
        common: pd.Index | None = None
        for d in ds:
            try:
                ts = pd.Index(ParquetStore.read(d.path)["ts"])
            except OSError:
                continue
            common = ts if common is None else common.intersection(ts)
        if common is None or len(common) < MIN_COMMON_BARS:
            continue
        common = common.sort_values()
        out.append({"frequency": freq, "asset_class": ac, "is_synthetic": syn, "datasets": ds,
                    "dataset_ids": [d.id for d in ds], "symbols": [d.instrument.symbol for d in ds],
                    "common_bars": len(common), "start": str(common[0]), "end": str(common[-1]),
                    "holdout_start": pd.Timestamp(common[int(len(common) * (1 - HOLDOUT_FRACTION))]).isoformat()})
    return out


def design_for(universe: dict[str, Any], horizon: int) -> dict[str, Any]:
    research = int(universe["common_bars"] * (1 - HOLDOUT_FRACTION))
    train = int(min(max(research * 0.4, 300), 2000))
    test = int(min(max(research * 0.1, 63), 500))
    return {"train_bars": train, "test_bars": test, "embargo_bars": max(horizon, 1),
            "holdout_start": universe["holdout_start"]}
