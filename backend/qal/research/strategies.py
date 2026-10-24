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
    n_assets: ClassVar[int] = 1  # 2 for pair strategies, 0 for a cross-sectional universe (3 or more)
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
    description = ("Sign of the trailing lookback return (long/short or long/flat), optionally volatility-targeted "
                   "and optionally active only in one volatility regime (short/long realised volatility ratio).")
    defaults = {"lookback": 60, "long_only": False, "vol_target": None, "vol_window": 20, "regime": "all",
                "regime_short": 20, "regime_long": 250}

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
        regime = self.params["regime"]
        if regime not in ("all", "high_vol", "low_vol"):
            raise ValueError("ts_momentum: regime must be all, high_vol or low_vol")
        if regime != "all":  # trailing volatility ratio known at t's close; NaN during warm-up -> flat
            ratio = r.rolling(int(self.params["regime_short"])).std() / r.rolling(int(self.params["regime_long"])).std()
            on = ratio > 1 if regime == "high_vol" else ratio <= 1
            w = w.where(on, 0.0)
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


class CrossSectionalFactor(Strategy):
    """Long the top and short the bottom of a universe ranked by a price-based factor (dollar neutral).

    Scores use returns up to and including bar t (decided at t's close, executed at t+1's open) and are
    refreshed every ``rebalance_bars``. Factors: ``momentum`` = sum of log returns from t-lookback to
    t-skip; ``low_vol`` = minus the standard deviation of returns over ``vol_window``.
    """

    name = "cross_sectional_factor"
    n_assets = 0  # a universe of 3 or more
    description = ("Rank the universe by a price factor (momentum: trailing return skipping the latest bars; low_vol: "
                   "lowest trailing volatility first); long the top quantile, short the bottom, equal weights, "
                   "gross exposure 1, rebalanced every rebalance_bars.")
    defaults = {"factor": "momentum", "lookback": 252, "skip": 21, "vol_window": 63, "rebalance_bars": 21,
                "quantile": 0.34}

    def weights(self, frames: list[pd.DataFrame]) -> pd.DataFrame:
        if self.params["factor"] not in ("momentum", "low_vol"):
            raise ValueError("cross_sectional_factor: factor must be momentum or low_vol")
        r = pd.concat([log_returns(f).reset_index(drop=True) for f in frames], axis=1).fillna(0.0)
        r.columns = range(len(frames))
        if self.params["factor"] == "momentum":
            lb, sk = int(self.params["lookback"]), int(self.params["skip"])
            score = r.rolling(lb - sk).sum().shift(sk)
            warm = lb
        else:
            vw = int(self.params["vol_window"])
            score = -r.rolling(vw).std()
            warm = vw
        k = max(1, int(np.floor(len(frames) * float(self.params["quantile"]))))
        reb = int(self.params["rebalance_bars"])
        W = np.zeros((len(r), len(frames)))
        cur = np.zeros(len(frames))
        for i in range(len(r)):
            if i >= warm and (i - warm) % reb == 0:
                row = score.iloc[i].to_numpy()
                cur = np.zeros(len(frames))
                if np.isfinite(row).sum() >= 2 * k:
                    order = np.argsort(np.where(np.isfinite(row), row, np.nan))
                    valid = [j for j in order if np.isfinite(row[j])]
                    for j in valid[-k:]:
                        cur[j] = 0.5 / k
                    for j in valid[:k]:
                        cur[j] = -0.5 / k
            W[i] = cur
        return pd.DataFrame(W)


class BuyAndHold(Strategy):
    name = "buy_and_hold"
    description = "Fully invested long. Used as benchmark."
    defaults: dict[str, Any] = {}

    def weights(self, frames: list[pd.DataFrame]) -> pd.DataFrame:
        return pd.DataFrame({0: np.ones(len(frames[0]))})


STRATEGIES: dict[str, type[Strategy]] = {
    s.name: s for s in (EventStrategy, TimeSeriesMomentum, PairMeanReversion, CrossSectionalFactor, BuyAndHold)
}
