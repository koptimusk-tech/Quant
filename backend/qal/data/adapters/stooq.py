"""Stooq daily CSV downloads (equities, ETFs, indices, FX, some futures)."""

from __future__ import annotations

import io
from datetime import timedelta

import httpx
import pandas as pd

from qal.data.adapters.base import CatalogEntry, DataAdapter, FetchRequest, ProviderError

_INTERVALS = {"1d": "d", "1wk": "w"}


class StooqAdapter(DataAdapter):
    kind = "stooq"
    supported_frequencies = tuple(_INTERVALS)
    default_availability_delay = timedelta(hours=1)
    adjustment = "split_dividend_adjusted"
    base_url = "https://stooq.com/q/d/l/"

    def __init__(self, provider_id: str | None = None, transport: httpx.BaseTransport | None = None, **config):
        super().__init__(provider_id, **config)
        self._transport = transport

    @classmethod
    def catalog(cls) -> CatalogEntry:
        return CatalogEntry(
            kind=cls.kind,
            name="Stooq (CSV)",
            asset_classes=["equity", "etf", "index", "fx", "future"],
            frequencies=list(_INTERVALS),
            requires_credentials=False,
            cost="Free",
            license_notes="Check stooq.com terms; intended for personal use. Access rules can change "
            "(the CSV endpoint may require an API key or block automated access).",
            history_notes="Long daily history for many symbols. US tickers use the suffix .us (e.g. spy.us).",
            delay_notes="End-of-day; prices are adjusted for splits and dividends (levels embed future "
            "adjustments, returns are usable).",
            limits_notes="Daily request limits apply.",
            install="Built in (httpx).",
        )

    def _fetch(self, req: FetchRequest) -> pd.DataFrame:
        params = {"s": req.instrument.symbol.lower(), "i": _INTERVALS[req.frequency]}
        if req.start:
            params["d1"] = req.start.strftime("%Y%m%d")
        if req.end:
            params["d2"] = req.end.strftime("%Y%m%d")
        if key := self.config.get("api_key"):
            params["apikey"] = key
        with httpx.Client(transport=self._transport, timeout=30) as client:
            resp = client.get(self.base_url, params=params)
        resp.raise_for_status()
        text = resp.text.strip()
        if not text.lower().startswith("date"):
            raise ProviderError(f"Stooq did not return CSV for {req.instrument.symbol}: {text[:120]!r}")
        df = pd.read_csv(io.StringIO(text))
        df.columns = [c.lower() for c in df.columns]
        df["ts"] = pd.to_datetime(df["date"], utc=True)
        # Stooq prices are already adjusted; store them as adj_close too so returns use them.
        df["adj_close"] = df["close"]
        return df.set_index("ts")[[c for c in ["open", "high", "low", "close", "adj_close", "volume"] if c in df]]
