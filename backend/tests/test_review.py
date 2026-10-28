"""Phase 13: paper trading results review and strategy readiness decision. Integrity checks, metrics with sample
sizes, uncertainty, comparison compatibility, original criteria and amendments, deterministic decisions with
history, manual reviews, reports, reproducibility and isolation from real execution.

The data is synthetic; tests that need "real" data flag the synthetic datasets as non-synthetic inside the test
database only. Nothing here is evidence about any market."""

from __future__ import annotations

import ast
import pathlib

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from qal import db
from qal.models import Experiment, PaperFill, PaperOrder, PaperPosition, PaperSession
from qal.paper import engine
from qal.program import service as program
from qal.review import (
    criteria,
    decision,
    integrity,
    metrics,
    report,
    service,
    uncertainty,
)
from tests.test_lab import lab, run_new  # noqa: F401  (lab is a fixture)
from tests.test_program import (  # noqa: F401  (labexp: fixture)
    JUST,
    WHY,
    admit,
    as_real,
    labexp,
    proto,
    started,
)


def finished(session, labexp, **over):  # noqa: F811
    prog = started(session, labexp, **over)
    program.run(session, prog.id, 5000)
    session.commit()
    return prog


def review(session, by="ana"):
    rows = service.run(session, by)
    session.commit()
    return rows


def sub(prog):
    return service.subject_id("program", prog.id)


# -- inventory and evidence classes ----------------------------------------------------------------------------------
def test_inventory_and_insufficient_evidence_without_sessions(session, labexp):  # noqa: F811
    summ = service.phase_summary(session)
    assert summ["status"] == "INSUFFICIENT_EVIDENCE" and summ["subjects"] == []
    assert any("no paper trading session" in x for x in summ["to_collect"])
    assert any("prospective evidence" in x for x in summ["to_collect"])
    assert review(session) == []  # nothing is fabricated


def test_replay_synthetic_and_prospective_are_kept_apart(session, labexp, monkeypatch):  # noqa: F811
    syn = finished(session, labexp)
    as_real(session, labexp)
    cand = admit(session, labexp, name="Momentum real-flagged")
    a = program.start(session, proto(session, cand, mode="A").id, "ana")
    session.commit()
    program.run(session, a.id, 80)
    session.commit()
    rows = {r["subject"]: r for r in review(session)}
    assert rows[sub(syn)]["evidence_class"] == "SYNTHETIC"
    assert rows[sub(a)]["evidence_class"] == "HISTORICAL"  # replayed bar by bar, but already known
    rec = service.latest(session, sub(a))
    inv = rec.config["inventory"]
    for k in ("strategy", "version", "hypothesis", "protocol", "observed", "evaluation_mode", "markets", "assets",
              "timeframes", "observations", "signals", "trades", "independent_trades", "missing_or_inactive",
              "incidents", "backtest_reference", "out_of_sample", "paper", "costs_and_assumptions", "risk_limits",
              "alerts_pauses_state_changes"):
        assert k in inv, k
    assert inv["evidence"]["prospective_bars"] == 0
    assert "SUPPORTED" not in rec.config["decision"]["state"]


# -- integrity --------------------------------------------------------------------------------------------------------
def test_integrity_passes_on_a_clean_session_and_blocks_on_defects(session, labexp):  # noqa: F811
    prog = finished(session, labexp)
    b = service.bundle(session, session.get(PaperSession, prog.config["session_id"]), prog)
    integ = b["integrity"]
    by = {c["check"]: c for c in integ["checks"]}
    assert set(by) == {"no_lookahead", "temporal_order", "signals_vs_risk_decisions", "orders_vs_fills",
                       "reconciliation", "costs_applied", "no_duplicates", "open_positions_and_missing_data",
                       "configuration_matches_protocol", "code_and_parameter_changes"}
    for k in ("no_lookahead", "temporal_order", "orders_vs_fills", "reconciliation", "costs_applied", "no_duplicates",
              "configuration_matches_protocol"):
        assert by[k]["status"] == "PASS", by[k]
    assert by["no_lookahead"]["evidence"]["recomputed"] > 0
    assert integ["status"] in ("OK", "LIMITED")
    # a fill on the decision bar is lookahead; a filled quantity that does not match its fills is inconsistent
    ps = session.get(PaperSession, prog.config["session_id"])
    f = session.scalars(select(PaperFill).where(PaperFill.session_id == ps.id).order_by(PaperFill.bar_ts)).first()
    o = session.get(PaperOrder, f.order_id)
    o.filled_quantity += 1.0
    session.commit()
    rt = engine.load_runtime(session, ps)
    assert integrity.orders_vs_fills(session, ps)["status"] == "FAIL"
    o.filled_quantity -= 1.0
    f.bar_ts = o.created_ts - pd.Timedelta(days=1)
    session.commit()
    lk = integrity.no_lookahead(session, ps, rt)
    assert lk["status"] == "FAIL" and lk["material"]
    rows = review(session)
    row = next(r for r in rows if r["subject"] == sub(prog))
    assert row["integrity"] == "BLOCKED" and row["state"] == "PAUSED_REVIEW"  # results are not evaluated as evidence


def test_reconciliation_and_parameter_changes_are_detected(session, labexp):  # noqa: F811
    prog = finished(session, labexp)
    ps = session.get(PaperSession, prog.config["session_id"])
    ps.cash += 500.0
    session.commit()
    assert integrity.reconciliation(session, ps)["status"] == "FAIL"
    ps.cash -= 500.0
    ps.config_history = [*ps.config_history, {"version": 2, "change": "order_type -> limit", "by": "x"}]
    session.commit()
    ch = integrity.changes_during_period(session, ps, prog)
    assert ch["status"] == "FAIL" and ch["material"]


def test_missing_and_defective_data_limit_the_evaluation(session, labexp):  # noqa: F811
    prog = finished(session, labexp)
    prog.results = {**prog.results, "missing_bars": 40}  # as if 40 bars had been missing or skipped
    session.commit()
    ps = session.get(PaperSession, prog.config["session_id"])
    b = service.bundle(session, ps, prog)
    chk = next(c for c in b["integrity"]["checks"] if c["check"] == "open_positions_and_missing_data")
    assert chk["status"] == "FAIL" and not chk["material"] and chk["evidence"]["missing_bars"] == 40
    assert b["integrity"]["status"] == "LIMITED"
    crit = {c["criterion"]: c for c in b["criteria"]["protocol_criteria"]}
    assert crit["missing_data"]["met"] is False
    if b["diagnosis"]["triggered"]:
        data = next(c for c in b["diagnosis"]["causes"] if c["cause"] == "data quality or latency")
        assert data["status"] == "CONFIRMED"
    row = next(r for r in review(session) if r["subject"] == sub(prog))
    assert row["state"] != "SUPPORTED_FOR_FURTHER_RESEARCH" and row["state"] in ("INCONCLUSIVE", "REJECTED")


# -- metrics ----------------------------------------------------------------------------------------------------------
def test_metrics_definitions_and_insufficient_samples(session, labexp):  # noqa: F811
    m = metrics.metric(None, 3, min_n=5)
    assert m["status"] == "INSUFFICIENT_DATA" and m["value"] is None and "needs at least 5" in m["note"]
    assert metrics.metric(0.1, 10, min_n=5)["status"] == "OK"
    dd = metrics.drawdown_episodes(np.array([0.01, -0.02, -0.01, 0.05, -0.01]))
    assert dd["episodes"] == 2 and dd["max_bars"] == 2 and dd["current_bars"] == 1 and not dd["recovered"]
    prog = started(session, labexp)
    program.run(session, prog.id, 12)
    session.commit()
    b = service.bundle(session, session.get(PaperSession, prog.config["session_id"]), prog)
    mm = b["metrics"]
    assert mm["sample"]["bars"] == 12
    assert mm["risk"]["sharpe_ann"]["status"] == "INSUFFICIENT_DATA"  # 12 bars: withheld, not zero
    assert mm["performance"]["profit_factor"]["status"] == "INSUFFICIENT_DATA"
    assert b["uncertainty"]["bootstrap"]["status"] == "INSUFFICIENT_DATA"


def test_pnl_costs_and_open_trades(session, labexp):  # noqa: F811
    prog = finished(session, labexp)
    ps = session.get(PaperSession, prog.config["session_id"])
    b = service.bundle(session, ps, prog)
    perf, ex = b["metrics"]["performance"], b["metrics"]["execution"]
    fills = session.scalars(select(PaperFill).where(PaperFill.session_id == ps.id)).all()
    costs = ex["costs_breakdown"]["value"]
    assert costs["commission"] == pytest.approx(sum(f.commission for f in fills))
    assert perf["pnl_gross"]["value"] - perf["pnl_net"]["value"] == pytest.approx(sum(costs.values()))
    assert perf["pnl_net"]["value"] == pytest.approx(ps.summary["equity"] - ps.initial_capital, rel=1e-9)
    s_ = b["metrics"]["sample"]
    open_now = any(abs(x.quantity) > 1e-12 for x in session.scalars(
        select(PaperPosition).where(PaperPosition.session_id == ps.id)))
    assert s_["trade_open_at_end"] == open_now
    if open_now:  # the open trade is valued at the last close and kept out of the closed-trade statistics
        assert s_["trades_closed"] == b["inventory"]["trades"] - 1


# -- uncertainty ------------------------------------------------------------------------------------------------------
def test_uncertainty_dependence_independent_trades_and_holm():
    rng = np.random.default_rng(3)
    iid = rng.normal(0, 0.01, 600)
    dep = np.repeat(rng.normal(0, 0.01, 60), 10)
    assert uncertainty.effective_n(dep) < uncertainty.effective_n(iid)
    tr = [{"entry_ts": "2024-01-01", "exit_ts": "2024-01-05", "bars": 4},
          {"entry_ts": "2024-01-03", "exit_ts": "2024-01-08", "bars": 5},
          {"entry_ts": "2024-01-10", "exit_ts": "2024-01-12", "bars": 2}]
    it = uncertainty.independent_trades(tr)
    assert it == {**it, "trades": 3, "independent": 2, "overlapping": 1}
    adj = uncertainty.holm({"a": 0.01, "b": 0.04, "c": None, "d": 0.03})
    assert adj["c"] is None and adj["a"] == pytest.approx(0.03) and adj["d"] == pytest.approx(0.06)
    assert adj["b"] == pytest.approx(0.06)  # monotone step-down
    u = uncertainty.analyse(iid, iid, tr, 252)
    assert u["excess_test"]["p_one_sided"] is not None and "not the probability" in u["excess_test"]["note"]
    assert u["sharpe"]["interpretable"] in (True, False) and u["block_bars"] >= 5
    small = uncertainty.analyse(iid[:40], iid[:40], tr, 252)
    assert small["sharpe"]["interpretable"] is False and small["excess_ci95"] is None


# -- comparison --------------------------------------------------------------------------------------------------------
def test_incompatible_configurations_are_not_compared(session, labexp):  # noqa: F811
    prog = finished(session, labexp)
    b = service.bundle(session, session.get(PaperSession, prog.config["session_id"]), prog)
    comp = b["comparison"]
    assert comp["comparable"] and {c["check"] for c in comp["compatibility"]} >= {
        "strategy_version", "universe_and_assets", "timeframe", "horizon", "capital_and_position_size", "cost_model",
        "execution_assumptions", "pnl_method", "period_and_data", "regime"}
    assert "context only" in comp["research_oos_context"]["note"]
    from qal.review import comparison

    cmp = {"conventions": [{"check": "allocation", "passed": False, "detail": "0.2 (candidate 0.3)"}], "returns": {}}
    out = comparison.build(cmp, {"bars": 100}, {}, {"timeframe": "1d", "results": {}}, {"baseline": "no_trade",
                                                                                        "tracking_tolerance_bps": 2})
    assert not out["comparable"] and out["backtest"] is None and "Not comparable" in out["note"]


# -- criteria, amendments, decisions ----------------------------------------------------------------------------------
def _ctx(**over):
    base = {"has_protocol": True, "evidence": {"class": "PROSPECTIVE", "prospective_share": 1.0, "prospective_bars": 300},
            "integrity": {"status": "OK", "blocking": []}, "program_status": "FINISHED",
            "criteria": {"reject_triggered": [], "unmet": [], "met": ["sample_complete"]},
            "readiness": [{"check": "independent_trades", "met": True, "detail": ""},
                          {"check": "stable_enough", "met": True, "detail": ""},
                          {"check": "no_invalidating_incident", "met": True, "detail": ""}],
            "open_pauses": 0, "admission_kind": "validated", "is_synthetic": False, "adjusted_p": 0.01}
    return {**base, **over}


def test_decision_engine_rules_are_deterministic():
    d = decision.decide
    assert d(_ctx())["state"] == "SUPPORTED_FOR_FURTHER_RESEARCH"
    assert d(_ctx()) == d(_ctx())
    assert d(_ctx(has_protocol=False))["state"] == "INCONCLUSIVE"
    assert d(_ctx(integrity={"status": "BLOCKED", "blocking": ["reconciliation"]}))["state"] == "PAUSED_REVIEW"
    assert d(_ctx(criteria={"reject_triggered": ["drawdown_stop"], "unmet": []}))["state"] == "REJECTED"
    assert d(_ctx(open_pauses=1))["state"] == "PAUSED_REVIEW"
    assert d(_ctx(program_status="RUNNING"))["state"] == "CONTINUE_PAPER"
    assert d(_ctx(program_status="STOPPED_EARLY"))["state"] == "INCONCLUSIVE"
    assert d(_ctx(criteria={"reject_triggered": [], "unmet": ["primary_excess_over_baseline"]}))["state"] == "INCONCLUSIVE"
    # the protocol finished with its criteria met, but the evidence cannot support the hypothesis
    for over in ({"evidence": {"class": "HISTORICAL", "prospective_share": 0}}, {"is_synthetic": True},
                 {"admission_kind": "exploratory"}, {"adjusted_p": 0.2}, {"adjusted_p": None},
                 {"readiness": [{"check": "stable_enough", "met": False, "detail": "halves differ"}]}):
        out = d(_ctx(**over))
        assert out["state"] == "COMPLETED", over
    out = d(_ctx())
    for k in ("state", "reason", "rules", "recommended_actions", "uncertainties", "engine_version",
              "not_an_authorisation"):
        assert k in out
    assert "No state authorises real trading" in out["not_an_authorisation"]
    assert decision.ALLOWED_MANUAL["INCONCLUSIVE"] == ("INCONCLUSIVE", "PAUSED_REVIEW", "REJECTED")
    assert all(s in decision.ALLOWED_MANUAL[s] for s in decision.STATES)


def test_original_criteria_and_retrospective_amendments(session, labexp):  # noqa: F811
    prog = finished(session, labexp)
    review(session)
    first = service.latest(session, sub(prog))
    crit = first.config["criteria"]
    assert {c["criterion"] for c in crit["protocol_criteria"]} >= {"sample_complete", "primary_excess_over_baseline",
                                                                 "backtest_consistency", "costs", "drawdown"}
    assert {r["check"] for r in crit["readiness_checks"]} == {
        "duration_complete", "minimum_observations", "independent_trades", "risk_limits_respected",
        "costs_within_assumptions", "no_invalidating_incident", "beats_baseline", "no_material_deviation_vs_backtest",
        "stable_enough"}
    pid = prog.config["protocol_id"]
    original = session.get(Experiment, pid).config_hash
    with pytest.raises(criteria.AmendmentError, match="agents"):
        criteria.amend(session, pid, criteria.AmendmentIn(by="agent:x", changes={"min_trades": 1},
                                                          justification="x" * 40, impact_expected="lower the bar on trades"))
    with pytest.raises(criteria.AmendmentError, match="cannot amend"):
        criteria.amend(session, pid, criteria.AmendmentIn(by="ana", changes={"mode": "B"}, justification="x" * 40,
                                                          impact_expected="switch mode"))
    a = criteria.amend(session, pid, criteria.AmendmentIn(
        by="ana", changes={"max_cost_ratio": 0.01, "reject_cost_ratio": 0.02},
        justification="Cost limits reconsidered after a broker fee schedule review, documented here.",
        impact_expected="costs criterion becomes stricter"))
    session.commit()
    assert a.config["post_hoc"] is True and a.config["previous_values"]["max_cost_ratio"] == 1.5
    assert session.get(Experiment, pid).config_hash == original  # the original protocol is kept
    review(session)
    rec = service.latest(session, sub(prog))
    am = rec.config["amendments"][0]
    assert am["used_for_decision"] is False and am["post_hoc"] is True
    assert rec.config["decision"]["state"] == first.config["decision"]["state"]  # decided on the original protocol
    assert rec.id != first.id and rec.config["previous_decision_id"] == first.id  # history kept


def test_synthetic_program_classification_history_and_manual_review(session, labexp):  # noqa: F811
    prog = finished(session, labexp)
    rows = review(session)
    row = next(r for r in rows if r["subject"] == sub(prog))
    assert row["stored"] and row["state"] in ("COMPLETED", "INCONCLUSIVE", "REJECTED")
    assert row["state"] != "SUPPORTED_FOR_FURTHER_RESEARCH"  # synthetic and exploratory: never support
    again = next(r for r in review(session) if r["subject"] == sub(prog))
    assert not again["stored"] and again["decision_id"] == row["decision_id"]  # same outputs: no duplicate record
    rec = service.get_decision(session, row["decision_id"])
    with pytest.raises(service.ReviewError, match="agents and LLMs"):
        service.manual_review(session, rec.id, "llm:assistant", row["state"], "An LLM tries to change the state here.")
    better = "SUPPORTED_FOR_FURTHER_RESEARCH"
    with pytest.raises(service.ReviewError, match="never less"):
        service.manual_review(session, rec.id, "ana", better, "Trying to classify above the engine, which must fail.")
    m = service.manual_review(session, rec.id, "ana", "REJECTED", "Reviewed: the costs assumption is not credible.")
    session.commit()
    assert m.config["engine_state"] == row["state"]
    cur = service.current_state(session, sub(prog))
    assert cur["state"] == "REJECTED" and cur["source"] == "manual review"
    assert rec.config["decision"]["state"] == row["state"]  # the engine's decision is not overwritten
    kinds = [h.kind for h in service.history(session, sub(prog))]
    assert kinds == [service.KIND, service.MANUAL_KIND]
    assert service.verify(rec) and service.verify(m)


def test_continue_and_early_stop_are_not_conclusive(session, labexp):  # noqa: F811
    prog = started(session, labexp)
    program.run(session, prog.id, 20)
    session.commit()
    row = next(r for r in review(session) if r["subject"] == sub(prog))
    assert row["state"] == "CONTINUE_PAPER"
    program.stop(session, prog.id, "ana", "stopping to test the review", True)
    session.commit()
    row = next(r for r in review(session) if r["subject"] == sub(prog))
    assert row["state"] in ("INCONCLUSIVE", "REJECTED") and row["bars"] == 20


# -- reports and reproducibility --------------------------------------------------------------------------------------
def test_report_sections_and_reproducibility(session, labexp):  # noqa: F811
    prog = finished(session, labexp)
    rows = review(session)
    did = next(r for r in rows if r["subject"] == sub(prog))["decision_id"]
    rec = service.get_decision(session, did)
    rep = report.build(session, rec)
    assert list(rep["sections"]) == ["1_hypothesis", "2_original_protocol", "3_evidence", "4_data_quality",
                                     "5_performance_and_risk", "6_comparison_backtest_and_baseline", "7_uncertainty",
                                     "8_operational_incidents", "9_criteria", "10_limitations", "11_final_state",
                                     "12_next_steps"]
    concl = rep["what_we_can_and_cannot_conclude"]
    assert concl["cannot_conclude"] and any("future" in x for x in concl["cannot_conclude"])
    md = report.markdown(rep)
    assert "What we can conclude and what we cannot conclude" in md and "## 9. Criteria" in md
    reg = service.regenerate(session, did)
    assert reg["reproducible"] is True, reg
    rec.config = {**rec.config, "decision": {**rec.config["decision"], "state": "SUPPORTED_FOR_FURTHER_RESEARCH"}}
    session.commit()
    assert not service.verify(rec)  # a tampered decision no longer matches its hash


def test_standalone_session_without_protocol_is_inconclusive(session, labexp):  # noqa: F811
    from qal.paper import service as paper
    from qal.paper.config import SessionConfig, StrategyIn

    cand = admit(session, labexp)
    ps = paper.create_session(session, SessionConfig(name="ad hoc", mode="replay",
                                                     strategies=[StrategyIn(strategy_id=f"P:{cand.id}",
                                                                            allocation=0.3)], created_by="ana"))
    paper.start(session, ps, "ana")
    session.commit()
    rt = engine.load_runtime(session, ps)
    for _ in range(30):
        engine.step(session, ps, rt)
    session.commit()
    row = next(r for r in review(session) if r["session_id"] == ps.id)
    assert row["subject_kind"] == "session" and row["state"] == "INCONCLUSIVE"
    rec = service.get_decision(session, row["decision_id"])
    assert "no protocol" in rec.config["decision"]["reason"]


# -- API ------------------------------------------------------------------------------------------------------------
def test_api_flow(env, lab):  # noqa: F811
    from qal.api.main import app

    with db.session_scope() as s:
        exp = run_new(s, lab)
        eid = exp.id
    with TestClient(app) as c:
        meta = c.get("/api/v1/review/meta").json()
        assert meta["live_trading_enabled"] is False and set(meta["states"]) == set(decision.STATES)
        assert c.get("/api/v1/review/summary").json()["status"] == "INSUFFICIENT_EVIDENCE"
        cid = c.post("/api/v1/program/candidates", json={"name": "Momentum API", "lab_experiment_id": eid,
                                                          "admitted_by": "ana", "justification": JUST,
                                                          "exploratory": True}).json()["id"]
        pid = c.post("/api/v1/program/protocols", json={"candidate_id": cid, "mode": "C", "created_by": "ana",
                                                         "rationale": WHY, "min_trades": 3,
                                                         "target_sharpe_se": 2.0}).json()["id"]
        prog = c.post("/api/v1/program/programs", json={"protocol_id": pid, "started_by": "ana"}).json()
        c.post(f"/api/v1/program/programs/{prog['id']}/run", json={"max_bars": 5000, "run_async": False})
        r = c.post("/api/v1/review/run", json={"by": "ana"})
        assert r.status_code == 200, r.text
        rows = c.get("/api/v1/review/strategies").json()
        assert len(rows) == 1 and rows[0]["evidence_class"] == "SYNTHETIC"
        assert c.get("/api/v1/review/strategies", params={"state": "SUPPORTED_FOR_FURTHER_RESEARCH"}).json() == []
        assert len(c.get("/api/v1/review/strategies", params={"asset": rows[0]["assets"][0]}).json()) == 1
        did = rows[0]["decision_id"]
        d = c.get(f"/api/v1/review/decisions/{did}").json()
        assert d["report"]["sections"]["11_final_state"]["engine"]["state"] == rows[0]["state"]
        assert c.get(f"/api/v1/review/decisions/{did}/report.md").status_code == 200
        assert c.get(f"/api/v1/review/decisions/{did}/regenerate").json()["reproducible"] is True
        r = c.post(f"/api/v1/review/decisions/{did}/manual", json={"by": "agent:x", "state": rows[0]["state"],
                                                                   "rationale": "An agent cannot do this at all."})
        assert r.status_code == 400
        r = c.post(f"/api/v1/review/decisions/{did}/manual", json={"by": "ana", "state": rows[0]["state"],
                                                                   "rationale": "Confirmed after reading the report."})
        assert r.status_code == 201, r.text
        hist = c.get("/api/v1/review/history", params={"subject": rows[0]["subject"]}).json()
        assert [h["kind"] for h in hist] == ["engine", "manual"]
        inv = c.get("/api/v1/review/inventory").json()
        assert inv[0]["evaluation_mode"] == "SYNTHETIC"
        r = c.post(f"/api/v1/review/protocols/{pid}/amendments", json={
            "by": "ana", "changes": {"min_trades": 4}, "justification": "y" * 45, "impact_expected": "slightly more"})
        assert r.status_code == 201, r.text
        assert c.get(f"/api/v1/review/protocols/{pid}/amendments").json()[0]["post_hoc"] is True


# -- isolation -------------------------------------------------------------------------------------------------------
def test_no_real_orders_no_credentials():
    forbidden = {"requests", "httpx", "urllib", "socket", "websocket", "websockets", "ccxt", "MetaTrader5", "yfinance",
                 "aiohttp", "ib_insync", "alpaca", "oandapyV20", "smtplib"}
    pkg = pathlib.Path(service.__file__).parent
    for f in pkg.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for n in names:
                assert n.split(".")[0] not in forbidden, f"{f.name} imports {n}"
        for word in ("api_key", "apikey", "secret", "password", "token"):
            assert word not in text.lower(), f"{f.name} mentions {word}"
        assert "place_order" not in text and "submit_order" not in text
    from qal.review import PAPER_ONLY

    assert PAPER_ONLY is True
    assert not any(w in f for f in criteria.AmendmentIn.model_fields for w in ("key", "secret", "account", "broker"))
