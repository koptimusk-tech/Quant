"""Content-addressed Parquet store for immutable dataset snapshots."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pandas as pd

HASH_COLUMNS = ["ts", "available_ts", "open", "high", "low", "close", "adj_close", "volume", "is_synthetic"]


def content_hash(df: pd.DataFrame) -> str:
    """Hash of the economically relevant content (excludes received_ts, so re-downloads dedupe)."""
    h = hashlib.sha256()
    part = df[HASH_COLUMNS].copy()
    for c in ("ts", "available_ts"):
        part[c] = pd.to_datetime(part[c], utc=True).astype("int64")
    h.update(pd.util.hash_pandas_object(part, index=False).values.tobytes())
    h.update(str(list(part.columns)).encode())
    return h.hexdigest()


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)


class ParquetStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path_for(self, provider: str, symbol: str, frequency: str, digest: str) -> Path:
        return self.root / _safe(provider) / _safe(symbol) / frequency / f"{digest[:16]}.parquet"

    def write(self, df: pd.DataFrame, provider: str, symbol: str, frequency: str) -> tuple[Path, str]:
        digest = content_hash(df)
        path = self.path_for(provider, symbol, frequency, digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            df.to_parquet(tmp, index=False)
            tmp.replace(path)
        return path, digest

    @staticmethod
    def read(path: str | Path) -> pd.DataFrame:
        df = pd.read_parquet(path)
        for c in ("ts", "event_ts", "available_ts", "received_ts"):
            df[c] = pd.to_datetime(df[c], utc=True)
        return df
