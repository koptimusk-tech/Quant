"""MetaTrader 5 terminal via the official ``MetaTrader5`` Python package (Windows only).

On other platforms load MT4/MT5 history exports with the local_file adapter (format mt4/mt5).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from qal.data.adapters.base import CatalogEntry, DataAdapter, FetchRequest, ProviderError, ProviderUnavailable

_TF_NAMES = {
    "1m": "TIMEFRAME_M1",
    "5m": "TIMEFRAME_M5",
    "15m": "TIMEFRAME_M15",
    "30m": "TIMEFRAME_M30",
    "1h": "TIMEFRAME_H1",
    "4h": "TIMEFRAME_H4",
    "1d": "TIMEFRAME_D1",
    "1wk": "TIMEFRAME_W1",
}


class MetaTrader5Adapter(DataAdapter):
    kind = "metatrader5"
    supported_frequencies = tuple(_TF_NAMES)
    default_availability_delay = timedelta(seconds=1)

    def __init__(self, provider_id: str | None = None, module: Any = None, **config: Any):
        super().__init__(provider_id, **config)
        self._mt5 = module

    @classmethod
    def catalog(cls) -> CatalogEntry:
        return CatalogEntry(
            kind=cls.kind,
            name="MetaTrader 5 terminal (broker data)",
            asset_classes=["fx", "future", "index", "equity", "crypto", "other"],
            frequencies=list(_TF_NAMES),
            requires_credentials=True,
            cost="Free with a broker account (demo accounts work)",
            license_notes="Data belongs to your broker; check the broker's terms before redistribution.",
            history_notes="Depends on broker and on how much history the terminal has downloaded.",
            delay_notes="Bar times are in broker server time, not UTC: set server_utc_offset_hours. "
            "Prices are broker quotes (CFD/FX), not a consolidated market feed; volume is tick volume.",
            limits_notes="Requires a running MT5 terminal on Windows and the MetaTrader5 package.",
            install="pip install MetaTrader5 (Windows only)",
        )

    def _module(self) -> Any:
        if self._mt5 is not None:
            return self._mt5
        try:
            import MetaTrader5 as mt5  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ProviderUnavailable(
                "MetaTrader5 package not available. It only runs on Windows next to an MT5 terminal. "
                "Alternative: export history from MT4/MT5 and load it with the local_file adapter "
                "(format 'mt4' or 'mt5')."
            ) from exc
        self._mt5 = mt5
        return mt5

    def _fetch(self, req: FetchRequest) -> pd.DataFrame:
        mt5 = self._module()
        init_kwargs = {k: v for k, v in {
            "path": self.config.get("terminal_path"),
            "login": self.config.get("login"),
            "password": self.config.get("password"),
            "server": self.config.get("server"),
        }.items() if v}
        if not mt5.initialize(**init_kwargs):
            raise ProviderUnavailable(f"MT5 initialize() failed: {mt5.last_error()}")
        try:
            if not mt5.symbol_select(req.instrument.symbol, True):
                raise ProviderError(f"MT5 symbol not available: {req.instrument.symbol}")
            start = req.start or datetime.now(UTC) - timedelta(days=365 * 5)
            end = req.end or datetime.now(UTC)
            rates = mt5.copy_rates_range(req.instrument.symbol, getattr(mt5, _TF_NAMES[req.frequency]), start, end)
            if rates is None or len(rates) == 0:
                raise ProviderError(f"MT5 returned no bars for {req.instrument.symbol}: {mt5.last_error()}")
        finally:
            mt5.shutdown()
        df = pd.DataFrame(rates)
        offset = float(self.config.get("server_utc_offset_hours", req.options.get("server_utc_offset_hours", 0)))
        df["ts"] = pd.to_datetime(df["time"], unit="s", utc=True) - pd.Timedelta(hours=offset)
        vol = "real_volume" if "real_volume" in df and df["real_volume"].sum() > 0 else "tick_volume"
        df = df.rename(columns={vol: "volume"})
        return df.set_index("ts")[["open", "high", "low", "close", "volume"]]
