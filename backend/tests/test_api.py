"""Integration tests through the HTTP API (FastAPI TestClient + SQLite)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from qal.jobs.runner import process_once


@pytest.fixture()
def client(env):
    from qal.api.main import create_app

    with TestClient(create_app()) as c:
        yield c


def _ingest(client, symbols=("SYN-A", "SYN-B", "SYN-C")):
    r = client.post("/api/v1/data/ingest", json={"items": [
        {"adapter": "synthetic", "symbol": s, "asset_class": "other"} for s in symbols]})
    assert r.status_code == 200, r.text
    return {x["symbol"]: x["dataset_id"] for x in r.json()["ingested"]}


def test_health_and_openapi(client):
    h = client.get("/api/v1/health").json()
    assert h["status"] == "ok" and h["live_trading_enabled"] is False
    spec = client.get("/api/v1/openapi.json").json()
    assert "/api/v1/anomalies" in spec["paths"] and "/api/v1/scans" in spec["paths"]


def test_end_to_end_scan_list_filter_detail(client):
    ids = _ingest(client)
    r = client.post("/api/v1/scans", json={"dataset_ids": list(ids.values()), "pairs": [[ids["SYN-B"], ids["SYN-A"]]]})
    assert r.status_code == 200, r.text
    exp = r.json()["experiment"]
    assert exp["status"] == "succeeded" and exp["results"]["n_anomalies"] > 0

    page = client.get("/api/v1/anomalies", params={"sort": "statistical_score"}).json()
    assert page["total"] == exp["results"]["n_anomalies"]
    scores = [a["statistical_score"] for a in page["items"]]
    assert scores == sorted(scores, reverse=True)

    only_vol = client.get("/api/v1/anomalies", params={"anomaly_type": "volume_liquidity"}).json()
    assert only_vol["total"] >= 1 and all(a["anomaly_type"] == "volume_liquidity" for a in only_vol["items"])
    searched = client.get("/api/v1/anomalies", params={"search": "syn-c"}).json()
    assert all("SYN-C" in (a["symbol"], a["related_symbol"]) for a in searched["items"])
    assert client.get("/api/v1/anomalies", params={"synthetic": False}).json()["total"] == 0

    pair = client.get("/api/v1/anomalies", params={"detector": "pair_divergence"}).json()["items"][0]
    d = client.get(f"/api/v1/anomalies/{pair['id']}").json()
    assert d["reproduced"] is True
    assert d["related_context_bars"] and d["reference_distribution"]["values_n"] > 0
    assert d["dataset"]["is_synthetic"] is True

    top = page["items"][0]
    d2 = client.get(f"/api/v1/anomalies/{top['id']}").json()
    assert d2["reproduced"] is True
    assert any(b["post_event"] for b in d2["context_bars"]) and not all(b["post_event"] for b in d2["context_bars"])
    facets = client.get("/api/v1/anomalies/facets").json()
    assert "extreme_return" in facets["anomaly_type"]


def test_saved_queries_crud(client):
    r = client.post("/api/v1/saved-queries", json={"name": "big crypto moves", "filters": {"asset_class": ["crypto"]}})
    assert r.status_code == 201
    qid = r.json()["id"]
    assert [q["id"] for q in client.get("/api/v1/saved-queries").json()] == [qid]
    assert client.delete(f"/api/v1/saved-queries/{qid}").status_code == 204


def test_data_health_and_catalog(client):
    _ingest(client, ("SYN-A",))
    h = client.get("/api/v1/data/health").json()
    assert h["providers"][0]["id"] == "synthetic" and h["providers"][0]["datasets"] == 1
    assert h["datasets"][0]["coverage"] == 1.0
    assert len(client.get("/api/v1/providers/catalog").json()) == 6


def test_provider_failure_is_reported_not_faked(client):
    r = client.post("/api/v1/data/ingest", json={"items": [
        {"adapter": "local_file", "symbol": "X", "asset_class": "equity", "options": {"path": "/missing.csv"}}]})
    assert r.status_code == 502
    prov = client.get("/api/v1/providers").json()
    assert prov[0]["last_error"] and "File not found" in prov[0]["last_error"]
    assert client.get("/api/v1/datasets").json() == []


def test_upload_csv(client, tmp_path):
    rows = ["date,open,high,low,close,volume"]
    for i in range(300):
        d = (datetime(2023, 1, 2) + timedelta(days=i)).strftime("%Y-%m-%d")
        rows.append(f"{d},{100+i%7},{102+i%7},{99+i%7},{101+i%7},{1000+i}")
    r = client.post("/api/v1/data/upload", files={"file": ("btc.csv", "\n".join(rows), "text/csv")},
                    data={"symbol": "BTC-LOCAL", "asset_class": "crypto"})
    assert r.status_code == 200, r.text
    ds = r.json()["ingested"][0]
    assert ds["quality_status"] in ("pass", "warn")
    bars = client.get(f"/api/v1/datasets/{ds['dataset_id']}/bars", params={"limit": 5}).json()
    assert len(bars["bars"]) == 5 and bars["is_synthetic"] is False


def test_async_jobs_idempotency_and_cancel(client):
    body = {"items": [{"adapter": "synthetic", "symbol": "SYN-D", "asset_class": "other"}], "run_async": True,
            "idempotency_key": "k1"}
    j1 = client.post("/api/v1/data/ingest", json=body).json()["job"]
    j2 = client.post("/api/v1/data/ingest", json=body).json()["job"]
    assert j1["id"] == j2["id"] and j1["status"] == "queued"
    j3 = client.post("/api/v1/jobs", json={"kind": "scan", "payload": {"dataset_ids": []}}).json()
    assert client.post(f"/api/v1/jobs/{j3['id']}/cancel").json()["status"] == "cancelled"
    assert process_once() == 1
    done = client.get(f"/api/v1/jobs/{j1['id']}").json()
    assert done["status"] == "succeeded" and done["result"]["ingested"][0]["symbol"] == "SYN-D"
    assert client.post(f"/api/v1/jobs/{j1['id']}/cancel").status_code == 409


def test_failed_job_retries_then_fails(client):
    j = client.post("/api/v1/jobs", json={"kind": "ingest", "payload": {"items": [
        {"adapter": "local_file", "symbol": "X", "asset_class": "equity", "options": {"path": "/missing.csv"}}]}}).json()
    far = datetime.now(UTC) + timedelta(days=1)
    for _ in range(3):
        process_once(now=far)
        far += timedelta(days=1)
    job = client.get(f"/api/v1/jobs/{j['id']}").json()
    assert job["status"] == "failed" and job["attempts"] == 3 and "All ingestions failed" in job["error"]


def test_schedule_downloads_and_scans_automatically(client):
    payload = {"items": [{"adapter": "synthetic", "symbol": s, "asset_class": "other"} for s in ("SYN-A", "SYN-B")],
               "pairs": [["SYN-B", "SYN-A"]]}
    r = client.post("/api/v1/schedules", json={"name": "daily synthetic", "payload": payload, "interval_seconds": 3600})
    assert r.status_code == 201
    sid = r.json()["id"]
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert process_once(now=now) == 1
    assert process_once(now=now) == 0  # same slot is not enqueued twice
    jobs = client.get("/api/v1/jobs").json()
    assert jobs[0]["schedule_id"] == sid and jobs[0]["status"] == "succeeded"
    assert jobs[0]["result"]["n_anomalies"] > 0
    sch = client.get("/api/v1/schedules").json()[0]
    assert datetime.fromisoformat(sch["next_run_at"]).replace(tzinfo=UTC) > now
    # Next slot: data unchanged -> scan served from the cache (no duplicate experiment).
    assert process_once(now=now + timedelta(hours=1, seconds=1)) == 1
    exps = client.get("/api/v1/experiments").json()
    assert len(exps) == 1
    assert client.patch(f"/api/v1/schedules/{sid}", json={"enabled": False}).json()["enabled"] is False
    assert client.delete(f"/api/v1/schedules/{sid}").status_code == 204


def test_audit_log_records_actions(client):
    _ingest(client, ("SYN-A",))
    actions = {e["action"] for e in client.get("/api/v1/audit").json()}
    assert "dataset_created" in actions
