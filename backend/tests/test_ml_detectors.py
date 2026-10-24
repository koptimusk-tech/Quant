"""Isolation Forest and LOF: causal walk-forward fits, known synthetic anomalies, calibrated false alarms."""

import numpy as np
import pytest

from qal.anomaly.detectors import DETECTORS
from qal.data.adapters.synthetic import injected_events
from tests.conftest import synthetic_frame

ML = ["isolation_forest", "local_outlier_factor"]
FAST = {"isolation_forest": {"n_estimators": 40}, "local_outlier_factor": {}}


def _sig(dets):
    return [(d.event_ts, round(d.statistic, 9), round(d.empirical_pvalue, 12)) for d in dets]


def _flagged(df, out):
    pos = {t: i for i, t in enumerate(df["event_ts"])}
    return sorted(pos[d.event_ts] for d in out.detections)


def test_isolation_forest_finds_the_injected_shock_and_volatility_regime(syn):
    ev = {(e.symbol, e.kind): e.bar_index for e in injected_events()}
    shock = ev[("SYN-A", "return_shock")]  # the later shock; the first one falls in the warm-up
    hits = _flagged(syn["SYN-A"], DETECTORS["isolation_forest"]().run(syn["SYN-A"]))
    assert any(shock <= i <= shock + 3 for i in hits)
    start = ev[("SYN-C", "volatility_regime")]
    hits = _flagged(syn["SYN-C"], DETECTORS["isolation_forest"]().run(syn["SYN-C"]))
    assert sum(start <= i < start + 80 for i in hits) >= 3


@pytest.mark.parametrize(("name", "max_ratio"), [("isolation_forest", 1.6), ("local_outlier_factor", 3.0)])
def test_false_alarm_rate_on_series_without_anomalies(name, max_ratio):
    """Control series (no injected events, two seeds): the flagged share stays near ``contamination``. LOF runs
    about twice the nominal rate because its density estimate drifts with the rolling features (documented)."""
    det = DETECTORS[name]()
    flagged = scored = 0
    for seed in (1, 2):
        out = det.run(synthetic_frame("SYN-D", n_bars=3000, seed=seed))
        p = out.pvalues[np.isfinite(out.pvalues)]
        assert ((p > 0) & (p <= 1)).all()
        flagged, scored = flagged + len(out.detections), scored + len(p)
    assert scored > 4000
    assert flagged / scored <= max_ratio * det.params["contamination"]


@pytest.mark.parametrize("name", ML)
@pytest.mark.parametrize("cut", [1000, 1302])
def test_truncation_invariance(syn, name, cut):
    df = syn["SYN-C"]
    full = DETECTORS[name](**FAST[name]).run(df).detections
    part = DETECTORS[name](**FAST[name]).run(df.iloc[:cut].copy()).detections
    end = df["event_ts"].iloc[cut - 1]
    assert _sig([d for d in full if d.event_ts <= end]) == _sig(part)


@pytest.mark.parametrize("name", ML)
def test_poisoned_future_does_not_leak(syn, name):
    df = syn["SYN-A"].copy()
    cut = 1100
    poisoned = df.copy()
    rng = np.random.default_rng(1)
    factor = np.exp(np.cumsum(rng.normal(0, 0.1, len(df) - cut)))
    for c in ("open", "high", "low", "close"):
        poisoned.loc[cut:, c] = df.loc[cut:, c].to_numpy() * factor
    end = df["event_ts"].iloc[cut - 1]
    a = [d for d in DETECTORS[name](**FAST[name]).run(df).detections if d.event_ts <= end]
    b = [d for d in DETECTORS[name](**FAST[name]).run(poisoned).detections if d.event_ts <= end]
    assert _sig(a) == _sig(b)


def test_same_seed_reproduces_and_seed_is_a_parameter(syn):
    df = syn["SYN-C"]
    a = DETECTORS["isolation_forest"](seed=3, **FAST["isolation_forest"]).run(df)
    b = DETECTORS["isolation_forest"](seed=3, **FAST["isolation_forest"]).run(df.copy())
    assert _sig(a.detections) == _sig(b.detections)
    np.testing.assert_array_equal(a.pvalues, b.pvalues)
    c = DETECTORS["isolation_forest"](seed=4, **FAST["isolation_forest"]).run(df)
    assert not np.array_equal(a.pvalues, c.pvalues, equal_nan=True)


def test_detections_explain_themselves(syn):
    out = DETECTORS["isolation_forest"]().run(syn["SYN-A"])
    d = out.detections[0]
    assert d.anomaly_type == "multivariate_outlier"
    assert len(d.metrics["top_features"]) == 3 and d.metrics["features"]
    assert any("regime" in e for e in d.alternative_explanations)
    assert d.empirical_pvalue <= d.metrics["contamination"]


@pytest.mark.parametrize("params", [
    {"contamination": 0.5}, {"contamination": 0}, {"min_train": 10}, {"train_window": 200, "min_train": 300},
    {"scaling": "minmax"}, {"refit_every": -1}, {"features": []}, {"features": ["ret_1", "tomorrow"]},
    {"n_trees": 10},
])
def test_configuration_errors_are_rejected(params):
    with pytest.raises(ValueError):
        DETECTORS["isolation_forest"](**params)


def test_not_applicable_when_data_is_too_short_or_features_unusable(syn):
    out = DETECTORS["isolation_forest"]().run(syn["SYN-D"].iloc[:400].copy())
    assert not out.applicable and "complete feature rows" in out.reason
    out = DETECTORS["isolation_forest"](features=["ret_1", "hour_sin"]).run(syn["SYN-D"])
    assert not out.applicable and "hour_sin" in out.reason


def test_missing_bars_are_skipped_not_fatal(syn):
    df = syn["SYN-D"].copy()
    df.loc[1000:1004, "close"] = np.nan
    out = DETECTORS["isolation_forest"](**FAST["isolation_forest"]).run(df)
    assert out.applicable and np.isfinite(out.pvalues).sum() > 300
    assert all(d.event_ts not in set(df["event_ts"].iloc[1000:1005]) for d in out.detections)
