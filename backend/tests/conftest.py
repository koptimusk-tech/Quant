from __future__ import annotations

import os
from datetime import timedelta

import pandas as pd
import pytest

from qal import db
from qal.config import get_settings
from qal.data.adapters import FetchRequest
from qal.data.adapters.synthetic import SyntheticAdapter
from qal.data.schema import InstrumentSpec, normalize_bars


@pytest.fixture()
def env(tmp_path, monkeypatch):
    url = os.environ.get("QAL_TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/qal.db"
    monkeypatch.setenv("QAL_DATABASE_URL", url)
    monkeypatch.setenv("QAL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("QAL_ENABLE_SCHEDULER", "false")
    get_settings.cache_clear()
    db.init_engine(url)
    db.create_all()
    yield get_settings()
    db.Base.metadata.drop_all(db.get_engine())
    get_settings.cache_clear()


@pytest.fixture()
def session(env):
    with db.session_scope() as s:
        yield s


def synthetic_frame(symbol: str, n_bars: int = 1500, seed: int = 7) -> pd.DataFrame:
    spec = InstrumentSpec(symbol, "other")
    raw = SyntheticAdapter().fetch(FetchRequest(spec, "1d", options={"seed": seed, "n_bars": n_bars}))
    df, _ = normalize_bars(raw, provider="synthetic", spec=spec, frequency="1d", availability_delay=timedelta(0),
                           is_synthetic=True, source_ref="test")
    return df


@pytest.fixture(scope="session")
def syn():
    return {s: synthetic_frame(s) for s in ("SYN-A", "SYN-B", "SYN-C", "SYN-D")}
