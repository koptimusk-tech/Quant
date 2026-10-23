import numpy as np
import pandas as pd
import pytest

from qal.anomaly.detectors import DETECTORS
from qal.anomaly.engine import benjamini_hochberg
from qal.data.adapters.synthetic import injected_events
from qal.features.basic import trailing_robust


def _ts(df, i):
    return df["event_ts"].iloc[i]


def test_trailing_robust_excludes_current_bar():
    x = np.r_[np.zeros(10) + np.arange(10) * 0.1, 100.0]
    med, scale, p = trailing_robust(x, 10)
    assert med[10] == pytest.approx(np.median(x[:10]))
    assert p[10] == pytest.approx(1 / 11)  # nothing in the window is as extreme
    assert np.isnan(med[:10]).all()


@pytest.mark.parametrize("ev", [e for e in injected_events() if e.kind == "return_shock"], ids=str)
def test_return_shock_recovers_injected_events(syn, ev):
    df = syn[ev.symbol]
    out = DETECTORS["return_shock"]().run(df)
    assert _ts(df, ev.bar_index) in {d.event_ts for d in out.detections}


def test_volume_spike_recovered(syn):
    ev = next(e for e in injected_events() if e.kind == "volume_spike")
    out = DETECTORS["volume_anomaly"]().run(syn[ev.symbol])
    hit = [d for d in out.detections if d.event_ts == _ts(syn[ev.symbol], ev.bar_index)]
    assert hit and hit[0].direction == 1 and hit[0].metrics["volume_ratio"] > 5


def test_volatility_regime_onset_detected_with_bounded_lag(syn):
    ev = next(e for e in injected_events() if e.kind == "volatility_regime")
    df = syn[ev.symbol]
    out = DETECTORS["volatility_regime"]().run(df)
    start = _ts(df, ev.bar_index)
    ups = [d for d in out.detections if d.direction == 1 and start <= d.event_ts <= _ts(df, ev.bar_index + 20)]
    assert ups, "volatility tripling should be flagged within one short window"


def test_pair_divergence_recovered_and_reports_cointegration(syn):
    ev = next(e for e in injected_events() if e.kind == "pair_divergence")
    out = DETECTORS["pair_divergence"]().run(syn["SYN-B"], syn["SYN-A"])
    hits = [d for d in out.detections if _ts(syn["SYN-B"], ev.bar_index) <= d.event_ts <= _ts(syn["SYN-B"], ev.bar_index + 2)]
    assert hits
    assert "engle_granger_pvalue_window" in hits[0].metrics


def test_control_series_has_no_return_shocks(syn):
    assert DETECTORS["return_shock"]().run(syn["SYN-D"]).detections == []


def test_volume_detector_not_applicable_without_volume(syn):
    df = syn["SYN-A"].assign(volume=np.nan)
    out = DETECTORS["volume_anomaly"]().run(df)
    assert not out.applicable and out.detections == []


def test_unknown_parameter_rejected():
    with pytest.raises(ValueError):
        DETECTORS["return_shock"](windw=10)


def test_benjamini_hochberg_matches_reference():
    p = np.array([0.01, 0.04, 0.03, 0.005, 0.5])
    # Reference values computed by hand: sorted p * m / rank, then cumulative min from the top.
    expected = np.array([0.025, 0.05, 0.05, 0.025, 0.5])
    assert np.allclose(benjamini_hochberg(p), expected)


def test_detections_are_deterministic(syn):
    a = DETECTORS["return_shock"]().run(syn["SYN-C"]).detections
    b = DETECTORS["return_shock"]().run(syn["SYN-C"].copy()).detections
    assert [(d.event_ts, d.statistic) for d in a] == [(d.event_ts, d.statistic) for d in b]
