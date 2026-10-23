"""Acceptance criterion 4 (applied to detection): future bars never change past results."""

import numpy as np
import pytest

from qal.anomaly.detectors import DETECTORS


def _sig(dets):
    return [(d.event_ts, round(d.statistic, 10), d.direction, round(d.empirical_pvalue, 12)) for d in dets]


@pytest.mark.parametrize("name", ["return_shock", "volume_anomaly", "volatility_regime"])
@pytest.mark.parametrize("cut", [700, 1000, 1299])
def test_truncation_invariance_single(syn, name, cut):
    df = syn["SYN-C"] if name != "return_shock" else syn["SYN-A"]
    full = DETECTORS[name]().run(df).detections
    part = DETECTORS[name]().run(df.iloc[:cut].copy()).detections
    end = df["event_ts"].iloc[cut - 1]
    assert _sig([d for d in full if d.event_ts <= end]) == _sig(part)


@pytest.mark.parametrize("name", ["return_shock", "volume_anomaly", "volatility_regime"])
def test_poisoned_future_does_not_leak(syn, name):
    df = syn["SYN-C"].copy()
    cut = 900
    poisoned = df.copy()
    rng = np.random.default_rng(0)
    k = len(df) - cut
    factor = np.exp(np.cumsum(rng.normal(0, 0.2, k)))
    for c in ("open", "high", "low", "close"):
        poisoned.loc[cut:, c] = df.loc[cut:, c].to_numpy() * factor
    poisoned.loc[cut:, "volume"] = df.loc[cut:, "volume"].to_numpy() * 50
    end = df["event_ts"].iloc[cut - 1]
    a = [d for d in DETECTORS[name]().run(df).detections if d.event_ts <= end]
    b = [d for d in DETECTORS[name]().run(poisoned).detections if d.event_ts <= end]
    assert _sig(a) == _sig(b)


@pytest.mark.parametrize("cut", [900, 1302])
def test_truncation_invariance_pair(syn, cut):
    det = DETECTORS["pair_divergence"]
    full = det().run(syn["SYN-B"], syn["SYN-A"]).detections
    part = det().run(syn["SYN-B"].iloc[:cut].copy(), syn["SYN-A"].iloc[:cut].copy()).detections
    end = syn["SYN-B"]["event_ts"].iloc[cut - 1]
    strip = lambda ds: [(d.event_ts, round(d.statistic, 10)) for d in ds]  # noqa: E731
    assert strip([d for d in full if d.event_ts <= end]) == strip(part)
