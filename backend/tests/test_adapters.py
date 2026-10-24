"""Adapter tests use recorded-shape fixtures and fakes; no network. Real calls: -m network."""

import sys
import types
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pandas as pd
import pytest

from qal.data.adapters import FetchRequest, ProviderError, ProviderUnavailable, build_adapter, provider_catalog
from qal.data.adapters.ccxt_adapter import CCXTAdapter
from qal.data.adapters.local_files import LocalFileAdapter
from qal.data.adapters.metatrader import MetaTrader5Adapter
from qal.data.adapters.stooq import StooqAdapter
from qal.data.adapters.yahoo import YahooAdapter
from qal.data.schema import InstrumentSpec


def _req(symbol, asset="equity", freq="1d", **opts):
    return FetchRequest(InstrumentSpec(symbol, asset), freq, options=opts)


def test_catalog_lists_all_adapters():
    kinds = {c["kind"] for c in provider_catalog()}
    assert kinds == {"local_file", "yahoo", "ccxt", "metatrader5", "stooq", "synthetic"}
    assert all(c["license_notes"] for c in provider_catalog())


def test_csv_with_aliases_and_timezone(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("Date,Open,High,Low,Close,Adj Close,Volume\n"
                 "2024-01-02,10,11,9,10.5,10.4,100\n2024-01-03,10.5,12,10,11,10.9,200\n")
    df = LocalFileAdapter().fetch(_req("X", path=str(p), source_timezone="America/New_York"))
    assert list(df["ts"]) == [pd.Timestamp("2024-01-02 05:00", tz="UTC"), pd.Timestamp("2024-01-03 05:00", tz="UTC")]
    assert "adj_close" in df.columns


def test_parquet_roundtrip(tmp_path):
    p = tmp_path / "x.parquet"
    pd.DataFrame({"timestamp": pd.date_range("2024-01-01", periods=3, freq="h", tz="UTC"), "open": 1.0,
                  "high": 2.0, "low": 0.5, "close": 1.5, "volume": 3.0}).to_parquet(p)
    df = LocalFileAdapter().fetch(_req("X", freq="1h", path=str(p)))
    assert len(df) == 3 and str(df["ts"].dt.tz) == "UTC"


def test_metatrader5_export_format(tmp_path):
    p = tmp_path / "EURUSD_H1.csv"
    p.write_text("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
                 "2024.01.02\t00:00:00\t1.1000\t1.1010\t1.0990\t1.1005\t1200\t0\t5\n"
                 "2024.01.02\t01:00:00\t1.1005\t1.1020\t1.1000\t1.1015\t900\t0\t5\n")
    df = LocalFileAdapter().fetch(_req("EURUSD", "fx", "1h", path=str(p), format="mt5"))
    assert df["volume"].tolist() == [1200, 900]  # tick volume when real volume is zero
    assert df["ts"].iloc[1] == pd.Timestamp("2024-01-02 01:00", tz="UTC")


def test_metatrader4_export_format(tmp_path):
    p = tmp_path / "EURUSD1440.csv"
    p.write_text("2024.01.02,00:00,1.1,1.2,1.0,1.15,1000\n2024.01.03,00:00,1.15,1.25,1.1,1.2,900\n")
    df = LocalFileAdapter().fetch(_req("EURUSD", "fx", path=str(p), format="mt4"))
    assert len(df) == 2 and df["close"].iloc[1] == 1.2


def test_missing_file_errors():
    with pytest.raises(ProviderError):
        LocalFileAdapter().fetch(_req("X", path="/nonexistent.csv"))


def test_yahoo_adapter_parses_multiindex(monkeypatch):
    idx = pd.DatetimeIndex(["2024-01-02", "2024-01-03"], name="Date")
    cols = pd.MultiIndex.from_product([["Adj Close", "Close", "High", "Low", "Open", "Volume"], ["SPY"]],
                                      names=["Price", "Ticker"])
    frame = pd.DataFrame([[470.0, 472.0, 473.0, 469.0, 471.0, 1e7], [468.0, 470.0, 471.0, 467.0, 469.0, 9e6]],
                         index=idx, columns=cols)
    fake = types.SimpleNamespace(download=lambda *a, **k: frame)
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    df = YahooAdapter().fetch(_req("SPY", "etf"))
    assert list(df.columns) == ["open", "high", "low", "close", "adj_close", "volume"]
    assert df.index[0] == pd.Timestamp("2024-01-02", tz="UTC")


def test_yahoo_empty_is_error_not_fake_data(monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=lambda *a, **k: pd.DataFrame()))
    ad = YahooAdapter(retry_base_seconds=0)
    with pytest.raises(ProviderError):
        ad.fetch(_req("NOPE"))


class FakeExchange:
    def __init__(self, n, step_ms, start_ms):
        self.rows = [[start_ms + i * step_ms, 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, 10.0] for i in range(n)]
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        self.calls += 1
        return [r for r in self.rows if r[0] >= since][:limit]


def test_ccxt_paginates_and_drops_forming_candle():
    now = pd.Timestamp.now(tz="UTC").floor("h")
    start = now - pd.Timedelta(hours=25)
    ex = FakeExchange(26, 3_600_000, int(start.timestamp() * 1000))  # last candle starts at `now` (forming)
    ad = CCXTAdapter(exchange="binance", client=ex)
    req = FetchRequest(InstrumentSpec("BTC/USDT", "crypto"), "1h", start=start.to_pydatetime(),
                       options={"page_limit": 10})
    df = ad.fetch(req)
    assert ex.calls >= 3
    assert len(df) == 25 and df.index.max() < now
    assert ad.provider_id == "ccxt:binance"


def test_stooq_parses_csv_and_rejects_non_csv():
    csv = "Date,Open,High,Low,Close,Volume\n2024-01-02,1,2,0.5,1.5,100\n2024-01-03,1.5,2.5,1,2,120\n"
    ok = StooqAdapter(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=csv)))
    df = ok.fetch(_req("spy.us", "etf"))
    assert len(df) == 2 and (df["adj_close"] == df["close"]).all()
    bad = StooqAdapter(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="Exceeded the daily hits limit")),
                       retry_base_seconds=0)
    with pytest.raises(ProviderError):
        bad.fetch(_req("spy.us", "etf"))


def test_metatrader5_unavailable_off_windows():
    if sys.platform == "win32":
        pytest.skip("MetaTrader5 may be installed on Windows")
    with pytest.raises(ProviderUnavailable):
        MetaTrader5Adapter().fetch(_req("EURUSD", "fx"))


def test_metatrader5_with_fake_module_applies_server_offset():
    t0 = int(datetime(2024, 1, 2, 2, tzinfo=UTC).timestamp())
    rates = np.array([(t0, 1.1, 1.2, 1.0, 1.15, 50, 0, 0)], dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"),
                     ("low", "f8"), ("close", "f8"), ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8")])
    fake = types.SimpleNamespace(initialize=lambda **k: True, symbol_select=lambda s, b: True, TIMEFRAME_H1=16385,
                                 copy_rates_range=lambda *a: rates, shutdown=lambda: None, last_error=lambda: (0, ""))
    ad = MetaTrader5Adapter(module=fake, server_utc_offset_hours=2)
    df = ad.fetch(_req("EURUSD", "fx", "1h"))
    assert df.index[0] == pd.Timestamp("2024-01-02 00:00", tz="UTC")
    assert df["volume"].iloc[0] == 50


def test_unsupported_frequency_rejected():
    with pytest.raises(ProviderError):
        StooqAdapter().fetch(_req("spy.us", freq="1m"))


def test_build_adapter_never_takes_credentials_from_payload(monkeypatch):
    monkeypatch.setenv("QAL_CCXT_EXCHANGE", "kraken")
    from qal.config import get_settings

    get_settings.cache_clear()
    ad = build_adapter("ccxt")
    assert ad.exchange_id == "kraken"
    assert "apiKey" not in ad.config and "secret" not in ad.config
    get_settings.cache_clear()


@pytest.mark.network
def test_real_yahoo_download():  # pragma: no cover - run manually with -m network
    df = YahooAdapter().fetch(FetchRequest(InstrumentSpec("SPY", "etf"), "1d",
                                           start=datetime.now(UTC) - timedelta(days=30)))
    assert len(df) > 10


@pytest.mark.network
def test_real_binance_download():  # pragma: no cover
    df = CCXTAdapter(exchange="binance").fetch(FetchRequest(InstrumentSpec("BTC/USDT", "crypto"), "1d",
                                                            start=datetime.now(UTC) - timedelta(days=30)))
    assert len(df) > 10


def test_metatrader5_reads_long_ranges_in_windows_and_stops_where_history_ends():
    dt = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"), ("tick_volume", "i8"),
          ("spread", "i4"), ("real_volume", "i8")]
    calls = []

    def rng(sym, tf, lo, hi):
        calls.append((lo, hi))
        if lo < datetime(2021, 1, 1, tzinfo=UTC):  # no history this far back
            return None
        hours = pd.date_range(lo, hi, freq="h", inclusive="left")[:3]
        return np.array([(int(t.timestamp()), 1.1, 1.2, 1.0, 1.15, 5, 1, 0) for t in hours], dtype=dt)

    fake = types.SimpleNamespace(initialize=lambda **k: True, symbol_select=lambda s, b: True, TIMEFRAME_H1=16385,
                                 copy_rates_range=rng, shutdown=lambda: None, last_error=lambda: (-2, "Invalid params"))
    ad = MetaTrader5Adapter(module=fake)
    # Naive dates, as the CLI passes them, are read as UTC.
    req = FetchRequest(InstrumentSpec("EURUSD", "fx"), "1h", start=datetime(2019, 1, 1), end=datetime(2024, 1, 1))
    df = ad.fetch(req)
    assert len(calls) >= 4 and all((hi - lo).days <= 365 for lo, hi in calls)
    assert len(df) == 9 and df.index.is_monotonic_increasing
    assert "no bars before" in ad.last_fetch_note
