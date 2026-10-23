"""Yahoo Finance via the community ``yfinance`` library (equities, ETFs, indices, FX, some futures)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from qal.data.adapters.base import CatalogEntry, DataAdapter, FetchRequest, ProviderError, ProviderUnavailable

_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "1d": "1d", "1wk": "1wk"}


class YahooAdapter(DataAdapter):
    kind = "yahoo"
    supported_frequencies = tuple(_INTERVALS)
    # Daily bars are stamped at 00:00 of the session date; event_ts = ts + 1 day is already
    # after the US close, so no extra delay is needed. Intraday Yahoo data is not real time.
    default_availability_delay = timedelta(minutes=15)
    adjustment = "raw+adj_close"

    @classmethod
    def catalog(cls) -> CatalogEntry:
        return CatalogEntry(
            kind=cls.kind,
            name="Yahoo Finance (yfinance)",
            asset_classes=["equity", "etf", "index", "fx", "future", "crypto"],
            frequencies=list(_INTERVALS),
            requires_credentials=False,
            cost="Free, unofficial",
            license_notes="Unofficial scraping of Yahoo endpoints; Yahoo terms restrict use to personal, "
            "non-commercial purposes. Not suitable for redistribution or production trading.",
            history_notes="Daily history for decades on many tickers; intraday history is short "
            "(about 7 days for 1m, about 60 days for other sub-daily intervals, longer for 60m).",
            delay_notes="Quotes can be delayed; historical bars may be revised. Survivorship bias: "
            "delisted tickers are generally unavailable.",
            limits_notes="Undocumented rate limits; the endpoint can break without notice.",
            install="pip install 'quant-anomaly-lab[yahoo]'",
        )

    def _fetch(self, req: FetchRequest) -> pd.DataFrame:
        try:
            import yfinance as yf
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ProviderUnavailable("yfinance is not installed: pip install 'quant-anomaly-lab[yahoo]'") from exc
        start = req.start or datetime(2000, 1, 1, tzinfo=UTC)
        end = req.end or datetime.now(UTC) + timedelta(days=1)
        raw = yf.download(
            req.instrument.symbol,
            start=start.date().isoformat() if req.frequency in ("1d", "1wk") else start,
            end=end.date().isoformat() if req.frequency in ("1d", "1wk") else end,
            interval=_INTERVALS[req.frequency],
            auto_adjust=False,
            actions=False,
            progress=False,
            threads=False,
        )
        if raw is None or raw.empty:
            raise ProviderError(f"Yahoo returned no data for {req.instrument.symbol}")
        if isinstance(raw.columns, pd.MultiIndex):
            raw = raw.xs(req.instrument.symbol, axis=1, level=-1) if raw.columns.nlevels > 1 else raw
        raw = raw.rename(columns=lambda c: str(c).lower().replace(" ", "_"))
        idx = pd.DatetimeIndex(raw.index)
        if req.frequency in ("1d", "1wk"):
            # Session dates: interpret as calendar dates in UTC (bar start 00:00 UTC).
            idx = pd.DatetimeIndex(idx.tz_localize(None) if idx.tz is not None else idx).normalize().tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC") if idx.tz is not None else idx.tz_localize("UTC")
        raw.index = idx
        cols = [c for c in ["open", "high", "low", "close", "adj_close", "volume"] if c in raw.columns]
        return raw[cols]
