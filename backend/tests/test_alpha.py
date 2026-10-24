"""Alpha discovery engine: agents, factor and relationship routes, budget, states and promotion rules."""

import numpy as np
import pandas as pd
import pytest

from qal.alpha import agents, factors, relationships
from qal.alpha.messages import Budget
from qal.anomaly.engine import ScanConfig, run_scan
from qal.data.store import ParquetStore
from qal.models import AgentMessage, Dataset, Experiment, Hypothesis
from qal.research.contract import HypothesisContract
from qal.research.promotion import (
    PaperApproval,
    RiskLimits,
    eligibility,
    promote_paper_eligible,
)
from qal.research.service import transition
from qal.research.states import TransitionError
from tests.test_predictive import _load, _mt5_csv


def _universe(session, env, tmp_path, n=1700, coint_pair=True):
    ids = {}
    for i, sym in enumerate(("EURUSD", "GBPUSD", "AUDUSD")):
        ids[sym] = _load(session, _mt5_csv(tmp_path / f"{sym}.csv", n, i + 1), sym)["dataset_id"]
    if coint_pair:  # a series that tracks EURUSD with a mean-reverting gap: cointegrated by construction
        df = ParquetStore.read(session.get(Dataset, ids["EURUSD"]).path)
        rng = np.random.default_rng(9)
        gap = np.zeros(len(df))
        for t in range(1, len(df)):
            gap[t] = 0.8 * gap[t - 1] + rng.normal(0, 0.002)
        path = tmp_path / "EURGBP.csv"
        _mt5_csv(path, n, 4)
        raw = pd.read_csv(path, sep="\t")
        close = df["close"].to_numpy() * np.exp(gap)
        raw["<CLOSE>"] = close.round(5)
        raw["<OPEN>"] = np.r_[close[0], close[:-1]].round(5)
        raw["<HIGH>"] = np.maximum(raw["<OPEN>"], raw["<CLOSE>"]) * 1.001
        raw["<LOW>"] = np.minimum(raw["<OPEN>"], raw["<CLOSE>"]) * 0.999
        raw.to_csv(path, sep="\t", index=False)
        ids["EURGBP"] = _load(session, path, "EURGBP")["dataset_id"]
    session.flush()
    run_scan(session, ScanConfig(dataset_ids=list(ids.values())), store=ParquetStore(env.data_dir), actor="test")
    session.flush()
    return ids


def _decisions(session, run):
    return [m.payload for m in session.query(AgentMessage).filter_by(run_id=run.id, kind="decision")
            .order_by(AgentMessage.seq)]


def test_cycle_generates_valid_contracts_from_all_routes(env, session, tmp_path):
    _universe(session, env, tmp_path)
    run = agents.ResearchCoordinator().run_cycle(session, Budget(max_experiments=0))
    msgs = session.query(AgentMessage).filter_by(run_id=run.id).order_by(AgentMessage.seq).all()
    ideas = [m.payload for m in msgs if m.kind == "idea"]
    assert {i["route"] for i in ideas} == {"anomaly", "prior", "factor", "relationship"}
    assert all(i["origin_observation"] and i["refutation"] for i in ideas)
    # Every route other than "prior" carries evidence that points to a stored record.
    for i in ideas:
        assert i["route"] == "prior" or i["evidence"]
        for e in i["evidence"]:
            assert e["ref"]
    hyps = session.query(Hypothesis).all()
    assert hyps and len(hyps) == run.results["n_registered"]
    for h in hyps:
        c = HypothesisContract.model_validate(h.contract)
        assert c.origin in ("anomaly", "prior", "factor", "relationship") and c.origin_observation
        assert c.success.metric == "sharpe_net" and c.success.min_trades >= 30  # pre-registered before any test
        assert c.validation.embargo_bars >= c.horizon_bars
        assert h.created_by == "agent:quant_analyst" and h.status == "HYPOTHESIS_FORMULATED"
    gaps = " ".join(m.payload["reason"] for m in msgs if m.kind == "data_gap")
    assert "point-in-time fundamentals" in gaps and "futures" in gaps.lower() and "tick volume" in gaps
    assert run.results["llm_tokens"] == 0


def test_second_cycle_detects_duplicates(env, session, tmp_path):
    _universe(session, env, tmp_path)
    first = agents.ResearchCoordinator().run_cycle(session, Budget(max_experiments=0))
    session.flush()
    n = session.query(Hypothesis).count()
    second = agents.ResearchCoordinator().run_cycle(session, Budget(max_experiments=0))
    assert second.results["n_registered"] == 0
    assert second.results["n_duplicates"] == first.results["n_registered"]
    assert session.query(Hypothesis).count() == n
    dup = [d for d in _decisions(session, second) if d["action"] == "duplicate"]
    assert all(d["hypothesis_id"] for d in dup)


def test_ideas_and_scans_are_reproducible(env, session, tmp_path):
    _universe(session, env, tmp_path)
    b = Budget()
    ctx1, ctx2 = agents.build_context(session, b), agents.build_context(session, b)
    assert ctx1.relationship_scan.id == ctx2.relationship_scan.id  # cached by data content
    i1, g1 = agents.AlphaResearcher().propose(ctx1)
    i2, g2 = agents.AlphaResearcher().propose(ctx2)
    assert [i.model_dump() for i in i1] == [i.model_dump() for i in i2] and g1 == g2
    f1 = [agents.QuantAnalyst().formalize(ctx1, i).model_dump() for i in i1]
    assert f1 == [agents.QuantAnalyst().formalize(ctx2, i).model_dump() for i in i2]


def test_insufficient_evidence_is_rejected_and_negatives_persist(env, session, tmp_path):
    _universe(session, env, tmp_path)
    run = agents.ResearchCoordinator().run_cycle(session, Budget(max_experiments=3))
    dec = _decisions(session, run)
    rejected = [d for d in dec if d["action"] == "rejected_by_review"]
    assert rejected and all("insufficient" in d["reason"] for d in rejected)
    assert all(d["hypothesis_id"] is None for d in rejected)
    # Objections of rejected ideas are kept even though no hypothesis exists for them.
    keys = {d["key"] for d in rejected}
    obj = session.query(AgentMessage).filter(AgentMessage.kind == "objection",
                                             AgentMessage.subject_key.in_(keys)).all()
    assert obj and all(o.hypothesis_id is None for o in obj)
    # Experiments ran through the validation engine; failed ones stay stored with their results.
    res = session.query(AgentMessage).filter_by(run_id=run.id, kind="experiment_result").all()
    assert len(res) == run.results["n_experiments"] == 3
    for r in res:
        exp = session.get(Experiment, r.payload["experiment_id"])
        assert exp.kind == "validation" and exp.results
        h = session.get(Hypothesis, r.hypothesis_id)
        assert h.status in ("VALIDATION_FAILED", "VALIDATION_PASSED")
        assert r.evidence[0]["values"]["decision"] == h.status  # copied from the stored result
    assert run.results["n_validation_failed"] + run.results["n_validation_passed"] == 3


def test_validation_keeps_train_test_and_holdout_apart(env, session, tmp_path):
    _universe(session, env, tmp_path)
    run = agents.ResearchCoordinator().run_cycle(session, Budget(max_experiments=6))
    exps = session.query(Experiment).filter_by(kind="validation").all()
    assert exps
    for e in exps:
        r = e.results
        assert all(c["passed"] for c in r["leakage_checks"]), r["leakage_checks"]
        for w in r["windows"]:
            assert w["train_end"] + e.config["contract"]["validation"]["embargo_bars"] <= w["test_start"]
        assert r["holdout_bars"] > 0 and r["windows"][-1]["test_end"] <= r["research_bars"]
    # The relationship scan stopped at the same instant the pair hypothesis' holdout starts.
    rel = session.get(Experiment, run.results["relationship_scan_id"]).results
    pair = next(h for h in session.query(Hypothesis).all() if h.contract["origin"] == "relationship")
    row = next(p for p in rel["pairs"] if p.get("tradable_reversion"))
    assert pd.Timestamp(pair.contract["validation"]["holdout_start"]) == pd.Timestamp(row["research_end"])
    assert pd.Timestamp(row["to"]) < pd.Timestamp(row["research_end"])


def test_relationship_scan_corrects_every_test(env, session, tmp_path):
    _universe(session, env, tmp_path)
    exp = relationships.run_relationship_scan(session)
    ok = [p for p in exp.results["pairs"] if p["status"] == "ok"]
    assert len(ok) == 6 and exp.n_tests == 18  # 4 instruments -> 6 pairs x 3 tests, all recorded
    for p in ok:
        assert p["coint_adj_pvalue"] >= p["coint_pvalue"] - 1e-15
        assert p["corr_change_adj_pvalue"] >= p["corr_change_pvalue"] - 1e-15
    tradable = [(p["symbol_a"], p["symbol_b"]) for p in ok if p["tradable_reversion"]]
    assert tradable == [("EURGBP", "EURUSD")]  # the pair built to be cointegrated, and only that one
    # The holdout part of the data is never read: changing it leaves the scan's statistics unchanged.
    a = pd.Series(np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.01, 800))))
    b = a * np.exp(np.random.default_rng(2).normal(0, 0.002, 800))
    r1 = relationships.pair_tests(a[:640], b[:640])
    b2 = b.copy()
    b2[640:] *= 5
    assert r1 == relationships.pair_tests(a[:640], b2[:640])


def test_factor_modules_report_availability(env, session, tmp_path):
    st = {f["name"]: f for f in factors.factor_status(session)}
    assert not st["momentum"]["available"] and not st["value"]["available"]
    _universe(session, env, tmp_path)
    st = {f["name"]: f for f in factors.factor_status(session)}
    assert st["momentum"]["available"] and st["low_vol"]["available"]
    assert st["momentum"]["universes"][0]["symbols"] == ["AUDUSD", "EURGBP", "EURUSD", "GBPUSD"]
    for name in ("value", "quality"):
        assert not st[name]["available"] and "point-in-time" in st[name]["reason"]
    for f in st.values():
        assert f["formula"] and f["availability"] and f["rebalance"] and f["benchmark"] and f["validation"]


def test_budget_limits_and_no_llm_tokens(env, session, tmp_path):
    with pytest.raises(ValueError):
        Budget(llm_tokens=1000)
    _universe(session, env, tmp_path)
    run = agents.ResearchCoordinator().run_cycle(session, Budget(max_experiments=5, max_variants=3,
                                                                 max_hypotheses=4))
    assert run.results["n_variants_backtested"] <= 3 and run.results["n_registered"] <= 4
    dec = _decisions(session, run)
    assert any(d["action"] == "experiment_skipped" and "variant budget" in d["reason"] for d in dec)
    assert any(d["action"] == "deferred_budget" for d in dec)


def test_agents_cannot_promote_or_jump_states(env, session, tmp_path):
    _universe(session, env, tmp_path)
    agents.ResearchCoordinator().run_cycle(session, Budget(max_experiments=1))
    h = session.query(Hypothesis).filter(Hypothesis.status == "VALIDATION_FAILED").first()
    fresh = session.query(Hypothesis).filter(Hypothesis.status == "HYPOTHESIS_FORMULATED").first()
    for target in ("VALIDATION_PASSED", "IN_SAMPLE_EVALUATED", "OUT_OF_SAMPLE_EVALUATED", "PAPER_TRADING_ELIGIBLE"):
        with pytest.raises(TransitionError):
            transition(session, fresh, target, actor="agent:research_coordinator", actor_kind="agent", reason="x")
    with pytest.raises(TransitionError):  # no jumps, even for the policy engine
        transition(session, fresh, "VALIDATION_PASSED", actor="policy_engine", actor_kind="policy", reason="x")
    limits = RiskLimits(max_gross_exposure=0.5, max_drawdown_stop=0.1, max_daily_loss=0.02)
    with pytest.raises(TransitionError, match="only a person"):
        promote_paper_eligible(session, h, PaperApproval(approved_by="agent", actor_kind="agent",
                                                         risk_limits=limits, confirm=True))
    with pytest.raises(TransitionError, match="not eligible"):
        promote_paper_eligible(session, h, PaperApproval(approved_by="koptimusk", risk_limits=limits, confirm=True))
    el = eligibility(session, h)
    assert not el["eligible"]
    assert {r["requirement"] for r in el["requirements"] if not r["met"]} >= {
        "validation_passed", "holdout_tested_once", "explicit_costs"}
    # Paper trading itself and live trading stay unreachable.
    h.status = "PAPER_TRADING_ELIGIBLE"
    with pytest.raises(TransitionError, match="blocked"):
        transition(session, h, "PAPER_TRADING", actor="policy_engine", actor_kind="policy", reason="x")


def test_research_stages_are_recorded_in_order(env, session, tmp_path):
    from qal.models import AuditEvent

    _universe(session, env, tmp_path)
    agents.ResearchCoordinator().run_cycle(session, Budget(max_experiments=1))
    h = session.query(Hypothesis).filter(Hypothesis.status.in_(["VALIDATION_FAILED", "VALIDATION_PASSED"])).one()
    steps = [e.details["to"] for e in session.query(AuditEvent).filter_by(entity_id=h.id,
                                                                          action="hypothesis_transition")
             .order_by(AuditEvent.created_at)]
    assert steps == ["EXPERIMENT_PENDING", "IN_SAMPLE_EVALUATED", "OUT_OF_SAMPLE_EVALUATED", h.status]


def test_explicit_costs_reach_the_contract(env, session, tmp_path):
    from qal.research.costs import CostModel

    _universe(session, env, tmp_path)
    cm = CostModel(commission_bps=0.2, half_spread_bps=0.4, slippage_bps=0.1, borrow_bps_annual=0)
    run = agents.ResearchCoordinator().run_cycle(session, Budget(max_experiments=0),
                                                 costs={s: cm for s in ("EURUSD", "GBPUSD", "AUDUSD", "EURGBP")})
    for h in session.query(Hypothesis).all():
        assert set(h.contract["costs"]) == set(h.contract["dataset_ids"])
    codes = {m.payload["code"] for m in session.query(AgentMessage).filter_by(run_id=run.id, kind="objection")}
    assert "default_costs" not in codes


def test_api_runs_hypotheses_and_paper_eligibility(env, tmp_path):
    from fastapi.testclient import TestClient

    from qal import db
    from qal.api.main import create_app
    from qal.jobs.runner import process_once

    with db.session_scope() as s:
        _universe(s, env, tmp_path)
    with TestClient(create_app()) as c:
        f = {x["name"]: x for x in c.get("/api/v1/alpha/factors").json()}
        assert f["momentum"]["available"] and not f["value"]["available"]
        r = c.post("/api/v1/alpha/runs", json={"budget": {"max_experiments": 2}})
        assert r.status_code == 202, r.text
        assert c.post("/api/v1/alpha/runs", json={"budget": {"llm_tokens": 5}}).status_code == 422
        assert process_once() == 1
        runs = c.get("/api/v1/alpha/runs").json()
        assert len(runs) == 1 and runs[0]["status"] == "succeeded" and runs[0]["n_experiments"] == 2
        detail = c.get(f"/api/v1/alpha/runs/{runs[0]['id']}").json()
        assert {m["sender"] for m in detail["messages"]} == {"alpha_researcher", "quant_analyst",
                                                            "adversarial_reviewer", "research_coordinator"}
        rel = c.get(f"/api/v1/alpha/relationships/{runs[0]['relationship_scan_id']}").json()
        assert rel["n_tests"] == 18
        table = c.get("/api/v1/alpha/hypotheses").json()
        tested = [h for h in table if h["predictive_evidence"]["level"] != "not_tested"]
        assert len(tested) == 2 and all(h["research_priority"] is not None for h in table)
        assert all(not h["validated_net_profitability"]["established"] for h in table)
        h = c.get(f"/api/v1/alpha/hypotheses/{tested[0]['id']}").json()
        assert h["validations"][0]["metrics"]["sharpe_net"] is not None and h["objections"] and h["decisions"]
        assert h["gates"] and h["eligibility"]["eligible"] is False
        approval = {"approved_by": "koptimusk", "confirm": True,
                    "risk_limits": {"max_gross_exposure": 0.5, "max_drawdown_stop": 0.1, "max_daily_loss": 0.02}}
        r = c.post(f"/api/v1/alpha/hypotheses/{tested[0]['id']}/paper-eligibility", json=approval)
        assert r.status_code == 409 and "not eligible" in r.json()["detail"]
        r = c.post(f"/api/v1/alpha/hypotheses/{tested[0]['id']}/paper-eligibility",
                   json={**approval, "actor_kind": "agent"})
        assert r.status_code == 409 and "only a person" in r.json()["detail"]
        untested = next(x for x in table if x["predictive_evidence"]["level"] == "not_tested")
        assert c.post(f"/api/v1/alpha/hypotheses/{untested['id']}/experiments").status_code == 202
        assert process_once() == 1
        assert c.get(f"/api/v1/alpha/hypotheses/{untested['id']}").json()["validations"]
