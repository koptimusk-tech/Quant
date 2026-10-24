"""Feature catalog: every feature is documented, causal, and handles missing data and bar frequency."""

import json

import numpy as np
import pandas as pd
import pytest

from qal.features.catalog import DEFAULT_FEATURES, FEATURES, build, usable

ALL = list(FEATURES)


def _hourly(df: pd.DataFrame) -> pd.DataFrame:
    h = df.copy()
    ts = pd.date_range("2024-01-01", periods=len(h), freq="h", tz="UTC")
    h["ts"], h["event_ts"], h["available_ts"] = ts, ts + pd.Timedelta(hours=1), ts + pd.Timedelta(hours=1)
    h["frequency"] = "1h"
    return h


def test_every_feature_is_documented():
    for f in FEATURES.values():
        d = f.as_dict()
        json.dumps(d)  # served by the API as is
        for k in ("formula", "source", "window", "availability", "missing", "assumptions", "biases"):
            assert d[k].strip(), (f.name, k)
    assert set(DEFAULT_FEATURES) <= set(FEATURES)


@pytest.mark.parametrize("cut", [300, 777, 1200])
def test_features_never_use_future_bars(syn, cut):
    df = _hourly(syn["SYN-C"])
    full = build(df, ALL)
    part = build(df.iloc[:cut].copy(), ALL)
    pd.testing.assert_frame_equal(full.iloc[:cut].reset_index(drop=True), part)


def test_poisoned_future_leaves_past_features_unchanged(syn):
    df = syn["SYN-A"].copy()
    cut = 1000
    poisoned = df.copy()
    for c in ("open", "high", "low", "close"):
        poisoned.loc[cut:, c] = df.loc[cut:, c] * 3.0
    poisoned.loc[cut:, "volume"] = df.loc[cut:, "volume"] * 40
    a, b = build(df, ALL), build(poisoned, ALL)
    pd.testing.assert_frame_equal(a.iloc[:cut], b.iloc[:cut])
    assert not a.iloc[cut:].equals(b.iloc[cut:])


def test_missing_prices_give_nan_locally_not_errors(syn):
    df = syn["SYN-D"].copy()
    df.loc[700:702, "close"] = np.nan
    X = build(df, ALL)
    assert X.loc[700:702, "ret_1"].isna().all()
    assert np.isfinite(X.loc[1400, list(DEFAULT_FEATURES)]).all()  # recovers once the windows pass the gap
    assert not np.isinf(X.to_numpy()).any()


def test_unusable_volume_and_calendar_features_are_dropped_with_a_reason(syn):
    df = syn["SYN-D"].copy()
    keep, dropped = usable(df, ["ret_1", "rel_volume", "hour_sin"])
    assert keep == ["ret_1", "rel_volume"] and "hour_sin" in dropped  # daily bars: no hour of day
    df["volume"] = np.where(np.arange(len(df)) % 10 == 0, df["volume"], np.nan)  # 10% coverage, like MT5 tick volume
    keep, dropped = usable(df, ["ret_1", "rel_volume"])
    assert keep == ["ret_1"] and "volume" in dropped["rel_volume"]
    keep, _ = usable(_hourly(syn["SYN-D"]), ["hour_sin", "hour_cos"])
    assert keep == ["hour_sin", "hour_cos"]


def test_unknown_feature_is_a_configuration_error(syn):
    with pytest.raises(ValueError, match="unknown feature"):
        usable(syn["SYN-D"], ["ret_1", "future_return"])
