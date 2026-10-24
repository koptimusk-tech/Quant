"""Predictive validation runner: real-only selection, missing data, spread alignment, outputs."""

import csv
import json

import numpy as np
import pandas as pd

from qal.data.ingest import IngestRequest, ingest
from qal.data.store import ParquetStore
from qal.models import Dataset
from qal.research import predictive


def _mt5_csv(path, n, seed, start="2010-01-04", freq="D", spread_pts=12, shift_hours=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n) if freq == "D" else pd.date_range(start, periods=n, freq="h")
    idx = idx + pd.Timedelta(hours=shift_hours)
    close = 1.1 * np.exp(np.cumsum(rng.standard_t(3, n) * 0.004))  # fat tails -> shocks
    open_ = np.r_[1.1, close[:-1]]
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["<DATE>", "<TIME>", "<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>", "<TICKVOL>", "<VOL>", "<SPREAD>"])
        for t, o, c, v in zip(idx, open_, close, rng.integers(500, 5000, n), strict=True):
            w.writerow([t.strftime("%Y.%m.%d"), t.strftime("%H:%M:%S"), f"{o:.5f}", f"{max(o, c) * 1.002:.5f}",
                        f"{min(o, c) * 0.998:.5f}", f"{c:.5f}", int(v), 0, spread_pts])
    return path


def _load(session, path, symbol, freq="1d"):
    return ingest(session, IngestRequest(adapter="local_file", symbol=symbol, asset_class="fx", frequency=freq,
                                         options={"path": str(path), "format": "mt5"}), actor="test")


def test_runner_uses_only_real_data_reports_missing_and_writes_outputs(env, session, tmp_path):
    _load(session, _mt5_csv(tmp_path / "eu.csv", 1500, 1), "EURUSD")
    _load(session, _mt5_csv(tmp_path / "uc.csv", 1500, 2), "USDCHF")
    syn = ingest(session, IngestRequest(adapter="synthetic", symbol="SYN-A", asset_class="fx",
                                        options={"seed": 3, "n_bars": 3000}), actor="test")
    session.flush()
    session.get(Dataset, syn["dataset_id"]).instrument.symbol = "EURUSD.syn"  # a synthetic look-alike
    session.flush()
    cfg = predictive.RunConfig(frequencies=("1d", "1h"), horizons=(1, 5))
    r = predictive.run(session, ParquetStore(env.data_dir), cfg, tmp_path / "out", log=lambda m: None)

    assert {a["symbol"] for a in r["data_audit"]} == {"EURUSD", "USDCHF"}
    assert all(not a["is_synthetic"] for a in r["data_audit"])
    eu = next(a for a in r["data_audit"] if a["symbol"] == "EURUSD")
    assert eu["synthetic_datasets_ignored"] == 1 and "tick volume" in eu["volume_kind"]
    # 1h is not loaded: reported with the export that would provide it, never filled in.
    missing = {(m["symbol"], m["frequency"]) for m in r["missing_datasets"]}
    assert missing == {("EURUSD", "1h"), ("USDCHF", "1h")}
    assert all("MetaTrader 5" in m["how_to_provide"] for m in r["missing_datasets"])

    dets = {(c["dataset"], c["detector"]) for c in r["comparison"]}
    assert ("EURUSD 1d", "pair_divergence") in dets and ("EURUSD 1d", "return_shock") in dets
    ok = [c for c in r["comparison"] if c["status"] == "ok"]
    assert any(c["selected_horizon"] and c["oos_events"] for c in ok)  # real rows, not only empty ones
    assert ok and all(c["verdict"] in ("no_evidence", "insufficient_sample", "in_sample_only", "oos_supported")
                      for c in ok)
    for s in r["studies"]:
        if s.get("status") == "ok":
            for row in s["result"]["table"]:  # run-wide BH is never below the per-study one
                assert row["in_sample"]["run_wide_adjusted_pvalue"] >= row["in_sample"]["adjusted_pvalue"] - 1e-12
    out = tmp_path / "out"
    assert {p.name for p in out.iterdir()} == {"results.json", "comparison.csv", "data_audit.json", "report.md"}
    assert json.loads((out / "results.json").read_text())["run_hash"] == r["run_hash"]
    assert "heuristic" in r["priority_note"] and "not calibrated" in r["priority_note"]
    report = (out / "report.md").read_text()
    assert "Datos reales únicamente" in report and "EURUSD 1h" in report

    # Same data and config: same studies (cached experiments) and same run hash.
    r2 = predictive.run(session, ParquetStore(env.data_dir), cfg, tmp_path / "out2", log=lambda m: None)
    assert r2["run_hash"] == r["run_hash"]
    assert [s.get("experiment_id") for s in r2["studies"]] == [s.get("experiment_id") for s in r["studies"]]


def test_spread_is_used_only_when_time_and_price_align(env, session, tmp_path):
    path = _mt5_csv(tmp_path / "eu.csv", 800, 1, spread_pts=10)
    _load(session, path, "EURUSD")
    session.flush()
    store = ParquetStore(env.data_dir)
    t = predictive.find_targets(session, ("EURUSD",), ("1d",))[0]
    df = store.read(t.dataset.path)
    good, info = predictive.align_spread(predictive.spread_from_mt5_csv(path, "EURUSD", "1d", 1e-5), df)
    assert info["used"] and info["coverage"] == 1.0
    assert abs(info["median_bps"] - 10 * 1e-5 / df["close"].median() * 1e4) < 0.05
    # The same export read in the wrong timezone does not line up with the bars: refused, not used.
    shifted = _mt5_csv(tmp_path / "eu_shift.csv", 800, 1, spread_pts=10, shift_hours=3)
    bad, info2 = predictive.align_spread(predictive.spread_from_mt5_csv(shifted, "EURUSD", "1d", 1e-5), df)
    assert bad is None and not info2["used"]


def test_symbol_matching():
    assert predictive.matches("EURUSD=X", "EURUSD") and predictive.matches("EURUSD.m", "EURUSD")
    assert predictive.matches("eur/usd", "EURUSD") and not predictive.matches("EURUSDCHFXYZ", "EURUSD")
    assert not predictive.matches("USDCHF", "EURUSD")


def test_mt5_spread_reader_stays_below_max_bars():
    from types import SimpleNamespace

    asked = {}

    class FakeMT5:
        TIMEFRAME_H1 = 16385

        def initialize(self):
            return True

        def shutdown(self):
            pass

        def symbol_select(self, s, on):
            return True

        def symbol_info(self, s):
            return SimpleNamespace(point=1e-5)

        def terminal_info(self):
            return SimpleNamespace(maxbars=100_000)

        def copy_rates_from_pos(self, s, tf, start, n):
            asked["n"] = n
            t = pd.date_range("2024-01-01", periods=3, freq="h", tz="UTC").astype("int64") // 10**9
            return pd.DataFrame({"time": t, "close": [1.1, 1.1, 1.1], "spread": [10, 12, 0]}).to_records(index=False)

    src = predictive.spread_from_mt5_terminal("EURUSD", "1h", 0, module=FakeMT5(), n_bars=200_000)
    assert asked["n"] == 99_999  # the terminal rejects a request for exactly maxbars bars
    assert abs(src.bps.iloc[0] - 10 * 1e-5 / 1.1 * 1e4) < 1e-9


def test_run_with_historical_spread_persists_the_study(env, session, tmp_path):
    path = _mt5_csv(tmp_path / "eu.csv", 1500, 1, spread_pts=10)
    _load(session, path, "EURUSD")
    session.flush()
    spread = predictive.spread_from_mt5_csv(path, "EURUSD", "1d", 1e-5)
    cfg = predictive.RunConfig(symbols=("EURUSD",), frequencies=("1d",), horizons=(1, 5), pair=None, spreads=[spread])
    r = predictive.run(session, ParquetStore(env.data_dir), cfg, tmp_path / "out", log=lambda m: None)
    assert r["data_audit"][0]["historical_spread"]["used"]
    ok = [s for s in r["studies"] if s.get("status") == "ok"]
    assert ok and all("historical_spread" in s["result"]["cost_model"]["scenarios"] for s in ok)
