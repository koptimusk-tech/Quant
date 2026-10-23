"""Rule-based strategies used to test hypotheses.

A strategy maps point-in-time data to *target weights* decided at the close of bar t (when the
bar is available). The backtest engine executes them at the next bar's open. Strategies must be
causal: ``tests/test_backtest.py`` checks that future bars never change past weights.
Weights are fractions of capital per dataset; gross exposure is kept <= 1.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from qal.anomaly.detectors import DETECTORS
from qal.data.schema import total_return_index
from qal.features.basic import log_returns


class Strategy(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    n_assets: ClassVar[int] = 1  # 2 for pair strategies
    defaults: ClassVar[dict[str, Any]]

    def __init__(self, **params: Any):
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"{self.name}: unknown parameters {sorted(unknown)}")
        self.params = {**self.defaults, **params}

    @abstractmethod
    def weights(self, frames: list[pd.DataFrame]) -> pd.DataFrame:
        """Target weights indexed by bar position of the aligned frames (one column per frame)."""


def _hold(signal_dir: np.ndarray, hold: int) -> np.ndarray:
    """Turn event directions (0 = no event) into positions held for ``hold`` bars after each event."""
    out = np.zeros(len(signal_dir))
    remaining, cur = 0, 0.0
    for i, d in enumerate(signal_dir):
        if d != 0:
            cur, remaining = float(d), hold
        if remaining > 0:
            out[i] = cur
            remaining -= 1
        else:
            cur = 0.0
    return out


class EventStrategy(Strategy):
    """Trade after a detector event: ``mode`` reversal (against the move) or continuation (with it)."""

    name = "event"
    description = ("After a detector flags bar t (decision at t's close), hold a position for hold_bars: against the "
                   "event direction (reversal) or with it (continuation).")
    defaults = {"detector": "return_shock", "mode": "reversal", "threshold": 4.0, "hold_bars": 5, "window": 250,
                "direction": "both"}

    def weights(self, frames: list[pd.DataFrame]) -> pd.DataFrame:
        df = frames[0]
        det_cls = DETECTORS[self.params["detector"]]
        if det_cls.pairwise:
            raise ValueError("event strategy uses single-series detectors")
        dp: dict[str, Any] = {"threshold": float(self.params["threshold"])}
        if "window" in det_cls.defaults:
            dp["window"] = int(self.params["window"])
        out = det_cls(**dp).run(df)
        sig = np.zeros(len(df))
        pos = {t: i for i, t in enumerate(df["event_ts"])}
        for d in out.detections:
            if self.params["direction"] == "up" and d.direction < 0:
                continue
            if self.params["direction"] == "down" and d.direction > 0:
                continue
            sig[pos[d.event_ts]] = -d.direction if self.params["mode"] == "reversal" else d.direction
        return pd.DataFrame({0: _hold(sig, int(self.params["hold_bars"]))})


class TimeSeriesMomentum(Strategy):
    name = "ts_momentum"
    description = "Sign of the trailing lookback return (long/short or long/flat), optionally volatility-targeted."
    defaults = {"lookback": 60, "long_only": False, "vol_target": None, "vol_window": 20}

    def weights(self, frames: list[pd.DataFrame]) -> pd.DataFrame:
        df = frames[0]
        r = log_returns(df).fillna(0.0)
        mom = r.rolling(int(self.params["lookback"])).sum()
        w = np.sign(mom).fillna(0.0)
        if self.params["long_only"]:
            w = w.clip(lower=0)
        if self.params["vol_target"]:
            vol = r.rolling(int(self.params["vol_window"])).std()
            w = (w * float(self.params["vol_target"]) / vol).clip(-1, 1).fillna(0.0)
        return pd.DataFrame({0: w.to_numpy()})


class PairMeanReversion(Strategy):
    name = "pair_mean_reversion"
    n_assets = 2
    description = ("Rolling OLS spread of log total-return indices (window ends at t-1). Enter when |z| >= entry_z "
                   "(short the rich leg, long the cheap leg weighted by the hedge ratio), exit when |z| <= exit_z or "
                   "after max_hold bars. Gross exposure 1.")
    defaults = {"window": 120, "entry_z": 2.0, "exit_z": 0.5, "max_hold": 20}

    def weights(self, frames: list[pd.DataFrame]) -> pd.DataFrame:
        a, b = frames
        y, x = np.log(total_return_index(a)), np.log(total_return_index(b))
        w = int(self.params["window"])
        mx, my = x.rolling(w).mean().shift(1), y.rolling(w).mean().shift(1)
        vx = (x * x).rolling(w).mean().shift(1) - mx**2
        beta = ((x * y).rolling(w).mean().shift(1) - mx * my) / vx
        vy = (y * y).rolling(w).mean().shift(1) - my**2
        sd = np.sqrt((vy - beta**2 * vx).clip(lower=0))
        z = ((y - (my - beta * mx) - beta * x) / sd.replace(0, np.nan)).to_numpy()
        bt = beta.to_numpy()
        entry, exitz, max_hold = float(self.params["entry_z"]), float(self.params["exit_z"]), int(self.params["max_hold"])
        wy, wx = np.zeros(len(a)), np.zeros(len(a))
        state, held, hb = 0, 0, 1.0
        for i in range(len(a)):
            zi = z[i]
            if not np.isfinite(zi) or not np.isfinite(bt[i]):
                state = 0
            elif state == 0 and abs(zi) >= entry:
                state, held, hb = -int(np.sign(zi)), 0, bt[i]
            elif state != 0 and (abs(zi) <= exitz or held >= max_hold):
                state = 0
            if state != 0:
                held += 1
                gross = 1 + abs(hb)
                wy[i], wx[i] = state / gross, -state * hb / gross
        return pd.DataFrame({0: wy, 1: wx})


class BuyAndHold(Strategy):
    name = "buy_and_hold"
    description = "Fully invested long. Used as benchmark."
    defaults: dict[str, Any] = {}

    def weights(self, frames: list[pd.DataFrame]) -> pd.DataFrame:
        return pd.DataFrame({0: np.ones(len(frames[0]))})


STRATEGIES: dict[str, type[Strategy]] = {
    s.name: s for s in (EventStrategy, TimeSeriesMomentum, PairMeanReversion, BuyAndHold)
}
