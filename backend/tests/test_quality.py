from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from qal.data.quality import check_bars
from qal.data.schema import InstrumentSpec, normalize_bars


def _raw(n=30, start="2024-01-01", freq="B"):
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    close = 100 + np.arange(n, dtype=float)
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close,
                         "volume": 1000.0}, index=idx)


def _norm(raw, asset="equity", freq="1d", delay=timedelta(0)):
    return normalize_bars(raw, provider="t", spec=InstrumentSpec("X", asset), frequency=freq,
                          availability_delay=delay, is_synthetic=False, source_ref="t")


def test_clean_data_passes_and_clocks_are_ordered():
    df, norm = _norm(_raw(), delay=timedelta(minutes=15))
    assert (df["event_ts"] == df["ts"] + pd.Timedelta(days=1)).all()
    assert (df["available_ts"] == df["event_ts"] + pd.Timedelta(minutes=15)).all()
    rep = check_bars(df, frequency="1d", asset_class="equity", normalization=norm, now=datetime(2024, 3, 1, tzinfo=UTC))
    assert rep.status == "pass", rep.issues
    assert rep.metrics["coverage"] == 1.0


def test_inconsistent_ohlc_and_non_positive_prices_fail():
    raw = _raw()
    raw.iloc[3, raw.columns.get_loc("high")] = 50  # high below close
    raw.iloc[4, raw.columns.get_loc("low")] = -1
    df, norm = _norm(raw)
    rep = check_bars(df, frequency="1d", asset_class="equity", normalization=norm)
    checks = {i.check: i.severity for i in rep.issues}
    assert checks["non_positive_price"] == "fail"
    assert rep.status == "fail"


def test_conflicting_duplicates_fail_exact_duplicates_dropped():
    raw = _raw(10)
    dup_exact = raw.iloc[[2]]
    dup_conf = raw.iloc[[3]].assign(close=999.0, high=1000.0)
    df, norm = _norm(pd.concat([raw, dup_exact, dup_conf]))
    assert norm["dropped_exact_duplicates"] == 1
    rep = check_bars(df, frequency="1d", asset_class="equity", normalization=norm)
    assert {i.check for i in rep.issues} >= {"conflicting_duplicates", "exact_duplicates"}
    assert rep.status == "fail"


def test_gaps_detected_weekday_calendar_vs_crypto():
    raw = _raw(60).drop(index=_raw(60).index[20:28])  # 8 missing business days
    df, norm = _norm(raw)
    rep = check_bars(df, frequency="1d", asset_class="equity", normalization=norm)
    assert rep.metrics["missing_bars"] == 8
    assert any(i.check == "long_gaps" for i in rep.issues)
    # Weekends are expected for crypto: business-day data is mostly "missing".
    df2, n2 = _norm(_raw(60), asset="crypto")
    rep2 = check_bars(df2, frequency="1d", asset_class="crypto", normalization=n2)
    assert rep2.metrics["missing_bars"] > 0


def test_availability_before_event_fails():
    df, norm = _norm(_raw())
    df.loc[5, "available_ts"] = df.loc[5, "event_ts"] - pd.Timedelta(hours=1)
    rep = check_bars(df, frequency="1d", asset_class="equity", normalization=norm)
    assert any(i.check == "availability" and i.severity == "fail" for i in rep.issues)


def test_missing_volume_is_reported_not_invented():
    raw = _raw().drop(columns=["volume"])
    df, norm = _norm(raw, asset="fx")
    assert df["volume"].isna().all()
    rep = check_bars(df, frequency="1d", asset_class="fx", normalization=norm)
    assert any(i.check == "volume_missing" for i in rep.issues)
