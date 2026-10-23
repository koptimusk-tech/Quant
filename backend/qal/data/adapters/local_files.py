"""CSV / Parquet files, including MetaTrader 4/5 history exports."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd

from qal.data.adapters.base import CatalogEntry, DataAdapter, FetchRequest, ProviderError
from qal.data.schema import FREQUENCIES

_TS_CANDIDATES = ("ts", "timestamp", "datetime", "date", "time", "open_time", "gmt time")
_ALIASES = {
    "o": "open",
    "h": "high",
    "l": "low",
    "c": "close",
    "v": "volume",
    "vol": "volume",
    "tickvol": "volume",
    "tick_volume": "volume",
    "adj close": "adj_close",
    "adjclose": "adj_close",
    "adjusted_close": "adj_close",
}


def _read_any(path: Path, fmt: str) -> pd.DataFrame:
    if fmt == "parquet" or (fmt == "auto" and path.suffix.lower() in (".parquet", ".pq")):
        return pd.read_parquet(path)
    if fmt == "mt4":
        # MT4 "Export" CSV: no header -> date,time,open,high,low,close,volume
        df = pd.read_csv(path, header=None, names=["date", "time", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str), format="mixed")
        return df.drop(columns=["date", "time"])
    if fmt == "mt5":
        # MT5 history export: tab separated, headers like <DATE> <TIME> <OPEN> ... <TICKVOL> <VOL> <SPREAD>
        df = pd.read_csv(path, sep="\t")
        df.columns = [c.strip("<>").lower() for c in df.columns]
        if "time" in df.columns:
            df["ts"] = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str), format="mixed")
            df = df.drop(columns=["date", "time"])
        else:
            df["ts"] = pd.to_datetime(df["date"].astype(str), format="mixed")
            df = df.drop(columns=["date"])
        if "vol" in df.columns and df["vol"].fillna(0).abs().sum() > 0:
            df = df.drop(columns=["tickvol"], errors="ignore").rename(columns={"vol": "volume"})
        else:
            df = df.drop(columns=["vol"], errors="ignore").rename(columns={"tickvol": "volume"})
        return df
    return pd.read_csv(path, sep=None, engine="python")


def standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.rename(columns={k: v for k, v in _ALIASES.items() if k in df.columns})
    if "ts" not in df.columns:
        for cand in _TS_CANDIDATES:
            if cand in df.columns:
                df = df.rename(columns={cand: "ts"})
                break
    if "ts" not in df.columns:
        raise ProviderError(f"No timestamp column found; expected one of {_TS_CANDIDATES}")
    col = df["ts"]
    if pd.api.types.is_numeric_dtype(col):
        unit = "ms" if col.dropna().abs().median() > 1e11 else "s"
        df["ts"] = pd.to_datetime(col, unit=unit, utc=True)
    else:
        df["ts"] = pd.to_datetime(col, utc=False, format="mixed")
    return df


class LocalFileAdapter(DataAdapter):
    kind = "local_file"
    supported_frequencies = tuple(FREQUENCIES)

    @classmethod
    def catalog(cls) -> CatalogEntry:
        return CatalogEntry(
            kind=cls.kind,
            name="Local CSV / Parquet (incl. MetaTrader 4/5 exports)",
            asset_classes=["equity", "etf", "future", "fx", "crypto", "index", "other"],
            frequencies=list(FREQUENCIES),
            requires_credentials=False,
            cost="Free",
            license_notes="Whatever licence applies to the file you load.",
            history_notes="Whatever the file contains.",
            delay_notes="Set availability_delay_minutes per file; default 0 after the bar end.",
            limits_notes="None.",
            install="Built in.",
        )

    def availability_delay(self, req: FetchRequest) -> timedelta:
        return timedelta(minutes=float(req.options.get("availability_delay_minutes", 0)))

    def source_ref(self, req: FetchRequest) -> str:
        return f"file:{Path(req.options['path']).resolve()}"

    def _fetch(self, req: FetchRequest) -> pd.DataFrame:
        path = Path(req.options.get("path", ""))
        if not path.is_file():
            raise ProviderError(f"File not found: {path}")
        df = standardize_columns(_read_any(path, req.options.get("format", "auto")))
        tz = req.options.get("source_timezone")
        if df["ts"].dt.tz is None:
            # Naive timestamps are interpreted in the declared source timezone (default UTC).
            df["ts"] = df["ts"].dt.tz_localize(tz or "UTC").dt.tz_convert("UTC")
        if req.start is not None:
            df = df[df["ts"] >= pd.Timestamp(req.start)]
        if req.end is not None:
            df = df[df["ts"] <= pd.Timestamp(req.end)]
        return df

    def fetch(self, req: FetchRequest) -> pd.DataFrame:
        # Local files are not retried.
        return self._fetch(req)
