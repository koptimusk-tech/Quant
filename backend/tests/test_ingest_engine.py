from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from qal.anomaly.engine import ScanConfig, run_scan
from qal.config import get_settings
from qal.data.adapters.base import CatalogEntry, DataAdapter, FetchRequest
from qal.data.ingest import IngestRequest, ingest
from qal.data.store import ParquetStore
from qal.models import AnomalyDetection, AuditEvent, DataQualityEvent, Dataset, Experiment


def _store():
    return ParquetStore(get_settings().data_dir)


def _ingest_synth(session, symbols=("SYN-A", "SYN-B", "SYN-C", "SYN-D")):
    return {s: ingest(session, IngestRequest(adapter="synthetic", symbol=s, asset_class="other"))["dataset_id"]
            for s in symbols}


class FrameAdapter(DataAdapter):
    """Test adapter returning a frame we control (stands in for a remote provider)."""

    kind = "frame"
    supported_frequencies = ("1d",)

    def __init__(self, frame, **kw):
        super().__init__("frame-test", **kw)
        self.frame = frame
        self.requests: list[FetchRequest] = []

    @classmethod
    def catalog(cls):
        return CatalogEntry("frame", "test", ["equity"], ["1d"], False, "", "", "", "", "", "")

    def _fetch(self, req):
        self.requests.append(req)
        f = self.frame
        if req.start is not None:
            f = f[f.index >= pd.Timestamp(req.start)]
        return f


def _frame(n, start="2024-01-01"):
    idx = pd.bdate_range(start, periods=n, tz="UTC")
    c = 100 + np.sin(np.arange(n)) + np.arange(n) * 0.1
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1e6}, index=idx)


def test_synthetic_data_is_labelled_everywhere(session):
    ids = _ingest_synth(session, ("SYN-A",))
    ds = session.get(Dataset, ids["SYN-A"])
    assert ds.is_synthetic and ds.provider_id == "synthetic"
    df = ParquetStore.read(ds.path)
    assert df["is_synthetic"].all()


def test_incremental_ingest_appends_and_detects_revisions(session):
    full = _frame(60)
    ad = FrameAdapter(full.iloc[:40])
    req = IngestRequest(adapter="frame", symbol="XYZ", asset_class="equity")
    r1 = ingest(session, req, adapter=ad, now=datetime(2024, 3, 1, tzinfo=UTC))
    # Same data again -> same content hash, no new dataset.
    r2 = ingest(session, req, adapter=ad, now=datetime(2024, 3, 2, tzinfo=UTC))
    assert r2["unchanged"] and r2["dataset_id"] == r1["dataset_id"]
    assert ad.requests[-1].start is not None  # incremental window requested
    # Provider adds 20 bars and revises one old bar.
    revised = full.copy()
    revised.iloc[38, revised.columns.get_loc("close")] += 0.5
    ad.frame = revised
    r3 = ingest(session, req, adapter=ad, now=datetime(2024, 4, 1, tzinfo=UTC))
    assert r3["rows"] == 60 and not r3["unchanged"]
    assert r3["merge"]["revised_bars"] == 1
    assert session.get(Dataset, r1["dataset_id"]).is_latest is False
    ev = session.scalars(select(DataQualityEvent).where(DataQualityEvent.dataset_id == r3["dataset_id"])).all()
    assert any(e.check == "revisions" for e in ev)
    df = ParquetStore.read(session.get(Dataset, r3["dataset_id"]).path)
    # Unchanged old bars keep the time we first received them.
    assert df["received_ts"].iloc[0] == pd.Timestamp("2024-03-01", tz="UTC")
    assert df["received_ts"].iloc[59] == pd.Timestamp("2024-04-01", tz="UTC")


def test_scan_records_experiment_with_provenance(session):
    ids = _ingest_synth(session)
    exp = run_scan(session, ScanConfig(dataset_ids=list(ids.values()), pairs=[(ids["SYN-B"], ids["SYN-A"])]),
                   store=_store())
    assert exp.status == "succeeded"
    assert exp.n_tests > 10000
    assert {r["content_hash"] for r in exp.data_refs} == {session.get(Dataset, i).content_hash for i in ids.values()}
    assert "return_shock@1.1.0" in exp.algorithm_version
    anomalies = session.scalars(select(AnomalyDetection).where(AnomalyDetection.experiment_id == exp.id)).all()
    assert len(anomalies) == exp.results["n_anomalies"] > 0
    for a in anomalies:
        assert a.is_synthetic
        assert a.alternative_explanations
        assert 0 <= a.statistical_score < 1
        assert a.adjusted_pvalue >= a.empirical_pvalue
        assert a.available_ts >= a.event_ts


def test_identical_scan_is_cached_and_forced_rerun_reproduces(session):
    ids = list(_ingest_synth(session, ("SYN-A", "SYN-C")).values())
    e1 = run_scan(session, ScanConfig(dataset_ids=ids), store=_store())
    session.flush()
    e2 = run_scan(session, ScanConfig(dataset_ids=ids), store=_store())
    assert e2.id == e1.id
    e3 = run_scan(session, ScanConfig(dataset_ids=ids, force=True), store=_store())
    session.flush()
    assert e3.id != e1.id and e3.config_hash == e1.config_hash

    def sig(e):
        rows = session.scalars(select(AnomalyDetection).where(AnomalyDetection.experiment_id == e.id)).all()
        return sorted((a.detector, a.instrument_id, a.event_ts, round(a.statistic, 12)) for a in rows)

    assert sig(e1) == sig(e3)


def test_gate_a_blocks_failed_dataset(session):
    bad = _frame(400)
    bad.iloc[10:30, bad.columns.get_loc("high")] = 1.0  # >1% OHLC violations -> fail
    r = ingest(session, IngestRequest(adapter="frame", symbol="BAD", asset_class="equity"), adapter=FrameAdapter(bad))
    assert r["quality_status"] == "fail"
    exp = run_scan(session, ScanConfig(dataset_ids=[r["dataset_id"]]), store=_store())
    assert exp.status == "succeeded"
    assert exp.results["blocked_by_gate_a"][0]["dataset_id"] == r["dataset_id"]
    assert exp.results["n_anomalies"] == 0


def test_negative_results_are_recorded(session):
    ids = _ingest_synth(session, ("SYN-D",))
    exp = run_scan(session, ScanConfig(dataset_ids=[ids["SYN-D"]], detectors={"return_shock": {}}), store=_store())
    assert exp.status == "succeeded" and exp.results["n_anomalies"] == 0
    assert session.get(Experiment, exp.id) is not None


def test_failed_scan_is_recorded(session, monkeypatch):
    ids = _ingest_synth(session, ("SYN-A",))
    from qal.anomaly import detectors

    def boom(self, df, other=None):
        raise RuntimeError("detector crashed")

    monkeypatch.setattr(detectors.ReturnShockDetector, "run", boom)
    exp = run_scan(session, ScanConfig(dataset_ids=[ids["SYN-A"]], detectors={"return_shock": {}}), store=_store())
    assert exp.status == "failed" and "detector crashed" in exp.error
    session.flush()
    assert session.scalars(select(AuditEvent).where(AuditEvent.entity_id == exp.id)).first() is not None


def test_economic_score_marks_indices_not_tradable(session):
    f = _frame(400)
    f.iloc[350, f.columns.get_loc("close")] *= 1.3
    f.iloc[350, f.columns.get_loc("high")] = f.iloc[350]["close"] + 1
    r = ingest(session, IngestRequest(adapter="frame", symbol="IDX", asset_class="index"), adapter=FrameAdapter(f))
    exp = run_scan(session, ScanConfig(dataset_ids=[r["dataset_id"]], detectors={"return_shock": {}}), store=_store())
    a = session.scalars(select(AnomalyDetection).where(AnomalyDetection.experiment_id == exp.id)).first()
    assert a is not None and a.economic_score is None
    assert "not directly tradable" in a.economic_context["reason"]


def test_unknown_detector_rejected(session):
    with pytest.raises(ValueError):
        run_scan(session, ScanConfig(dataset_ids=[], detectors={"magic": {}}), store=_store())
