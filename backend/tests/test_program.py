"""Phase 12: paper trading validation program. Candidate registry, pre-registered protocol, modes, simulated
execution and accounting, comparison with the backtest, statistical tracking, pause causes, audited resume,
classification and review, persistence, versioning and isolation from real execution.

The data is synthetic. Tests that need "real" data flag the synthetic datasets as non-synthetic inside the test
database only, to exercise the replay and prospective paths; nothing here is evidence about any market."""

from __future__ import annotations

import ast
import pathlib

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from qal import db
from qal.data.ingest import IngestRequest, ingest
from qal.models import (
    Dataset,
    Experiment,
    PaperFill,
    PaperLedger,
    PaperOrder,
    PaperPosition,
    PaperSession,
    PaperSignal,
)
from qal.paper import engine
from qal.paper import service as paper
from qal.program import classify, protocol, registry, service, tracking
from tests.test_lab import lab, run_new  # noqa: F401  (lab is a fixture)

JUST = "Admitted to exercise the validation program on test data; the evidence requirements are not met."
WHY = "Settings chosen before the run: a short horizon so the test data covers it."


@pytest.fixture()
def labexp(session, lab):  # noqa: F811
    exp = run_new(session, lab)
    assert exp.status == "COMPLETED"
    session.commit()
    return exp


def admit(session, exp, **over):
    body = {"name": "Momentum SYN-D", "lab_experiment_id": exp.id, "admitted_by": "ana", "justification": JUST,
            "exploratory": True, **over}
    rec = registry.admit(session, registry.AdmissionIn.model_validate(body))
    session.commit()
    return rec


def proto(session, cand, mode="C", **over):
    body = {"candidate_id": cand.id, "mode": mode, "created_by": "ana", "rationale": WHY, "min_trades": 3,
            "target_sharpe_se": 2.0, "min_observations_floor": 60, **over}
    p = protocol.create(session, protocol.ProtocolIn.model_validate(body))
    session.commit()
    return p


def started(session, exp, mode="C", cand_over=None, **over):
    cand = admit(session, exp, **(cand_over or {}))
    p = proto(session, cand, mode, **over)
    prog = service.start(session, p.id, "ana")
    session.commit()
    return prog


def as_real(session, exp):
    for r in exp.data_refs:
        session.get(Dataset, r["dataset_id"]).is_synthetic = False
    session.commit()


def fills_digest(session, sid):
    return [(f.instrument, str(f.bar_ts), f.side, round(f.quantity, 6), round(f.price, 9)) for f in session.scalars(
        select(PaperFill).where(PaperFill.session_id == sid).order_by(PaperFill.bar_ts, PaperFill.instrument))]


# -- registry ---------------------------------------------------------------------------------------------
def test_admission_needs_evidence_or_an_explicit_exploratory_decision_by_a_person(session, labexp):
    with pytest.raises(registry.RegistryError, match="evidence requirements not met"):
        admit(session, labexp, exploratory=False)
    with pytest.raises(registry.RegistryError, match="agents and automated processes"):
        admit(session, labexp, admitted_by="agent:coordinator")
    refused = session.scalars(select(Experiment).where(Experiment.kind == registry.KIND,
                                                       Experiment.status == "refused")).all()
    assert len(refused) == 2  # refusals are kept in the registry
    rec = admit(session, labexp)
    c = rec.config
    assert c["status"] == registry.EXPLORATORY and c["admission"]["kind"] == "exploratory"
    assert {x["check"] for x in c["admission"]["checks"]} >= {"lab_decision", "robustness", "data_unchanged",
                                                              "named_person", "justification"}
    for k in ("hypothesis", "assets", "timeframe", "code_version", "strategy", "features", "results", "robustness",
              "baselines", "costs", "risk_limits", "admission"):
        assert k in c
    assert c["strategy"]["params"]["lookback"] in (20, 60, 120)  # the walk-forward selection, not refitted
    assert all("estimate" in m["source"] for m in c["costs"].values())
    assert c["allowed_modes"] == ["C"]  # synthetic data
    assert registry.verify(rec)


def test_candidates_are_immutable_and_versioned(session, labexp):
    a = admit(session, labexp)
    assert admit(session, labexp).id == a.id  # same snapshot: the same version
    b = admit(session, labexp, allocation=0.2)
    assert (a.config["version"], b.config["version"]) == (1, 2)
    assert [v.id for v in registry.versions(session, "Momentum SYN-D")] == [a.id, b.id]
    a.config = {**a.config, "allocation": 0.9}  # tampering
    session.commit()
    assert not registry.verify(a) and registry.admission_status(a) == "UNKNOWN"
    with pytest.raises(protocol.ProtocolError, match="does not match its hash"):
        proto(session, a)


# -- protocol ---------------------------------------------------------------------------------------------
def test_horizon_depends_on_the_candidate_and_infeasible_protocols_are_refused(session, labexp):
    cand = admit(session, labexp)
    oos = cand.config["results"]["oos"]
    p = proto(session, cand)
    hz = p.config["horizon"]
    assert hz["bars_for_trades"] == -(-3 * oos["bars"] // oos["n_trades"])
    assert hz["horizon_bars"] == max(60, hz["bars_for_trades"], hz["bars_for_precision"])
    tighter = protocol.plan_horizon(cand.config, protocol.ProtocolIn.model_validate(
        {"candidate_id": cand.id, "mode": "C", "created_by": "ana", "rationale": WHY, "min_trades": 3,
         "target_sharpe_se": 0.5}))
    assert tighter["horizon_bars"] > hz["horizon_bars"]  # more precision, longer horizon: not a fixed length
    with pytest.raises(protocol.ProtocolError, match="exceeds max_duration_bars"):
        proto(session, cand, target_sharpe_se=0.2, max_duration_bars=500)
    with pytest.raises(protocol.ProtocolError, match="replay period has"):
        proto(session, cand, min_trades=30)
    with pytest.raises(protocol.ProtocolError, match="mode B"):
        proto(session, cand, mode="B")
    with pytest.raises(protocol.ProtocolError, match="agents"):
        proto(session, cand, created_by="agent:x")
    assert p.config["frozen_at"] and p.config["primary_metric"] and p.config["acceptance"] and p.config["rejection"]
    assert set(p.config["pause_causes"]) >= {"stale_or_corrupt_data", "risk_limit_breach", "drawdown", "exposure",
                                             "anomalous_costs", "engine_errors", "inconsistent_orders_positions_pnl",
                                             "persistence_failure"}


def test_protocol_must_be_fixed_before_the_start_and_governs_one_program(session, labexp, monkeypatch):
    cand = admit(session, labexp)
    monkeypatch.setattr(protocol, "utcnow", lambda: pd.Timestamp("2099-01-01", tz="UTC").to_pydatetime())
    late = proto(session, cand)
    monkeypatch.undo()
    with pytest.raises(service.ProgramError, match="fixed before the program starts"):
        service.start(session, late.id, "ana")
    p = proto(session, cand)
    service.start(session, p.id, "ana")
    with pytest.raises(service.ProgramError, match="already governs"):
        service.start(session, p.id, "ana")
    with pytest.raises(service.ProgramError, match="agents"):
        service.start(session, proto(session, cand).id, "agent:runner")


# -- a full program (mode C) --------------------------------------------------------------------------------
def test_program_runs_to_its_horizon_and_needs_a_conservative_review(session, labexp):
    prog = started(session, labexp)
    out = service.run(session, prog.id, 5000)
    assert out["status"] == "FINISHED", out
    ps, _, pc = service.parts(session, prog)
    assert ps.bars_processed >= pc["horizon"]["horizon_bars"]
    ev = service.refresh_state(session, prog)
    rule = ev["rule"]
    assert rule["state"] in ("COMPLETED", "INCONCLUSIVE", "REJECTED")
    assert "SYNTHETIC" in rule["note"] and "EXPLORATORY" in rule["note"] and "does not authorise" in rule["note"]
    assert service.classification(prog)["state"] is None  # rules alone do not classify
    with pytest.raises(service.ProgramError, match="never less"):
        service.review(session, prog.id, "ana", "COMPLETED" if rule["state"] != "COMPLETED" else "CONTINUE_PAPER",
                       "Trying to classify above what the rules give, which must fail.")
    with pytest.raises(service.ProgramError, match="agents"):
        service.review(session, prog.id, "agent:x", rule["state"], "An agent cannot record the classification at all.")
    service.review(session, prog.id, "ana", rule["state"], "Rules confirmed after reading the comparison and pauses.")
    cl = service.classification(prog)
    assert cl["reviewed"] and cl["state"] == rule["state"] and cl["by"] == "ana"
    kinds = [t["kind"] for t in prog.results["timeline"]]
    assert kinds[0] == "started" and "finished" in kinds and "reviewed" in kinds
    assert all("event_seq" in t and t["at"] for t in prog.results["timeline"])
    from qal.program import report

    rep = report.build(session, prog)
    assert rep["label"] == "SYNTHETIC_SIMULATION"
    assert any("Synthetic data" in x for x in rep["limitations"])
    assert any("Exploratory" in x for x in rep["limitations"])
    assert rep["protocol"]["integrity_ok"] and rep["candidate"]["integrity_ok"]


def test_program_session_history_cannot_be_reset_deleted_or_reconfigured(session, labexp):
    prog = started(session, labexp)
    service.run(session, prog.id, 10)
    ps, _, _ = service.parts(session, prog)
    for call in (lambda: paper.reset(session, ps, "ana", True), lambda: paper.delete_session(session, ps, "ana", True),
                 lambda: paper.change_config(session, ps, {"order_type": "limit"}, "ana", "tuning"),
                 lambda: paper.start(session, ps, "ana")):
        with pytest.raises(paper.PaperError, match="validation program"):
            call()


# -- replay vs prospective -------------------------------------------------------------------------------------
def test_modes_are_separated_and_labelled(session, lab, labexp, monkeypatch):  # noqa: F811
    with pytest.raises(protocol.ProtocolError, match="not allowed"):
        proto(session, admit(session, labexp), mode="A")  # synthetic data: only C
    as_real(session, labexp)
    cand = admit(session, labexp, name="Momentum real-flagged")
    assert cand.config["allowed_modes"] == ["A", "B"]
    with pytest.raises(protocol.ProtocolError, match="not allowed"):
        proto(session, cand, mode="C")
    a = service.start(session, proto(session, cand, mode="A").id, "ana")
    service.run(session, a.id, 30)
    assert a.config["mode_label"] == "HISTORICAL_REPLAY"
    assert a.results["feed_log"] and not any(x["prospective"] for x in a.results["feed_log"])
    pa = session.get(Experiment, a.config["protocol_id"]).config
    assert pa["replay"]["overlaps_research"] is False  # the reserved final period, never evaluated
    # B: only bars that closed after the protocol was frozen are prospective; the freeze time is set in the past
    monkeypatch.setattr(protocol, "utcnow", lambda: pd.Timestamp("2024-06-01", tz="UTC").to_pydatetime())
    pb = proto(session, cand, mode="B")
    monkeypatch.undo()
    b = service.start(session, pb.id, "ana")
    session.commit()
    ds = session.get(Dataset, labexp.data_refs[0]["dataset_id"])
    new = ingest(session, IngestRequest(adapter="synthetic", symbol=ds.instrument.symbol, asset_class="other",
                                        options={"n_bars": 1530}), actor="test")["dataset_id"]
    session.get(Dataset, new).is_synthetic = False
    session.commit()
    last = engine.utc(session.get(Dataset, new).end_ts)
    out = service.run(session, b.id, 100, now=(last + pd.Timedelta(hours=30)).to_pydatetime())
    assert out["bars"] > 0, (out, b.results["errors"])
    log = b.results["feed_log"]
    assert log and all(x["prospective"] for x in log)
    assert all(x["processing_delay_h"] is not None for x in log)
    assert b.config["mode_label"] == "PROSPECTIVE_PAPER"
    assert service.summary_row(b)["prospective_bars"] == len(log)


# -- execution, accounting, temporal order, lookahead -------------------------------------------------------------
def test_temporal_order_no_lookahead_and_costs_counted_once(session, labexp):
    prog = started(session, labexp)
    service.run(session, prog.id, 5000)
    ps, _, _ = service.parts(session, prog)
    sigs = session.scalars(select(PaperSignal).where(PaperSignal.session_id == ps.id)).all()
    for f in session.scalars(select(PaperFill).where(PaperFill.session_id == ps.id)):
        o = session.get(PaperOrder, f.order_id)
        decided = max(engine.utc(x.bar_ts) for x in sigs if engine.utc(x.decided_at) <= engine.utc(o.created_ts))
        assert engine.utc(f.bar_ts) >= engine.utc(o.created_ts)  # the bar started at/after the decision
        assert engine.utc(f.bar_ts) > decided  # never on the decision bar
    assert all(engine.utc(x.decided_at) >= engine.utc(x.bar_ts) for x in sigs)
    ev = service.refresh_state(session, prog)
    assert ev["comparison"]["signal_frequency"]["agreement"] == 1.0  # same decisions as the full-history backtest
    # costs: the fills' costs are the ledger's commissions plus the price adjustments, counted once in equity
    pos = session.scalars(select(PaperPosition).where(PaperPosition.session_id == ps.id)).all()
    led = session.scalars(select(PaperLedger).where(PaperLedger.session_id == ps.id)).all()
    fills = session.scalars(select(PaperFill).where(PaperFill.session_id == ps.id)).all()
    assert sum(-x.amount for x in led if x.kind == "commission") == pytest.approx(sum(f.commission for f in fills))
    assert ps.summary["equity"] == pytest.approx(ps.initial_capital + sum(p.realized_pnl + p.unrealized_pnl - p.fees
                                                                          - p.holding_costs for p in pos), rel=1e-9)
    c = ev["comparison"]["costs"]
    assert c["paper_total"] == pytest.approx(sum(c["paper_breakdown"].values()))


def test_lookahead_future_bars_do_not_change_past_decisions(session, labexp):
    prog = started(session, labexp)
    service.run(session, prog.id, 40)
    ps, _, _ = service.parts(session, prog)
    rt = engine.load_runtime(session, ps)
    plan = rt.plans[0]
    sym = rt.by_dataset[plan.dataset_ids[0]]
    from qal.paper.signals import decide

    for sg in session.scalars(select(PaperSignal).where(PaperSignal.session_id == ps.id)).all()[:10]:
        known = rt.instr[sym].known(sg.decided_at)
        f = rt.instr[sym].frame.copy()
        cut = len(known)
        f.loc[cut:, ["open", "high", "low", "close"]] *= 3.0  # rewrite the future
        again = decide(plan, [f.iloc[:cut]], rt.cfg.history_bars)
        assert again[0] == pytest.approx(sg.targets[sym])


# -- reconciliation, restart, idempotency, errors -------------------------------------------------------------------
def test_reconciliation_failure_pauses_and_resume_requires_it_to_pass(session, labexp):
    prog = started(session, labexp)
    service.run(session, prog.id, 20)
    ps, _, _ = service.parts(session, prog)
    ps.cash += 1234.5  # inconsistency between cash and the ledger
    session.commit()
    service.run(session, prog.id, 1)
    p = prog.results["pauses"][-1]
    assert p["cause"] == "inconsistent_orders_positions_pnl" and p["at"] and p["evidence"]
    assert service.refresh_state(session, prog)["rule"]["state"] == "PAUSED_REVIEW"
    with pytest.raises(service.ProgramError, match="reconciliation still fails"):
        service.resume(session, prog.id, "ana", "checked the ledger entries", "nothing fixed yet", True)
    ps.cash -= 1234.5  # the person fixes the cause (here: the injected error); the bar's own moves are in the ledger
    session.commit()
    with pytest.raises(service.ProgramError, match="confirmed"):
        service.resume(session, prog.id, "ana", "checked the ledger entries", "cash restored from ledger", False)
    service.resume(session, prog.id, "ana", "checked the ledger entries", "cash restored from ledger", True)
    assert prog.results["pauses"][-1]["checks"][0] == {"check": "reconciliation", "passed": True, "mismatches": []}
    assert service.run(session, prog.id, 5)["bars"] == 5


def test_restart_after_a_crash_resumes_identically_without_duplicates(session, labexp):
    cand = admit(session, labexp)
    a = service.start(session, proto(session, cand).id, "ana")
    service.run(session, a.id, 5000)
    b = service.start(session, proto(session, cand).id, "ana")
    session.commit()

    def crash(i):
        if i == 7:
            raise RuntimeError("process killed")

    service.run(session, b.id, 20, fault=crash)  # bar 8 is rolled back and repeated
    assert b.results["errors"] and "process killed" in b.results["errors"][0]["error"]
    session.expire_all()  # state must come from the database
    service.run(session, b.id, 5000)
    pa, _, _ = service.parts(session, a)
    pb, _, _ = service.parts(session, b)
    assert fills_digest(session, pa.id) == fills_digest(session, pb.id)
    ids = [o.client_order_id for o in session.scalars(select(PaperOrder).where(PaperOrder.session_id == pb.id))]
    assert len(ids) == len(set(ids))
    assert pa.cash == pytest.approx(pb.cash)


def test_repeated_engine_errors_pause_the_program(session, labexp):
    prog = started(session, labexp, max_consecutive_errors=2)

    def boom(i):
        raise RuntimeError("engine fault")

    service.run(session, prog.id, 10, fault=boom)
    p = prog.results["pauses"][-1]
    assert p["cause"] == "engine_errors" and "engine fault" in p["evidence"]["last_error"]
    ps, _, _ = service.parts(session, prog)
    assert ps.status == "PAUSED" and ps.bars_processed == 0


# -- circuit breakers ----------------------------------------------------------------------------------------------
def test_drawdown_exposure_and_cost_breakers_pause_with_evidence(session, labexp):
    prog = started(session, labexp, cand_over={"risk_limits": {"drawdown_pause": 0.001, "drawdown_stop": 0.5}})
    service.run(session, prog.id, 5000)
    p = next(x for x in prog.results["pauses"] if x["cause"] == "drawdown")
    assert p["evidence"]["drawdown"] <= -0.001 and p["sim_ts"] and p["at"]
    with pytest.raises(service.ProgramError, match="agents"):
        service.resume(session, prog.id, "agent:x", "the agent wants to resume", "nothing really", True)
    service.resume(session, prog.id, "ana", "drawdown reviewed against the protocol", "within expectations", True)
    r = next(x for x in prog.results["pauses"] if x["id"] == p["id"])
    assert r["resolved_by"] == "ana" and r["resolution"]
    assert prog.results["timeline"][-1]["kind"] == "resumed"
    costly = started(session, labexp, cand_over={"name": "Costly"}, anomalous_cost_bps=0.01)
    service.run(session, costly.id, 5000)
    assert any(x["cause"] == "anomalous_costs" for x in costly.results["pauses"])
    expo = started(session, labexp, cand_over={"name": "Exposed", "risk_limits": {"max_gross_exposure": 0.01}})
    service.run(session, expo.id, 5000)
    assert any(x["cause"] == "exposure" for x in expo.results["pauses"])


def test_drawdown_stop_rejects_and_cannot_resume_and_kill_switch_blocks(session, labexp):
    prog = started(session, labexp, cand_over={"risk_limits": {"drawdown_pause": 0.0005, "drawdown_stop": 0.001}})
    service.run(session, prog.id, 5000)
    assert any(x["cause"] == "drawdown_stop" for x in prog.results["pauses"])
    assert service.refresh_state(session, prog)["rule"]["state"] == "REJECTED"
    with pytest.raises(service.ProgramError, match="cannot resume"):
        service.resume(session, prog.id, "ana", "trying to continue anyway", "no resolution possible", True)
    k = started(session, labexp, cand_over={"name": "Killed"})
    service.run(session, k.id, 5)
    service.kill_switch(session, k.id, "ana", True, True, "operator drill")
    session.commit()
    n = session.query(PaperOrder).filter(PaperOrder.session_id == k.config["session_id"]).count()
    out = service.run(session, k.id, 50)
    assert out["bars"] == 0 and "kill switch" in out["reason"]
    assert session.query(PaperOrder).filter(PaperOrder.session_id == k.config["session_id"]).count() == n
    with pytest.raises(service.ProgramError, match="kill switch active"):
        service.resume(session, k.id, "ana", "resume after the drill", "drill finished", True)


# -- early stop, classification rules ---------------------------------------------------------------------------
def test_early_stop_keeps_the_period_and_is_never_completed(session, labexp):
    prog = started(session, labexp)
    service.run(session, prog.id, 30)
    service.stop(session, prog.id, "ana", "results look bad so far", True)
    rule = service.refresh_state(session, prog)["rule"]
    assert rule["state"] in ("INCONCLUSIVE", "REJECTED")
    assert prog.results["stopped"]["bars"] == 30
    ps, _, _ = service.parts(session, prog)
    assert service.refresh_state(session, prog)["tracking"]["bars"] == 30 == ps.bars_processed
    with pytest.raises(service.ProgramError, match="STOPPED_EARLY"):
        service.run(session, prog.id, 10)


def _rule(n, trades, ex, dci, ratio=1.0, status="FINISHED", pauses=(), drawdown=-0.01):
    proto_ = {"horizon": {"horizon_bars": 100}, "min_trades": 10, "baseline": "no_trade", "tracking_tolerance_bps": 2.0,
              "min_return_correlation": 0.8, "min_signal_agreement": 0.95, "max_cost_ratio": 1.5,
              "reject_cost_ratio": 3.0, "min_fill_rate": 0.8, "max_drawdown_ratio": 1.5, "max_missing_frac": 0.05}
    cand = {"risk_limits": {"drawdown_stop": 0.2}, "admission": {"kind": "validated"}, "is_synthetic": False}
    trk = {"bars": n, "trades": trades, "provisional": n < 100, "paper": {"max_drawdown": drawdown},
           "excess_vs_baseline": {"mean_ci95": ex}, "diff_vs_backtest": {"mean_ci95_bps": dci, "correlation": 0.99}}
    cmp_ = {"comparable": True, "costs": {"ratio": ratio}, "signal_frequency": {"agreement": 1.0},
            "fills": {"fill_rate": 0.95}, "drawdown": {"ratio": 1.0}}
    return classify.rule_state({"status": status, "pauses": list(pauses)}, proto_, trk, cmp_, cand)["state"]


def test_accept_reject_and_insufficient_sample_rules():
    assert _rule(120, 12, [0.0001, 0.001], [-1, 1]) == "COMPLETED"
    assert _rule(120, 12, [-0.0002, 0.001], [-1, 1]) == "INCONCLUSIVE"  # not distinguishable from the baseline
    assert _rule(120, 12, [-0.002, -0.0001], [-1, 1]) == "REJECTED"
    assert _rule(120, 12, [0.0001, 0.001], [3, 5]) == "REJECTED"  # does not reproduce the backtest
    assert _rule(120, 12, [0.0001, 0.001], [-1, 1], ratio=4.0) == "REJECTED"
    # insufficient sample: the statistical reject criteria are not applied, nothing is forced
    assert _rule(50, 3, [-0.002, -0.0001], [3, 5], ratio=4.0, status="FINISHED") == "INCONCLUSIVE"
    assert _rule(50, 3, [-0.002, -0.0001], [3, 5], status="RUNNING") == "CONTINUE_PAPER"
    assert _rule(50, 3, [0.0001, 0.001], [-1, 1], status="STOPPED_EARLY") == "INCONCLUSIVE"
    assert _rule(50, 3, None, None, status="RUNNING", drawdown=-0.3) == "REJECTED"  # the stop applies any time
    assert _rule(50, 3, None, None, status="RUNNING",
                 pauses=[{"cause": "exposure", "message": "x", "resolved_at": None}]) == "PAUSED_REVIEW"
    assert classify.ALLOWED["INCONCLUSIVE"] == ("INCONCLUSIVE", "REJECTED")
    assert "COMPLETED" not in classify.ALLOWED["CONTINUE_PAPER"]


# -- versions and comparison -------------------------------------------------------------------------------------
def test_versions_are_tracked_separately_and_comparison_checks_conventions(session, labexp):
    v1 = started(session, labexp)
    v2 = started(session, labexp, cand_over={"allocation": 0.2})
    assert v1.config["candidate_version"] == 1 and v2.config["candidate_version"] == 2
    service.run(session, v1.id, 70)
    service.run(session, v2.id, 70)
    ev1, ev2 = service.evaluate(session, v1), service.evaluate(session, v2)
    assert ev1["comparison"]["comparable"] and ev2["comparison"]["comparable"]
    assert {c["check"] for c in ev1["comparison"]["conventions"]} >= {"candidate_version", "strategy_and_parameters",
                                                                     "universe", "timeframe", "costs", "allocation",
                                                                     "data_snapshot"}
    assert v1.config["session_id"] != v2.config["session_id"]
    assert ev1["tracking"]["paper"]["total_return"] != ev2["tracking"]["paper"]["total_return"]
    cmp_ = ev1["comparison"]
    for k in ("returns", "volatility", "drawdown", "signal_frequency", "per_trade", "costs", "exposure", "per_asset",
              "regimes", "fills", "unreconciled"):
        assert k in cmp_
    assert all(u["cause"].startswith("not asserted") for u in cmp_["unreconciled"])
    trk = ev1["tracking"]
    assert trk["provisional"] and trk["provisional_reasons"]
    assert trk["paper"]["sharpe_se"] is not None and trk["excess_vs_baseline"]["mean_ci95"]
    assert trk["rolling"] is not None and trk["rolling"]["window"] == 63
    assert "not a probability" in trk["note"]


def test_block_bootstrap_ci_is_deterministic_and_wider_for_dependent_data():
    import numpy as np

    rng = np.random.default_rng(1)
    x = rng.normal(0.001, 0.01, 500)
    a = tracking.block_bootstrap_ci(x, 5)
    assert a == tracking.block_bootstrap_ci(x, 5) and a[0] < x.mean() < a[1]
    walk = np.repeat(rng.normal(0, 0.01, 50), 10)  # strongly dependent
    wide, narrow = tracking.block_bootstrap_ci(walk, 10), tracking.block_bootstrap_ci(walk, 1)
    assert wide[1] - wide[0] > narrow[1] - narrow[0]


# -- isolation --------------------------------------------------------------------------------------------------
def test_no_real_orders_no_credentials():
    forbidden = {"requests", "httpx", "urllib", "socket", "websocket", "websockets", "ccxt", "MetaTrader5", "yfinance",
                 "aiohttp", "ib_insync", "alpaca", "oandapyV20", "smtplib"}
    pkg = pathlib.Path(service.__file__).parent
    for f in pkg.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for n in names:
                assert n.split(".")[0] not in forbidden, f"{f.name} imports {n}"
        for word in ("api_key", "apikey", "secret", "password", "token"):
            assert word not in text.lower(), f"{f.name} mentions {word}"
    from qal.program import PAPER_ONLY

    assert PAPER_ONLY is True
    fields = set(registry.AdmissionIn.model_fields) | set(protocol.ProtocolIn.model_fields)
    assert not any(w in f for f in fields for w in ("key", "secret", "password", "token", "account", "broker"))


# -- API --------------------------------------------------------------------------------------------------------
def test_api_flow(env, lab):  # noqa: F811
    from qal.api.main import app

    with db.session_scope() as s:
        exp = run_new(s, lab)
        eid = exp.id
    with TestClient(app) as c:
        meta = c.get("/api/v1/program/meta").json()
        assert meta["live_trading_enabled"] is False and set(meta["states"]) == set(classify.STATES)
        assert c.get("/api/v1/program/eligibility").json()[0]["lab_experiment_id"] == eid
        r = c.post("/api/v1/program/candidates", json={"name": "Momentum API", "lab_experiment_id": eid,
                                                         "admitted_by": "ana", "justification": JUST})
        assert r.status_code == 400 and "evidence requirements" in r.json()["detail"]
        r = c.post("/api/v1/program/candidates", json={"name": "Momentum API", "lab_experiment_id": eid,
                                                         "admitted_by": "ana", "justification": JUST,
                                                         "exploratory": True})
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        assert c.get("/api/v1/program/candidates").json()[0]["id"]
        body = {"candidate_id": cid, "mode": "C", "created_by": "ana", "rationale": WHY, "min_trades": 3,
                "target_sharpe_se": 2.0}
        assert c.post("/api/v1/program/protocols/preview", json=body).json()["horizon"]["horizon_bars"] >= 60
        pid = c.post("/api/v1/program/protocols", json=body).json()["id"]
        prog = c.post("/api/v1/program/programs", json={"protocol_id": pid, "started_by": "ana"}).json()
        r = c.post(f"/api/v1/program/programs/{prog['id']}/run", json={"max_bars": 5000, "run_async": False})
        assert r.status_code == 200, r.text
        st = c.get(f"/api/v1/program/programs/{prog['id']}").json()
        assert st["summary"]["status"] == "FINISHED" and st["tracking"]["curves"]["paper"]
        rule = st["rule"]["state"]
        r = c.post(f"/api/v1/program/programs/{prog['id']}/review",
                   json={"by": "ana", "decision": rule, "rationale": "Rules confirmed after reading the comparison."})
        assert r.status_code == 200, r.text
        rep = c.get(f"/api/v1/program/programs/{prog['id']}/export.json")
        assert rep.status_code == 200 and "SYNTHETIC_SIMULATION" in rep.text and "attachment" in rep.headers[
            "content-disposition"]
        assert c.get(f"/api/v1/program/candidates/{cid}/versions").json()[0]["version"] == 1
        sid = st["summary"]["session_id"]
        assert c.post(f"/api/v1/paper/sessions/{sid}/reset", json={"by": "ana", "confirm": True}).status_code == 400
    with db.session_scope() as s:
        assert s.get(PaperSession, sid).bars_processed > 0
