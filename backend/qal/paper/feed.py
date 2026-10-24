"""Market data feed for paper sessions: historical replay or new data, always with provenance.

* ``replay``: bars of a fixed dataset snapshot (content hash recorded), processed one at a time in the order
  they became available. It is a reproduction of the past, not real-time trading, and is labelled so.
* ``new_data``: the latest snapshot of each instrument, re-read at every step. Only bars that become
  available after the session started are traded; ingesting new data (manually or with a schedule) is what
  moves the session forward.

Synthetic data is never mixed silently: a session over synthetic datasets carries ``is_synthetic``.

Each incoming bar is checked before anything uses it: timestamp order, duplicates, gaps, invalid prices,
availability before completion, publication delay and, for new data, freshness and provider errors.
A ``fail`` pauses the session before the bar is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from qal.data.ingest import latest_dataset
from qal.data.quality import CONTINUOUS
from qal.data.schema import bar_delta
from qal.data.store import ParquetStore
from qal.models import Dataset, Provider


@dataclass
class Instr:
    symbol: str
    dataset_id: str
    instrument_id: str
    provider_id: str
    asset_class: str
    currency: str | None
    frequency: str
    metadata: dict[str, Any]
    is_synthetic: bool
    content_hash: str
    quality_status: str


@dataclass
class FeedCheck:
    status: str = "pass"  # pass, warn, fail
    issues: list[dict[str, Any]] = field(default_factory=list)

    def add(self, check: str, severity: str, message: str, **details: Any) -> None:
        self.issues.append({"check": check, "severity": severity, "message": message, **details})
        rank = {"pass": 0, "warn": 1, "fail": 2}
        if rank[severity] > rank[self.status]:
            self.status = severity


def instruments_for(session: Session, dataset_ids: list[str]) -> list[Instr]:
    out = []
    for ds_id in dataset_ids:
        ds = session.get(Dataset, ds_id)
        if ds is None:
            raise ValueError(f"dataset {ds_id} not found")
        inst = ds.instrument
        out.append(Instr(inst.symbol, ds.id, inst.id, ds.provider_id, inst.asset_class, inst.currency, ds.frequency,
                         inst.metadata_json or {}, bool(ds.is_synthetic), ds.content_hash, ds.quality_status))
    return out


def load(session: Session, instr: Instr, mode: str) -> tuple[pd.DataFrame, Instr]:
    """Bars of an instrument. In new-data mode the latest snapshot replaces the recorded one."""
    ds = session.get(Dataset, instr.dataset_id)
    if mode == "new_data":
        latest = latest_dataset(session, ds.instrument_id, ds.frequency)
        if latest is not None and latest.id != ds.id:
            ds = latest
            instr = Instr(instr.symbol, ds.id, instr.instrument_id, instr.provider_id, instr.asset_class,
                          instr.currency, instr.frequency, instr.metadata, bool(ds.is_synthetic), ds.content_hash,
                          ds.quality_status)
    df = ParquetStore.read(ds.path).sort_values("ts").reset_index(drop=True)
    return df, instr


def expected_bars_between(prev_ts: pd.Timestamp, ts: pd.Timestamp, frequency: str, asset_class: str) -> int:
    """Bars the calendar expects strictly between two bar starts (weekdays only for non-continuous markets)."""
    rng = pd.date_range(prev_ts, ts, freq=bar_delta(frequency))[1:-1]
    if asset_class not in CONTINUOUS and frequency != "1wk":
        rng = rng[rng.day_of_week < 5]
    return len(rng)


def check_bar(rows: pd.DataFrame, prev: pd.Series | None, instr: Instr, *, mode: str, now: datetime | None,
              max_gap_bars: int, max_delay_hours: float, max_staleness_hours: float,
              provider: Provider | None = None) -> FeedCheck:
    """Check the bar(s) of one instrument at one timestamp before the session uses them."""
    chk = FeedCheck()
    if len(rows) > 1:
        same = rows[["open", "high", "low", "close"]].nunique().max() == 1
        chk.add("duplicate", "warn" if same else "fail",
                "duplicate bar" if same else "several different bars share the same timestamp", rows=len(rows))
    r = rows.iloc[-1]
    px = np.array([r["open"], r["high"], r["low"], r["close"]], dtype=float)
    if not np.isfinite(px).all() or (px <= 0).any():
        chk.add("invalid_price", "fail", "missing or non-positive price")
    elif r["high"] < max(r["open"], r["close"]) - 1e-12 or r["low"] > min(r["open"], r["close"]) + 1e-12:
        chk.add("invalid_price", "fail", "high/low do not bracket open/close")
    if pd.isna(r["available_ts"]) or r["available_ts"] < r["event_ts"]:
        chk.add("availability", "fail", "available before the bar was complete: not point-in-time")
    else:
        delay_h = (r["available_ts"] - r["event_ts"]).total_seconds() / 3600
        if delay_h > max_delay_hours:
            chk.add("delay", "warn", f"published {delay_h:.1f} h after the bar closed", hours=round(delay_h, 2))
    if prev is not None:
        if r["ts"] <= prev["ts"]:
            chk.add("timestamp_order", "fail", "bar is not later than the previous one")
        else:
            missing = expected_bars_between(prev["ts"], r["ts"], instr.frequency, instr.asset_class)
            if missing > max_gap_bars:
                chk.add("gap", "fail", f"{missing} expected bars missing before this one", bars=missing)
            elif missing > 0:
                chk.add("gap", "warn", f"{missing} expected bar(s) missing (holiday or missing data)", bars=missing)
    if mode == "new_data" and now is not None:
        age_h = (now - pd.Timestamp(r["available_ts"]).to_pydatetime()).total_seconds() / 3600
        if age_h > max_staleness_hours:
            chk.add("freshness", "warn", f"latest bar is {age_h:.0f} h old", hours=round(age_h, 1))
        if provider is not None and provider.last_error_at and (
                provider.last_success_at is None or provider.last_error_at > provider.last_success_at):
            chk.add("provider", "warn", f"provider {provider.id} reported an error: {provider.last_error}")
    return chk


def stale_feed_check(last_available: pd.Timestamp, now: datetime, max_staleness_hours: float) -> FeedCheck:
    """New-data mode with nothing new: is the feed disconnected or just waiting for the next bar?"""
    chk = FeedCheck()
    age_h = (now - last_available.to_pydatetime()).total_seconds() / 3600
    if age_h > max_staleness_hours:
        chk.add("freshness", "warn", f"no new bar for {age_h:.0f} h: feed may be disconnected", hours=round(age_h, 1))
    return chk


def utcnow() -> datetime:
    return datetime.now(UTC)
