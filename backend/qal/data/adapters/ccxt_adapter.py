"""Crypto exchanges through CCXT (Binance, Kraken, Coinbase, Bybit, OKX, ...). Public OHLCV needs no key."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from qal.data.schema import bar_delta
from qal.data.adapters.base import CatalogEntry, DataAdapter, FetchRequest, ProviderError, ProviderUnavailable

_TF = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d", "1wk": "1w"}


class CCXTAdapter(DataAdapter):
    kind = "ccxt"
    supported_frequencies = tuple(_TF)
    # Exchange candles are final shortly after the bar closes.
    default_availability_delay = timedelta(seconds=5)

    def __init__(self, provider_id: str | None = None, exchange: str = "binance", client: Any = None, **config: Any):
        super().__init__(provider_id or f"ccxt:{exchange}", **config)
        self.exchange_id = exchange
        self._client = client

    @classmethod
    def catalog(cls) -> CatalogEntry:
        return CatalogEntry(
            kind=cls.kind,
            name="Crypto exchanges via CCXT (Binance, Kraken, Coinbase, Bybit, OKX...)",
            asset_classes=["crypto"],
            frequencies=list(_TF),
            requires_credentials=False,
            cost="Free public market data",
            license_notes="Each exchange's API terms apply. Some exchanges geo-block regions "
            "(for example Binance from the US; use binanceus or another venue).",
            history_notes="Varies by exchange; Binance spot klines go back to listing date.",
            delay_notes="Candles final seconds after close. Volume is venue-specific, not consolidated.",
            limits_notes="Exchange rate limits; CCXT throttles automatically (enableRateLimit).",
            install="pip install 'quant-anomaly-lab[crypto]'",
        )

    def _client_or_build(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import ccxt
        except ImportError as exc:  # pragma: no cover
            raise ProviderUnavailable("ccxt is not installed: pip install 'quant-anomaly-lab[crypto]'") from exc
        if not hasattr(ccxt, self.exchange_id):
            raise ProviderUnavailable(f"Unknown CCXT exchange {self.exchange_id!r}")
        # Market data only: no API key is passed to research ingestion (DECISIONS.md D-008).
        self._client = getattr(ccxt, self.exchange_id)({"enableRateLimit": True})
        return self._client

    def _fetch(self, req: FetchRequest) -> pd.DataFrame:
        client = self._client_or_build()
        tf = _TF[req.frequency]
        step_ms = int(bar_delta(req.frequency).total_seconds() * 1000)
        since = int((req.start or datetime.now(UTC) - timedelta(days=365)).timestamp() * 1000)
        end_ms = int((req.end or datetime.now(UTC)).timestamp() * 1000)
        limit = int(req.options.get("page_limit", 1000))
        rows: list[list[float]] = []
        for _ in range(int(req.options.get("max_pages", 500))):
            page = client.fetch_ohlcv(req.instrument.symbol, timeframe=tf, since=since, limit=limit)
            if not page:
                break
            rows.extend(page)
            last = page[-1][0]
            if last >= end_ms or len(page) < limit:
                break
            since = last + step_ms
        if not rows:
            raise ProviderError(f"{self.exchange_id} returned no candles for {req.instrument.symbol}")
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df[df["ts"] <= pd.Timestamp(end_ms, unit="ms", tz="UTC")]
        # Drop the still-forming candle: its values are not final until the bar closes.
        now = pd.Timestamp.now(tz="UTC")
        df = df[df["ts"] + pd.Timedelta(milliseconds=step_ms) <= now]
        return df.set_index("ts")
