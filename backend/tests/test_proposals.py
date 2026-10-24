"""Dynamic hypothesis proposals: valid contracts, evidence attached, nothing run, duplicates flagged."""

from fastapi.testclient import TestClient
import pytest

from qal.research.contract import HypothesisContract


@pytest.fixture()
def client(env):
    from qal.api.main import create_app

    with TestClient(create_app()) as c:
        yield c


def _ingest(client, symbols=("SYN-A", "SYN-B", "SYN-D")):
    r = client.post("/api/v1/data/ingest", json={"items": [
        {"adapter": "synthetic", "symbol": s, "asset_class": "other"} for s in symbols]})
    return {x["symbol"]: x["dataset_id"] for x in r.json()["ingested"]}


def test_no_data_means_no_proposals(client):
    assert client.get("/api/v1/research/proposals").json() == []


def test_proposals_are_valid_contracts_built_from_the_data(client, monkeypatch):
    from qal.research import proposals

    monkeypatch.setattr(proposals, "MIN_EVENTS", 3)  # the short synthetic series have only a few shocks
    ds = _ingest(client)
    client.post("/api/v1/scans", json={"dataset_ids": list(ds.values())})
    props = client.get("/api/v1/research/proposals").json()
    kinds = {p["kind"] for p in props}
    assert "momentum" in kinds and "event" in kinds
    for p in props:
        HypothesisContract.model_validate(p["contract"])
        assert p["is_synthetic"] is True and p["title"].startswith("SYNTHETIC")
        assert p["why"] and p["variants"] >= 1 and p["existing_hypotheses"] == []
    ev = next(p for p in props if p["kind"] == "event")
    assert ev["evidence"]["shocks"] >= 3 and ev["contract"]["source_anomaly_ids"]
    # Proposing runs nothing and registers nothing.
    hyps = client.get("/api/v1/hypotheses").json()
    assert (hyps if isinstance(hyps, list) else hyps["items"]) == []
    # Deterministic: same data, same list.
    assert [p["id"] for p in client.get("/api/v1/research/proposals").json()] == [p["id"] for p in props]


def test_registered_proposal_is_flagged_and_sinks(client):
    ds = _ingest(client)
    props = client.get("/api/v1/research/proposals").json()
    first = props[0]
    r = client.post("/api/v1/hypotheses", json={"contract": first["contract"]})
    assert r.status_code == 201, r.text
    again = client.get("/api/v1/research/proposals").json()
    same = next(p for p in again if p["id"] == first["id"])
    assert same["existing_hypotheses"][0]["id"] == r.json()["id"]
    assert again[-1]["id"] == first["id"] or all(p["existing_hypotheses"] for p in again[again.index(same):])
