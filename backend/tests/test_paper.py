"""Phase 7: simulated paper trading. Execution rules, order states, accounting, the pre-trade risk gate, the
session engine (timing, reconciliation, restart, data-quality pauses, kill switch) and the API.

All data here is synthetic and every strategy is an UNVALIDATED DEMO: the tests check the simulator, not any
market."""

import ast
import pathlib

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from qal import db
from qal.data.ingest import IngestRequest, ingest
from qal.models import Experiment, Hypothesis, PaperFill, PaperOrder, PaperPosition, PaperSession
from qal.paper import accounting, engine, evaluation, execution, orders, risk_gate, service
from qal.paper.config import SessionConfig
from qal.paper.reconcile import reconcile
from qal.portfolio.risk_manager import PolicyLimits
from qal.research.costs import CostModel

NOCOST = CostModel(commission_bps=0, half_spread_bps=0, slippage_bps=0, slippage_range_frac=0, max_participation=1.0)


def bar(o, h, l, c, v=None, rng=0.0):  # noqa: E741
    return execution.Bar(pd.Timestamp("2024-01-02", tz="UTC"), o, h, l, c, v, rng)


def view(side, typ, qty=100, limit=None, stop=None, triggered=False):
    return execution.OrderView(side, typ, qty, limit, stop, triggered)


# -- isolation ---------------------------------------------------------------------------------------------
def test_isolated_from_real_execution():
    forbidden = {"requests", "httpx", "urllib", "socket", "websocket", "websockets", "ccxt", "MetaTrader5", "yfinance",
                 "aiohttp", "ib_insync", "alpaca", "oandapyV20"}
    pkg = pathlib.Path(engine.__file__).parent
    for f in pkg.glob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for n in names:
                assert n.split(".")[0] not in forbidden, f"{f.name} imports {n}"
    from qal.paper import PAPER_ONLY

    assert PAPER_ONLY is True


# -- execution rules ---------------------------------------------------------------------------------------
def test_market_fills_at_next_open_plus_costs():
    cm = CostModel(commission_bps=1, half_spread_bps=2, slippage_bps=3, slippage_range_frac=0.1, max_participation=1)
    d = execution.simulate(view("buy", "market"), bar(100, 105, 95, 101, rng=0.02), cm, lot_step_units=1)
    assert d.filled == 100
    assert d.price == pytest.approx(100 * (1 + 0.0002 + 0.0003 + 0.1 * 0.02))
    s = execution.simulate(view("sell", "market"), bar(100, 105, 95, 101), NOCOST, lot_step_units=1)
    assert s.price == pytest.approx(100)


def test_limit_touch_is_not_a_fill_and_cross_fills_at_better_of_open_and_limit():
    touch = execution.simulate(view("buy", "limit", limit=95), bar(100, 105, 95, 101), NOCOST, lot_step_units=1)
    assert touch.filled == 0 and "touch" in touch.rule
    cross = execution.simulate(view("buy", "limit", limit=96), bar(100, 105, 95, 101), NOCOST, lot_step_units=1)
    assert cross.filled == 100 and cross.price == pytest.approx(96)
    gap = execution.simulate(view("buy", "limit", limit=96), bar(94, 97, 93, 95), NOCOST, lot_step_units=1)
    assert gap.price == pytest.approx(94)  # opened below the limit: filled at the open
    sell = execution.simulate(view("sell", "limit", limit=104), bar(100, 105, 95, 101), NOCOST, lot_step_units=1)
    assert sell.price == pytest.approx(104)


def test_stop_gap_fills_at_the_worse_open_not_at_the_stop():
    gap = execution.simulate(view("sell", "stop", stop=95), bar(90, 92, 88, 91), NOCOST, lot_step_units=1)
    assert gap.filled == 100 and gap.price == pytest.approx(90) and "gapped" in gap.rule
    intrabar = execution.simulate(view("sell", "stop", stop=95), bar(100, 101, 94, 96), NOCOST, lot_step_units=1)
    assert intrabar.price == pytest.approx(95)
    miss = execution.simulate(view("sell", "stop", stop=90), bar(100, 101, 94, 96), NOCOST, lot_step_units=1)
    assert miss.filled == 0 and not miss.triggered


def test_stop_limit_triggered_inside_the_bar_does_not_fill_on_that_bar():
    d = execution.simulate(view("buy", "stop_limit", stop=102, limit=103), bar(100, 104, 99, 103), NOCOST,
                           lot_step_units=1)
    assert d.triggered and d.filled == 0 and "next bar" in d.rule
    nxt = execution.simulate(view("buy", "stop_limit", stop=102, limit=103, triggered=True), bar(104, 105, 102.5, 104),
                             NOCOST, lot_step_units=1)
    assert nxt.filled == 100 and nxt.price == pytest.approx(103)
    at_open = execution.simulate(view("buy", "stop_limit", stop=102, limit=103), bar(102.5, 104, 102, 103), NOCOST,
                                 lot_step_units=1)
    assert at_open.filled == 100 and at_open.price == pytest.approx(102.5)


def test_partial_fill_by_participation_and_no_volume_is_reported():
    cm = NOCOST.model_copy(update={"max_participation": 0.1})
    d = execution.simulate(view("buy", "market", qty=500), bar(100, 101, 99, 100, v=2000), cm, lot_step_units=1)
    assert d.filled == 200 and any("partial" in n for n in d.notes)
    nv = execution.simulate(view("buy", "market", qty=500), bar(100, 101, 99, 100, v=0), cm, lot_step_units=1)
    assert nv.filled == 500 and any("liquidity not evaluated" in n for n in nv.notes)


def test_order_state_machine_refuses_illegal_transitions():
    o = PaperOrder(client_order_id="x", status="CREATED")
    with pytest.raises(orders.OrderStateError):
        orders.transition(o, "FILLED")
    orders.transition(o, "PENDING")
    orders.transition(o, "PARTIALLY_FILLED")
    orders.transition(o, "FILLED")
    with pytest.raises(orders.OrderStateError):
        orders.transition(o, "CANCELLED")


# -- accounting --------------------------------------------------------------------------------------------
def test_realized_and_unrealized_pnl_are_separate_and_flip_reopens():
    p = accounting.Pos()
    assert accounting.apply_fill(p, 100, 10.0, 1.0) == 0
    assert accounting.unrealized(p, 12.0, 1.0) == pytest.approx(200)
    assert accounting.apply_fill(p, -40, 12.0, 1.0) == pytest.approx(80)
    assert p.quantity == 60 and p.avg_price == pytest.approx(10.0)
    assert accounting.apply_fill(p, -100, 11.0, 1.0) == pytest.approx(60)  # closes 60, opens 40 short at 11
    assert p.quantity == -40 and p.avg_price == pytest.approx(11.0)
    assert accounting.unrealized(p, 10.0, 1.0) == pytest.approx(40)


def test_currency_conversion_uses_session_rates_or_refuses():
    meta = {"USDJPY": {"asset_class": "fx", "currency": None}, "EURUSD": {"asset_class": "fx", "currency": None},
            "EURGBP": {"asset_class": "fx", "currency": None}}
    plan, problems = accounting.conversion_plan(meta, "USD")
    assert plan["EURUSD"]["via"] is None
    assert plan["USDJPY"] == {"quote": "JPY", "via": "USDJPY", "invert": True, "assumed": False}
    assert accounting.fx_rate(plan, "USDJPY", {"USDJPY": 150.0}) == pytest.approx(1 / 150)
    assert problems and "EURGBP" in problems[0]
    meta["GBPUSD"] = {"asset_class": "fx", "currency": None}
    plan, problems = accounting.conversion_plan(meta, "USD")
    assert not problems and plan["EURGBP"]["via"] == "GBPUSD"


# -- risk gate ---------------------------------------------------------------------------------------------
def ctx(**kw):
    base = dict(as_of="t", session_status="RUNNING", kill_switch=False, reconciliation_ok=True, strategy_status="DEMO",
                allow_demo=True, data_status="pass", data_age_hours=0.0, max_staleness_hours=96, equity=100_000,
                day_start_equity=100_000, peak_equity=100_000, margin_used=0.0, positions={}, asset_class={"X": "other"},
                is_spot_crypto=False, prev_dollar_volume=None, max_participation=0.05, max_leverage_contract=1.0)
    return risk_gate.GateContext(**{**base, **kw})


def gate(current=0.0, delta=100.0, price=100.0, **kw):
    return risk_gate.evaluate(instrument="X", current_units=current, delta_units=delta, price_acct=price, lot_units=1,
                              min_units=1, ctx=ctx(**kw), limits=PolicyLimits())


def test_risk_gate_rejects_kill_switch_unvalidated_and_stale_data():
    assert not gate(kill_switch=True).approved
    r = gate(allow_demo=False)
    assert not r.approved and "not eligible" in r.reason
    assert not gate(strategy_status="VALIDATION_FAILED").approved
    assert not gate(data_age_hours=200).approved
    assert not gate(data_status="fail").approved
    assert gate(strategy_status="PAPER_TRADING_ELIGIBLE").approved


def test_risk_gate_caps_exposure_and_blocks_risk_in_drawdown_but_allows_reducing():
    r = gate(delta=1000)  # 100k notional vs 0.6 instrument limit
    assert r.approved and r.quantity == 600
    assert any(c["rule"] == "max_instrument_gross" and c["decision"] == "blocked" for c in r.checks)
    dd = gate(equity=80_000, peak_equity=100_000)
    assert not dd.approved and dd.reason == "drawdown limit"
    reduce = gate(current=300, delta=-200, equity=80_000, peak_equity=100_000)
    assert reduce.approved and reduce.quantity == -200
    assert not gate(current=300, delta=10, equity=96_000, day_start_equity=100_000).approved  # daily loss 4% > 3%


def test_risk_gate_spot_crypto_no_short_and_liquidity_cap():
    assert not gate(delta=-1, is_spot_crypto=True).approved
    r = gate(delta=300, prev_dollar_volume=200_000)  # 30k traded vs 5% of 200k = 10k
    assert r.approved and r.quantity == 100


# -- engine with stored data -------------------------------------------------------------------------------
@pytest.fixture()
def lab(session):
    ids = {}
    for sym in ("SYN-A", "SYN-B", "SYN-C"):
        ids[sym] = ingest(session, IngestRequest(adapter="synthetic", symbol=sym, asset_class="other",
                                                 options={"seed": 7, "n_bars": 700}), actor="test")["dataset_id"]
    session.commit()
    return ids


def make(session, ids, **kw):
    start = pd.Timestamp("2021-06-21", tz="UTC")  # about 60 bars before the end of the 700-bar synthetic history
    cfg = SessionConfig(name=kw.pop("name", "demo"), created_by="tester",
                        allow_unvalidated_demo=kw.pop("allow_unvalidated_demo", True),
                        start=start.to_pydatetime(),
                        strategies=kw.pop("strategies", [
                            {"strategy_id": "DEMO:ts_momentum", "dataset_ids": [ids["SYN-A"]], "params": {"lookback": 20},
                             "allocation": 0.3},
                            {"strategy_id": "DEMO:pair_mean_reversion", "dataset_ids": [ids["SYN-B"], ids["SYN-C"]],
                             "params": {"window": 60, "entry_z": 1.0}, "allocation": 0.3}]),
                        **kw)
    ps = service.create_session(session, cfg)
    service.start(session, ps, "tester")
    session.commit()
    return ps


def fills_digest(session, sid):
    return [(f.strategy_id, f.instrument, str(f.bar_ts), f.side, round(f.quantity, 6), round(f.price, 9)) for f in
            session.scalars(select(PaperFill).where(PaperFill.session_id == sid).order_by(PaperFill.bar_ts,
                                                                                          PaperFill.strategy_id,
                                                                                          PaperFill.instrument))]


def test_session_never_fills_on_the_decision_bar_and_reconciles(session, lab):
    ps = make(session, lab)
    out = engine.run(session, ps, 1000)
    assert out["status"] == "COMPLETED" and out["bars"] > 40
    fills = session.scalars(select(PaperFill).where(PaperFill.session_id == ps.id)).all()
    assert fills
    for f in fills:
        o = session.get(PaperOrder, f.order_id)
        assert engine.utc(f.bar_ts) >= engine.utc(o.created_ts)  # bar started at/after the decision
    rec = reconcile(session, ps)
    assert rec["ok"], rec["mismatches"]
    pos = session.scalars(select(PaperPosition).where(PaperPosition.session_id == ps.id)).all()
    # unrealized P&L never enters cash: equity = cash + unrealized
    assert ps.summary["equity"] == pytest.approx(ps.cash + sum(p.unrealized_pnl for p in pos))
    ev = evaluation.evaluate(session, ps)
    assert ev["metrics"]["signal_agreement"] == 1.0
    assert "not an approval" in ev["note"]
    assert ev["verdict"] in ("insufficient_sample", "consistent_with_backtest", "deviates_from_backtest")
    assert "ready" not in ev["verdict"]


def test_restart_resumes_identically_and_sessions_are_reproducible(session, lab):
    a = make(session, lab, name="one go")
    engine.run(session, a, 1000)
    b = make(session, lab, name="interrupted")
    engine.run(session, b, 25)
    session.expire_all()  # drop everything in memory: state must come from the database
    b = session.get(PaperSession, b.id)
    engine.run(session, b, 1000)
    assert fills_digest(session, a.id)
    da, db_ = fills_digest(session, a.id), fills_digest(session, b.id)
    assert [x[:3] + x[4:] for x in da] == [x[:3] + x[4:] for x in db_]
    assert session.get(PaperSession, a.id).cash == pytest.approx(session.get(PaperSession, b.id).cash)


def test_repeating_a_step_does_not_duplicate_orders(session, lab):
    ps = make(session, lab)
    engine.run(session, ps, 10)
    rt = engine.load_runtime(session, ps)
    book = engine.positions(session, ps)
    n = session.query(PaperOrder).filter(PaperOrder.session_id == ps.id).count()
    o = engine.place_order(session, ps, rt, book, sid="MANUAL", sym="SYN-A", delta_units=5, order_type="market",
                           purpose="manual", ref_price=100.0, seq=1)
    again = engine.place_order(session, ps, rt, book, sid="MANUAL", sym="SYN-A", delta_units=5, order_type="market",
                               purpose="manual", ref_price=100.0, seq=1)
    assert o is not None and again is None
    assert session.query(PaperOrder).filter(PaperOrder.session_id == ps.id).count() == n + 1


def test_bad_bar_pauses_before_use_and_can_be_skipped(session, lab):
    ps = make(session, lab)
    engine.run(session, ps, 5)
    rt = engine.load_runtime(session, ps)
    nxt = rt.timeline(engine.utc(ps.cursor_ts))[0]
    f = rt.instr["SYN-A"].frame
    f.loc[f["ts"] == nxt, "high"] = 1.0  # high below open/close: invalid
    res = engine.step(session, ps, rt)
    assert not res["processed"] and ps.status == "PAUSED" and "data quality" in ps.pause_reason
    assert engine.utc(ps.cursor_ts) < nxt
    ps.status = "RUNNING"
    res = engine.step(session, ps, rt, skip_bad_bar=True)
    assert res.get("skipped") and engine.utc(ps.cursor_ts) == nxt


def test_kill_switch_cancels_open_orders_and_blocks_new_ones(session, lab):
    ps = make(session, lab)
    engine.run(session, ps, 10)
    o = service.manual_order(session, ps, orders.OrderIn(instrument="SYN-A", side="buy", lots=5, order_type="limit",
                                                         limit_price=1.0, tif_bars=None), "tester")
    assert o.status == "PENDING"
    with pytest.raises(service.PaperError):
        service.kill_switch(session, ps, active=True, by="tester", confirm=False)
    service.kill_switch(session, ps, active=True, by="tester", confirm=True, reason="test")
    assert o.status == "CANCELLED" and ps.status == "HALTED"
    with pytest.raises(service.PaperError):
        service.manual_order(session, ps, orders.OrderIn(instrument="SYN-A", side="buy", lots=1), "tester")
    with pytest.raises(service.PaperError):
        service.start(session, ps, "tester")
    service.kill_switch(session, ps, active=False, by="tester", confirm=True)
    assert ps.status == "PAUSED"


def test_reconciliation_mismatch_pauses_the_session(session, lab):
    ps = make(session, lab)
    engine.run(session, ps, 30)
    pos = session.scalars(select(PaperPosition).where(PaperPosition.session_id == ps.id,
                                                      PaperPosition.quantity != 0)).first()
    assert pos is not None
    pos.quantity += 1  # corrupt the stored state
    session.flush()
    assert not reconcile(session, ps)["ok"]
    engine.run(session, ps, 1)
    assert ps.status == "PAUSED" and "reconciliation" in ps.pause_reason
    with pytest.raises(service.PaperError):
        service.start(session, ps, "tester")


def test_config_changes_are_versioned_and_criteria_are_fixed(session, lab):
    ps = make(session, lab)
    service.change_config(session, ps, {"cost_scenario": "adverse"}, "tester", "stress costs")
    assert ps.config_version == 2 and ps.config_history[-1]["diff"]["cost_scenario"]["to"] == "adverse"
    with pytest.raises(service.PaperError):
        service.change_config(session, ps, {"criteria": {"min_bars": 1}}, "tester", "make it pass")
    with pytest.raises(service.PaperError):
        service.change_config(session, ps, {"capital": 1e9}, "tester", "more")


def test_unvalidated_hypothesis_and_demo_without_flag_are_refused(session, lab):
    h = Hypothesis(family_id="f", title="t", status="VALIDATION_FAILED", contract={}, contract_hash="x")
    session.add(h)
    session.flush()
    with pytest.raises(service.PaperError, match="VALIDATION_FAILED"):
        service.create_session(session, SessionConfig(name="xx", created_by="tester",
                                                      strategies=[{"strategy_id": f"H:{h.id}"}]))
    with pytest.raises(service.PaperError, match="allow_unvalidated_demo"):
        service.create_session(session, SessionConfig(name="xx", created_by="tester", strategies=[
            {"strategy_id": "DEMO:buy_and_hold", "dataset_ids": [lab["SYN-A"]]}]))


def test_protective_stops_and_limit_orders(session, lab):
    ps = make(session, lab, stop_loss_pct=0.01, order_type="limit", limit_offset_bps=150, order_ttl_bars=1,
              strategies=[{"strategy_id": "DEMO:ts_momentum", "dataset_ids": [lab["SYN-A"]], "params": {"lookback": 5},
                           "allocation": 0.3}])
    engine.run(session, ps, 1000)
    rows = session.scalars(select(PaperOrder).where(PaperOrder.session_id == ps.id)).all()
    assert any(o.purpose == "protective_stop" for o in rows)
    assert any(o.order_type == "limit" and o.status == "EXPIRED" for o in rows)
    assert reconcile(session, ps)["ok"]
    assert not any(o.status in orders.OPEN_STATES for o in rows)  # replay end cancels what is left


def test_validated_hypothesis_trades_with_its_selected_parameters_and_is_rechecked(session, lab):
    from tests.test_research import contract

    from qal.research.contract import HypothesisContract

    c = HypothesisContract.model_validate(contract({"SYN-B": lab["SYN-B"], "SYN-A": lab["SYN-A"]},
                                                   validation={"train_bars": 200, "test_bars": 50, "embargo_bars": 20,
                                                               "holdout_start": "2021-06-01T00:00:00Z"}))
    h = Hypothesis(family_id="f", title=c.title, status="VALIDATION_PASSED", contract=c.model_dump(mode="json"),
                   contract_hash="h")
    session.add(h)
    session.flush()
    session.add(Experiment(kind="validation", hypothesis_id=h.id, status="succeeded", algorithm_version="t",
                           code_version="t", config={}, config_hash="c",
                           results={"selected_params_last_window": {"entry_z": 1.5, "window": 60},
                                    "oos": {"metrics": {"sharpe_net": 0.7, "bars": 300}}}))
    session.flush()
    ps = make(session, lab, strategies=[{"strategy_id": f"H:{h.id}", "allocation": 0.3}], allow_unvalidated_demo=False)
    plan = ps.config["resolved"]["plans"][0]
    assert plan["params"] == {"entry_z": 1.5, "window": 60} and plan["status"] == "VALIDATION_PASSED"
    assert not ps.is_demo
    engine.run(session, ps, 15)
    h.status = "SUSPENDED"  # status is checked again at every order, not only at creation
    session.flush()
    engine.run(session, ps, 1000)
    rej = session.scalars(select(PaperOrder).where(PaperOrder.session_id == ps.id,
                                                   PaperOrder.status == "REJECTED")).all()
    assert rej and all("SUSPENDED" in o.reject_reason for o in rej)


# -- API ---------------------------------------------------------------------------------------------------
def test_api_flow(env, lab):
    from qal.api.main import create_app

    c = TestClient(create_app())
    assert c.get("/api/v1/health").json()["live_trading_enabled"] is False
    st = c.get("/api/v1/paper/strategies").json()
    assert any(d["strategy_id"] == "DEMO:ts_momentum" for d in st["demo"])
    body = {"name": "api", "created_by": "tester", "allow_unvalidated_demo": True, "start": "2021-06-21T00:00:00Z",
            "strategies": [{"strategy_id": "DEMO:ts_momentum", "dataset_ids": [lab["SYN-A"]], "params": {"lookback": 20}}]}
    r = c.post("/api/v1/paper/sessions", json=body)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert any("HISTORICAL REPLAY" in x for x in r.json()["labels"])
    assert c.post(f"/api/v1/paper/sessions/{sid}/step", json={"by": "tester"}).status_code == 400  # not started
    assert c.post(f"/api/v1/paper/sessions/{sid}/start", json={"by": "tester"}).status_code == 200
    r = c.post(f"/api/v1/paper/sessions/{sid}/step", json={"by": "tester", "bars": 500})
    assert r.status_code == 200 and r.json()["session"]["status"] == "COMPLETED"
    assert c.get(f"/api/v1/paper/sessions/{sid}/orders").json()
    assert c.get(f"/api/v1/paper/sessions/{sid}/positions").status_code == 200
    ev = c.get(f"/api/v1/paper/sessions/{sid}/events?category=fill&instrument=SYN-A").json()
    assert ev and all(e["category"] == "fill" for e in ev)
    csv = c.get(f"/api/v1/paper/sessions/{sid}/export?table=fills")
    assert csv.status_code == 200 and "SIMULATION ONLY" in csv.text
    e = c.post(f"/api/v1/paper/sessions/{sid}/evaluation").json()
    assert "not an approval" in e["note"]
    assert c.get(f"/api/v1/paper/compare?ids={sid}").json()[0]["verdict"] == e["verdict"]
    assert c.post(f"/api/v1/paper/sessions/{sid}/reset", json={"by": "tester"}).status_code == 400
    assert c.post(f"/api/v1/paper/sessions/{sid}/delete", json={"by": "tester", "confirm": True}).status_code == 200
    assert c.get(f"/api/v1/paper/sessions/{sid}").status_code == 404
    with db.session_scope() as s:
        exp = s.scalars(select(Experiment).where(Experiment.kind == "paper_session")).first()
        assert exp.results["deleted"]["by"] == "tester"  # the record stays in the registry
