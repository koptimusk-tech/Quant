"""Data quality checks (Gate A: data quality).

A dataset whose status is ``fail`` is blocked: detectors refuse to run on it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from qal.data.schema import bar_delta, return_price

# Calendar used to count expected bars. Crypto trades continuously; FX and most
# exchange-traded instruments trade on weekdays (exchange holidays show up as small gaps).
CONTINUOUS = {"crypto"}


@dataclass
class QualityIssue:
    check: str
    severity: str  # info, warn, fail
    message: str
    count: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class QualityReport:
    status: str
    issues: list[QualityIssue]
    metrics: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return {"status": self.status, "metrics": self.metrics, "issues": [asdict(i) for i in self.issues]}


def _expected_index(df: pd.DataFrame, frequency: str, asset_class: str) -> pd.DatetimeIndex:
    start, end = df["ts"].iloc[0], df["ts"].iloc[-1]
    delta = bar_delta(frequency)
    full = pd.date_range(start, end, freq=delta)
    if asset_class in CONTINUOUS or frequency == "1wk":
        return full
    return full[full.day_of_week < 5]


def check_bars(
    df: pd.DataFrame,
    *,
    frequency: str,
    asset_class: str,
    normalization: dict[str, int] | None = None,
    now: datetime | None = None,
    fail_ohlc_share: float = 0.01,
    max_gap_bars_warn: int = 5,
) -> QualityReport:
    issues: list[QualityIssue] = []
    now = now or datetime.now(UTC)
    n = len(df)
    metrics: dict[str, Any] = {"rows": n}
    normalization = normalization or {}

    if normalization.get("dropped_nan_price"):
        issues.append(QualityIssue("nan_prices", "warn", "Rows with missing prices were dropped",
                                   normalization["dropped_nan_price"]))
    if normalization.get("dropped_exact_duplicates"):
        issues.append(QualityIssue("exact_duplicates", "info", "Exact duplicate rows were dropped",
                                   normalization["dropped_exact_duplicates"]))
    if n == 0:
        issues.append(QualityIssue("empty", "fail", "Dataset has no rows"))
        return QualityReport("fail", issues, metrics)

    dup = int(df["ts"].duplicated().sum())
    if dup:
        issues.append(QualityIssue("conflicting_duplicates", "fail",
                                   "Several different bars share the same timestamp", dup))

    px = df[["open", "high", "low", "close"]]
    nonpos = int((px <= 0).any(axis=1).sum())
    if nonpos:
        issues.append(QualityIssue("non_positive_price", "fail", "Prices <= 0", nonpos))

    bad_ohlc = (df["high"] < df[["open", "close"]].max(axis=1) - 1e-12) | (
        df["low"] > df[["open", "close"]].min(axis=1) + 1e-12
    ) | (df["high"] < df["low"])
    n_bad = int(bad_ohlc.sum())
    if n_bad:
        sev = "fail" if n_bad / n > fail_ohlc_share else "warn"
        issues.append(QualityIssue("ohlc_inconsistent", sev, "High/low do not bracket open/close", n_bad,
                                   {"first": [str(t) for t in df.loc[bad_ohlc, "ts"].head(5)]}))

    vol = df["volume"]
    if vol.isna().all():
        issues.append(QualityIssue("volume_missing", "info", "No volume field; volume detectors not applicable"))
        metrics["volume_coverage"] = 0.0
    else:
        neg = int((vol < 0).sum())
        if neg:
            issues.append(QualityIssue("negative_volume", "fail", "Negative volume", neg))
        zero_share = float((vol.fillna(0) == 0).mean())
        metrics["volume_coverage"] = 1 - zero_share
        if zero_share > 0.2:
            issues.append(QualityIssue("zero_volume", "warn",
                                       f"{zero_share:.0%} of bars have zero/missing volume "
                                       "(common for FX/indices on some providers)", int(zero_share * n)))

    if (df["available_ts"] < df["event_ts"]).any() or df["available_ts"].isna().any():
        issues.append(QualityIssue("availability", "fail",
                                   "available_ts missing or earlier than event_ts: point-in-time not reconstructable"))

    # Coverage and gaps.
    expected = _expected_index(df, frequency, asset_class)
    present = pd.DatetimeIndex(df["ts"])
    if frequency in ("1d", "1wk"):
        present = present.normalize()
        expected = expected.normalize()
    missing = expected.difference(present)
    metrics["expected_bars"] = int(len(expected))
    metrics["coverage"] = float(len(present.intersection(expected)) / len(expected)) if len(expected) else 1.0
    metrics["missing_bars"] = int(len(missing))
    gaps: list[dict[str, Any]] = []
    if len(missing):
        pos = expected.get_indexer(missing)
        run_start = 0
        for i in range(1, len(pos) + 1):
            if i == len(pos) or pos[i] != pos[i - 1] + 1:
                length = i - run_start
                gaps.append({"start": str(missing[run_start]), "end": str(missing[i - 1]), "bars": int(length)})
                run_start = i
    long_gaps = [g for g in gaps if g["bars"] > max_gap_bars_warn]
    metrics["gaps"] = sorted(gaps, key=lambda g: -g["bars"])[:20]
    metrics["n_gaps"] = len(gaps)
    if long_gaps:
        issues.append(QualityIssue("long_gaps", "warn", f"{len(long_gaps)} gaps longer than {max_gap_bars_warn} bars",
                                   len(long_gaps), {"largest": sorted(long_gaps, key=lambda g: -g["bars"])[:5]}))
    elif gaps:
        issues.append(QualityIssue("short_gaps", "info",
                                   f"{len(gaps)} short gaps (holidays or missing bars)", len(gaps)))

    # Corporate actions: raw vs adjusted return disagreement.
    if df["adj_close"].notna().mean() > 0.95:
        raw_r = np.log(df["close"]).diff()
        adj_r = np.log(return_price(df)).diff()
        ca = (raw_r - adj_r).abs() > 0.02
        if ca.any():
            issues.append(QualityIssue("corporate_actions", "info",
                                       "Bars where raw and adjusted returns differ by >2% (splits/dividends); "
                                       "returns use adjusted prices", int(ca.sum()),
                                       {"ts": [str(t) for t in df.loc[ca, "ts"].head(10)]}))

    last_avail = pd.Timestamp(df["available_ts"].iloc[-1]).to_pydatetime()
    age = now - last_avail
    metrics["last_event_ts"] = str(df["event_ts"].iloc[-1])
    metrics["first_ts"] = str(df["ts"].iloc[0])
    metrics["freshness_seconds"] = max(age.total_seconds(), 0.0)
    metrics["stale"] = age > 5 * bar_delta(frequency) + pd.Timedelta(days=3).to_pytimedelta()

    status = "pass"
    if any(i.severity == "warn" for i in issues):
        status = "warn"
    if any(i.severity == "fail" for i in issues):
        status = "fail"
    return QualityReport(status, issues, metrics)
