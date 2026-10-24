"""Documented feature catalog for the multivariate models.

Every feature value at bar t uses bars <= t only (the bar's own close included), so it is available at
the bar's ``available_ts``; ``tests/test_features.py`` checks that appending future bars never changes past
values. Each entry states its formula, source columns, window, availability, how missing values are handled,
its assumptions and the biases to keep in mind. Features are *not* blindly made stationary: returns, ratios
and z-scores are used because the models compare a bar with a recent past, not with the whole history.

Models standardise features using statistics of their training window only (see ``qal.anomaly.ml``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from qal.features.basic import log_returns


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    group: str
    formula: str
    source: str
    window: str
    availability: str
    missing: str
    assumptions: str
    biases: str
    needs: tuple[str, ...] = ()  # columns or data that must be usable
    fn: Callable[[pd.DataFrame], pd.Series] | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("fn")
        d["needs"] = list(self.needs)
        return d


AT_CLOSE = "At the bar's close (available_ts): uses bars <= t"
WARMUP_NAN = ("NaN until 80% of the window is observed; missing bars inside a window are skipped if at least "
              "80% remain; rows with any NaN feature are not scored")


MIN_OBS = 0.8  # a rolling statistic needs this share of its window, so a few missing bars do not blank it


def _roll(x: pd.Series, w: int):
    return x.rolling(w, min_periods=max(2, int(np.ceil(MIN_OBS * w))))


def _r(df: pd.DataFrame) -> pd.Series:
    return log_returns(df).reset_index(drop=True)


def _ret_h(h: int) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda df: _r(df).rolling(h).sum()


def _rv(w: int) -> Callable[[pd.DataFrame], pd.Series]:
    return lambda df: _roll(_r(df), w).std()


def _rel_vol(df: pd.DataFrame) -> pd.Series:
    r = _r(df)
    return np.log(_roll(r, 20).std() / _roll(r, 250).std())


def _robust_z(df: pd.DataFrame) -> pd.Series:
    r = _r(df)
    med = _roll(r.shift(1), 250).median()
    mad = _roll((r.shift(1) - med).abs(), 250).median() * 1.4826
    return (r - med) / mad.replace(0, np.nan)


def _range_rel(df: pd.DataFrame) -> pd.Series:
    rng = np.log(df["high"] / df["low"]).reset_index(drop=True)
    return np.log(rng / _roll(rng.shift(1), 60).median()).replace([np.inf, -np.inf], np.nan)


def _gap(df: pd.DataFrame) -> pd.Series:
    o, c = df["open"].reset_index(drop=True), df["close"].reset_index(drop=True)
    return np.log(o / c.shift(1))


def _skew(df: pd.DataFrame) -> pd.Series:
    return _roll(_r(df), 60).skew()


def _kurt(df: pd.DataFrame) -> pd.Series:
    return _roll(_r(df), 60).kurt()


def _autocorr(df: pd.DataFrame) -> pd.Series:
    r = _r(df)
    return _roll(r, 60).corr(r.shift(1))


def _trend_t(df: pd.DataFrame) -> pd.Series:
    r = _r(df)
    m, s = _roll(r, 60).mean(), _roll(r, 60).std()
    return m / (s / np.sqrt(60))


def _rel_volume(df: pd.DataFrame) -> pd.Series:
    v = df["volume"].reset_index(drop=True).astype(float)
    return np.log(v / _roll(v.shift(1), 60).median()).replace([np.inf, -np.inf], np.nan)


def _hour_sin(df: pd.DataFrame) -> pd.Series:
    h = pd.to_datetime(df["ts"]).dt.hour.reset_index(drop=True)
    return np.sin(2 * np.pi * h / 24)


def _hour_cos(df: pd.DataFrame) -> pd.Series:
    h = pd.to_datetime(df["ts"]).dt.hour.reset_index(drop=True)
    return np.cos(2 * np.pi * h / 24)


FEATURES: dict[str, FeatureSpec] = {f.name: f for f in [
    FeatureSpec("ret_1", "returns", "log(P_t / P_{t-1})", "close (adjusted when available)", "1 bar", AT_CLOSE,
                "NaN on the first bar", "Log returns are additive over time", "Stale or bad ticks show up as large "
                "returns", fn=_ret_h(1)),
    FeatureSpec("ret_5", "returns", "sum of ret_1 over the last 5 bars", "close", "5 bars", AT_CLOSE, WARMUP_NAN,
                "Short-horizon drift", "Overlaps with ret_1 (correlated by construction)", fn=_ret_h(5)),
    FeatureSpec("ret_20", "returns", "sum of ret_1 over the last 20 bars", "close", "20 bars", AT_CLOSE, WARMUP_NAN,
                "Medium-horizon trend", "Overlaps with ret_5", fn=_ret_h(20)),
    FeatureSpec("rv_20", "volatility", "std of ret_1 over the last 20 bars", "close", "20 bars", AT_CLOSE,
                WARMUP_NAN, "Volatility is locally stable over the window", "Includes the current bar's return",
                fn=_rv(20)),
    FeatureSpec("rel_vol", "volatility", "log(rv_20 / rv_250)", "close", "20 and 250 bars", AT_CLOSE, WARMUP_NAN,
                "A ratio above 0 marks a high-volatility regime relative to the past year (daily bars)",
                "The 250-bar baseline lags structural changes", fn=_rel_vol),
    FeatureSpec("ret_robust_z", "robust", "(ret_1 - median) / (1.4826 * MAD) of the previous 250 returns",
                "close", "250 bars (current bar excluded)", AT_CLOSE, WARMUP_NAN,
                "Median/MAD are robust to past outliers", "MAD near 0 on flat series; set to NaN when it is 0",
                fn=_robust_z),
    FeatureSpec("range_rel", "volatility", "log(log(H/L) / median of log(H/L) over the previous 60 bars)",
                "high, low", "60 bars (current bar excluded)", AT_CLOSE, WARMUP_NAN,
                "High/low are traded prices", "Bad ticks inflate the range; zero-range bars give NaN",
                fn=_range_rel),
    FeatureSpec("gap", "returns", "log(open_t / close_{t-1})", "open, close", "1 bar", AT_CLOSE,
                "NaN on the first bar", "The open reflects information arriving between bars",
                "24h FX markets have tiny gaps except after weekends", fn=_gap),
    FeatureSpec("skew_60", "robust", "sample skewness of ret_1 over 60 bars", "close", "60 bars", AT_CLOSE,
                WARMUP_NAN, "Moments exist", "Very noisy with 60 observations", fn=_skew),
    FeatureSpec("kurt_60", "robust", "excess kurtosis of ret_1 over 60 bars", "close", "60 bars", AT_CLOSE,
                WARMUP_NAN, "Moments exist", "Dominated by single outliers", fn=_kurt),
    FeatureSpec("autocorr_60", "persistence", "corr(ret_t, ret_{t-1}) over 60 bars", "close", "60 bars", AT_CLOSE,
                WARMUP_NAN, "Linear dependence", "Bid-ask bounce induces negative values intraday", fn=_autocorr),
    FeatureSpec("trend_t_60", "persistence", "mean(ret_1) / (std(ret_1) / sqrt(60)) over 60 bars", "close",
                "60 bars", AT_CLOSE, WARMUP_NAN, "Independent returns (t-statistic of the drift)",
                "Overstated under autocorrelation; descriptive only", fn=_trend_t),
    FeatureSpec("rel_volume", "liquidity", "log(volume_t / median volume of the previous 60 bars)", "volume",
                "60 bars (current bar excluded)", AT_CLOSE, WARMUP_NAN + "; zero volume gives NaN",
                "Volume measures traded size", "FX volume from MT5 is tick volume (number of quotes), not size; "
                "excluded when volume coverage is below 80%", needs=("volume",), fn=_rel_volume),
    FeatureSpec("hour_sin", "calendar", "sin(2*pi*hour/24) of the bar start (UTC)", "ts", "none",
                "Known in advance", "Always defined", "Session effects repeat daily", "Daylight-saving shifts move "
                "sessions by one hour; only meaningful for intraday bars", needs=("intraday",), fn=_hour_sin),
    FeatureSpec("hour_cos", "calendar", "cos(2*pi*hour/24) of the bar start (UTC)", "ts", "none",
                "Known in advance", "Always defined", "Session effects repeat daily", "As hour_sin",
                needs=("intraday",), fn=_hour_cos),
]}

DEFAULT_FEATURES = ("ret_1", "ret_5", "rel_vol", "ret_robust_z", "range_rel", "gap", "kurt_60", "autocorr_60")
INTRADAY = ("1m", "5m", "15m", "30m", "1h", "4h")


def volume_usable(df: pd.DataFrame) -> bool:
    v = df["volume"]
    return bool(v.notna().mean() >= 0.8 and (v > 0).mean() >= 0.8 and v.nunique() > 10)


def usable(df: pd.DataFrame, names: list[str] | tuple[str, ...]) -> tuple[list[str], dict[str, str]]:
    """Requested features that this dataset supports, and the reason for each one dropped."""
    keep, dropped = [], {}
    intraday = str(df["frequency"].iloc[0]) in INTRADAY if "frequency" in df else False
    for n in names:
        if n not in FEATURES:
            raise ValueError(f"unknown feature {n!r}; available: {list(FEATURES)}")
        needs = FEATURES[n].needs
        if "volume" in needs and not volume_usable(df):
            dropped[n] = "volume coverage below 80% or constant volume"
        elif "intraday" in needs and not intraday:
            dropped[n] = "calendar hour is only meaningful for intraday bars"
        else:
            keep.append(n)
    return keep, dropped


def build(df: pd.DataFrame, names: list[str] | tuple[str, ...]) -> pd.DataFrame:
    """Feature matrix aligned with ``df`` rows (positional index)."""
    out = pd.DataFrame({n: FEATURES[n].fn(df).to_numpy(dtype=float) for n in names})
    return out.replace([np.inf, -np.inf], np.nan)
