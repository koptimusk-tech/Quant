"""Seeded synthetic market with *known injected anomalies*.

Every row produced here is flagged ``is_synthetic=True`` and the provider id is ``synthetic``,
so synthetic data can never be confused with real data (acceptance criterion 9). It exists to
test detectors against ground truth and to run the demo without network access.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache

import numpy as np
import pandas as pd

from qal.data.adapters.base import CatalogEntry, DataAdapter, FetchRequest, ProviderError

SYMBOLS = ("SYN-A", "SYN-B", "SYN-C", "SYN-D")


@dataclass(frozen=True)
class InjectedEvent:
    symbol: str
    bar_index: int
    kind: str  # return_shock, volume_spike, volatility_regime, pair_divergence
    detail: str


def injected_events(n_bars: int = 1500) -> list[InjectedEvent]:
    """Ground truth for the default universe; indices scale with ``n_bars``."""
    k = n_bars / 1500
    return [
        InjectedEvent("SYN-A", int(400 * k), "return_shock", "+8 sigma one-bar return"),
        InjectedEvent("SYN-A", int(900 * k), "return_shock", "-9 sigma one-bar return"),
        InjectedEvent("SYN-C", int(600 * k), "volume_spike", "volume x12 for one bar"),
        InjectedEvent("SYN-C", int(1100 * k), "volatility_regime", "volatility x3 for 80 bars"),
        InjectedEvent("SYN-B", int(1300 * k), "pair_divergence", "SYN-B/SYN-A spread +6 std, decaying"),
    ]


@lru_cache(maxsize=8)
def synthetic_universe(seed: int = 7, n_bars: int = 1500, start: str = "2019-01-01") -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start=start, periods=n_bars, tz="UTC")
    ev = injected_events(n_bars)
    events = {(e.symbol, e.kind): e.bar_index for e in ev}
    shocks = [e.bar_index for e in ev if e.kind == "return_shock"]

    def garch_returns(base_vol: float, n: int) -> np.ndarray:
        omega, alpha, beta = base_vol**2 * 0.05, 0.08, 0.87
        var = base_vol**2
        out = np.empty(n)
        z = rng.standard_t(df=6, size=n) / np.sqrt(6 / 4)
        for t in range(n):
            out[t] = np.sqrt(var) * z[t]
            var = omega + alpha * out[t] ** 2 + beta * var
        return out

    market = garch_returns(0.009, n_bars)
    # SYN-A: market factor + idiosyncratic noise, with two injected return shocks.
    r_a = 0.0002 + 1.0 * market + rng.normal(0, 0.006, n_bars)
    sd_a = np.std(r_a)
    r_a[shocks[0]] += 8 * sd_a
    r_a[shocks[1]] += -9 * sd_a
    log_a = np.log(100) + np.cumsum(r_a)

    # SYN-B: cointegrated with SYN-A through a stationary AR(1) spread; injected divergence.
    spread = np.zeros(n_bars)
    eps = rng.normal(0, 0.004, n_bars)
    for t in range(1, n_bars):
        spread[t] = 0.93 * spread[t - 1] + eps[t]
    j = events[("SYN-B", "pair_divergence")]
    sd_spread = np.std(spread)
    bump = 6 * sd_spread * 0.9 ** np.arange(n_bars - j)
    spread[j:] += bump
    log_b = np.log(50) + 0.8 * (log_a - log_a[0]) + spread

    # SYN-C: independent asset with a volatility regime shift and a volume spike.
    r_c = rng.standard_t(df=5, size=n_bars) * 0.01 / np.sqrt(5 / 3)
    v0 = events[("SYN-C", "volatility_regime")]
    r_c[v0 : v0 + 80] *= 3.0
    log_c = np.log(20) + np.cumsum(r_c)

    # SYN-D: plain random walk, no injected events (control series).
    log_d = np.log(75) + np.cumsum(rng.normal(0.0001, 0.012, n_bars))

    out: dict[str, pd.DataFrame] = {}
    for sym, logp in (("SYN-A", log_a), ("SYN-B", log_b), ("SYN-C", log_c), ("SYN-D", log_d)):
        close = np.exp(logp)
        prev = np.concatenate([[close[0]], close[:-1]])
        rets = np.diff(logp, prepend=logp[0])
        intrabar = np.abs(rng.normal(0, 0.004, n_bars)) + np.abs(rets) * 0.3
        open_ = prev * np.exp(rng.normal(0, 0.001, n_bars))
        high = np.maximum(open_, close) * np.exp(intrabar)
        low = np.minimum(open_, close) * np.exp(-intrabar)
        log_vol = np.empty(n_bars)
        log_vol[0] = 0
        noise = rng.normal(0, 0.25, n_bars)
        for t in range(1, n_bars):
            log_vol[t] = 0.6 * log_vol[t - 1] + noise[t]
        volume = 1_000_000 * np.exp(log_vol + 8 * np.abs(rets))
        if sym == "SYN-C":
            volume[events[("SYN-C", "volume_spike")]] *= 12
        out[sym] = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume.round()}, index=idx
        )
    return out


class SyntheticAdapter(DataAdapter):
    kind = "synthetic"
    is_synthetic = True
    supported_frequencies = ("1d",)
    default_availability_delay = timedelta(0)

    @classmethod
    def catalog(cls) -> CatalogEntry:
        return CatalogEntry(
            kind=cls.kind,
            name="Synthetic data with injected anomalies (clearly labelled, never real)",
            asset_classes=["other"],
            frequencies=["1d"],
            requires_credentials=False,
            cost="Free",
            license_notes="Generated locally.",
            history_notes=f"Symbols {', '.join(SYMBOLS)}; length set by n_bars (default 1500 business days).",
            delay_notes="None.",
            limits_notes="Only for testing detectors and demos. Every row is flagged is_synthetic.",
            install="Built in.",
        )

    def source_ref(self, req: FetchRequest) -> str:
        seed = int(req.options.get("seed", 7))
        n = int(req.options.get("n_bars", 1500))
        return f"synthetic:seed={seed}:n_bars={n}:{req.instrument.symbol}"

    def _fetch(self, req: FetchRequest) -> pd.DataFrame:
        if req.instrument.symbol not in SYMBOLS:
            raise ProviderError(f"Unknown synthetic symbol {req.instrument.symbol}; use one of {SYMBOLS}")
        df = synthetic_universe(int(req.options.get("seed", 7)), int(req.options.get("n_bars", 1500)))[
            req.instrument.symbol
        ].copy()
        if req.start is not None:
            df = df[df.index >= pd.Timestamp(req.start)]
        if req.end is not None:
            df = df[df.index <= pd.Timestamp(req.end)]
        return df
