"""Canonical point-in-time bar schema.

Every bar carries three clocks (see DECISIONS.md D-004):

* ``ts``           bar start, UTC.
* ``event_ts``     instant the bar is complete (bar end). Nothing in the bar is knowable earlier.
* ``available_ts`` instant the data was (or, for history, is conservatively assumed to be)
                   available to a decision maker: ``event_ts`` + the provider's publication delay.
* ``received_ts``  instant *this system* received the row. For history downloaded in bulk it is
                   later than ``available_ts`` and is kept for auditing, not for simulation.

Research code must only use rows whose ``available_ts`` is <= the simulated decision time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

FREQUENCIES: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1wk": timedelta(weeks=1),
}

ASSET_CLASSES = ("equity", "etf", "future", "fx", "crypto", "index", "other")

PRICE_COLUMNS = ["open", "high", "low", "close"]
CANONICAL_COLUMNS = [
    "ts",
    "event_ts",
    "available_ts",
    "received_ts",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "provider",
    "symbol",
    "market",
    "frequency",
    "is_synthetic",
    "source_ref",
]


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    asset_class: str
    market: str = ""
    currency: str | None = None
    timezone: str = "UTC"

    def __post_init__(self) -> None:
        if self.asset_class not in ASSET_CLASSES:
            raise ValueError(f"asset_class must be one of {ASSET_CLASSES}, got {self.asset_class!r}")


def bar_delta(frequency: str) -> timedelta:
    try:
        return FREQUENCIES[frequency]
    except KeyError as exc:
        raise ValueError(f"Unsupported frequency {frequency!r}; use one of {list(FREQUENCIES)}") from exc


def normalize_bars(
    raw: pd.DataFrame,
    *,
    provider: str,
    spec: InstrumentSpec,
    frequency: str,
    availability_delay: timedelta,
    is_synthetic: bool,
    source_ref: str,
    received_ts: datetime | None = None,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Convert a provider frame (index or column ``ts`` + OHLCV[+adj_close]) to the canonical schema.

    Returns the frame and counters of rows dropped during normalisation, so the quality report
    can disclose them instead of silently hiding them.
    """
    delta = bar_delta(frequency)
    df = raw.copy()
    if "ts" not in df.columns:
        df = df.rename_axis("ts").reset_index()
    df.columns = [str(c).lower() for c in df.columns]
    missing = [c for c in ["ts", *PRICE_COLUMNS] if c not in df.columns]
    if missing:
        raise ValueError(f"Provider frame lacks required columns: {missing}")
    ts = pd.to_datetime(df["ts"], utc=True)
    df["ts"] = ts
    if "volume" not in df.columns:
        df["volume"] = np.nan
    if "adj_close" not in df.columns:
        df["adj_close"] = np.nan
    for col in [*PRICE_COLUMNS, "adj_close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")

    counters = {"input_rows": int(len(df)), "dropped_nan_price": 0, "dropped_exact_duplicates": 0}
    nan_mask = df[PRICE_COLUMNS].isna().any(axis=1) | df["ts"].isna()
    counters["dropped_nan_price"] = int(nan_mask.sum())
    df = df.loc[~nan_mask]
    before = len(df)
    df = df.drop_duplicates(subset=["ts", *PRICE_COLUMNS, "volume"])
    counters["dropped_exact_duplicates"] = int(before - len(df))
    df = df.sort_values("ts", kind="stable").reset_index(drop=True)

    received = pd.Timestamp(received_ts or datetime.now(UTC))
    df["event_ts"] = df["ts"] + delta
    df["available_ts"] = df["event_ts"] + availability_delay
    df["received_ts"] = received
    df["provider"] = provider
    df["symbol"] = spec.symbol
    df["market"] = spec.market
    df["frequency"] = frequency
    df["is_synthetic"] = bool(is_synthetic)
    df["source_ref"] = source_ref
    return df[CANONICAL_COLUMNS], counters


def return_price(df: pd.DataFrame) -> pd.Series:
    """Price used for returns: adjusted close where available, else close.

    Ratios of consecutive back-adjusted closes only use the corporate action known on the
    ex-date, so *returns* from ``adj_close`` are point-in-time safe; adjusted *levels* are not
    (they embed future adjustment factors) and must not be treated as tradable prices.
    """
    adj = df["adj_close"]
    if adj.notna().mean() > 0.95:
        return adj.where(adj.notna(), df["close"])
    return df["close"]


def total_return_index(df: pd.DataFrame) -> pd.Series:
    """Point-in-time total-return index rebuilt by compounding returns from the first bar."""
    px = return_price(df)
    r = px.pct_change().fillna(0.0)
    return (1.0 + r).cumprod()
