"""Phase 9 audit: regression tests for the defects found and no-lookahead checks for the critical paths that
had none (walk-forward selection, holdout isolation, paper decisions, backtest/paper risk-limit consistency).

All data is synthetic: these tests check the machinery, never a market."""

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from qal.anomaly.engine import NO_PRICE_MAGNITUDE, ScanConfig, run_scan
from qal.anomaly.priority import economic_component, score
from qal.config import get_settings
from qal.data.ingest import IngestRequest, ingest
from qal.data.store import ParquetStore
from qal.models import AnomalyDetection, PaperSignal
from qal.paper import engine, risk_gate, service
from qal.paper.config import SessionConfig
from qal.portfolio.risk_manager import GroupLimits, PolicyLimits
from qal.research.contract import HypothesisContract
from qal.research.costs import default_cost_model
from qal.research.validation import Validator

# -- D1/D2/D3: no economic score from a variable that is not a tradable price move -------------------------


def _scan(session, detectors):
    ids = {s: ingest(session, IngestRequest(adapter="synthetic", symbol=s, asset_class="equity"))["dataset_id"]
           for s in ("SYN-A", "SYN-C")}
    exp = run_scan(session, ScanConfig(dataset_ids=list(ids.values()), detectors=detectors),
                   store=ParquetStore(get_settings().data_dir))
    return session.scalars(select(AnomalyDetection).where(AnomalyDetection.experiment_id == exp.id)).all()


def test_volatility_and_volume_events_get_no_move_vs_cost_score(session):
    rows = _scan(session, {"return_shock": {}, "volume_anomaly": {}, "volatility_regime": {}})
    by = {d: [r for r in rows if r.detector == d] for d in ("return_shock", "volume_anomaly", "volatility_regime")}
    assert all(by.values()), {k: len(v) for k, v in by.items()}  # the synthetic series has all three kinds
    for d in ("volume_anomaly", "volatility_regime"):
        for r in by[d]:
            assert r.economic_score is None
            assert "event study" in r.economic_context["reason"]
            assert "move_to_cost_ratio" not in r.economic_context
            assert r.economic_context["method"] == "move_vs_cost@2"
    # the return shock's size IS the detected variable: it keeps its preliminary score
    assert any(r.economic_score is not None for r in by["return_shock"])


@pytest.mark.parametrize("detector", sorted(NO_PRICE_MAGNITUDE))
def test_stored_scores_of_old_scans_do_not_count_towards_priority(detector):
    assert economic_component(detector, 0.86) == (0.0, False)
    s = score(detector=detector, adjusted_p=0.01, stat_score=0.9, econ=0.86, verdict="not_evaluated", quality="pass",
              synthetic=False, duplicate_of=None, age_bars=0.0)
    comp = {c["name"]: c for c in s["components"]}["economic"]
    assert comp["contribution"] == 0 and "no price magnitude" in comp["source"]
    assert economic_component("return_shock", 0.86) == (0.86, True)
    assert economic_component("pair_divergence", 0.8) == (0.4, False)  # documented: halved, not removed


# -- walk-forward selection and holdout isolation ---------------------------------------------------------


def _validator(frame, holdout_start, grid=None):
    c = HypothesisContract.model_validate({
        "title": "Momentum on SYN-A",
        "premise": "Trends persist for some weeks because information diffuses slowly across investors.",
        "mechanism": "Slow diffusion of information and herding keep prices moving in the same direction.",
        "dataset_ids": ["ds-a"], "features": ["trailing return"],
        "strategy": {"type": "ts_momentum", "param_grid": grid or {"lookback": [10, 20, 60, 120]}},
        "entry_rule": "Long when the trailing return is positive", "exit_rule": "Flat or short when it turns",
        "horizon_bars": 10, "risks": ["trend reversals"], "invalidation": ["OOS Sharpe below threshold"],
        "validation": {"train_bars": 300, "test_bars": 100, "embargo_bars": 10, "holdout_start": holdout_start,
                       "placebo_runs": 20, "bootstrap_runs": 100}})
    return Validator(c, [frame], "other", ["SYN-A"], [default_cost_model("other")])


def _poison_after(frame, k):
    p = frame.copy()
    rng = np.random.default_rng(1)
    f = np.exp(np.cumsum(rng.normal(0, 0.05, len(p) - k)))
    for col in ("open", "high", "low", "close", "adj_close"):
        if col in p:
            p.loc[k:, col] = p.loc[k:, col].to_numpy() * f
    return p


def test_walk_forward_choice_uses_only_each_windows_training_bars(syn):
    df = syn["SYN-A"]
    hs = str(df["available_ts"].iloc[1300])
    base = _validator(df, hs).walk_forward()
    w = base["wins"][1]
    # poison everything after the 2nd window's training end: its choice (and the 1st window's) must not move
    moved = _validator(_poison_after(df, w["train_end"]), hs).walk_forward()
    assert moved["chosen_idx"][:2] == base["chosen_idx"][:2]
    assert moved["scores"][1] == base["scores"][1]


def test_holdout_bars_never_reach_validation(syn):
    df = syn["SYN-A"]
    hs = str(df["available_ts"].iloc[1300])
    a = _validator(df, hs).run()
    b = _validator(_poison_after(df, 1300), hs).run()
    assert a["selected_params_last_window"] == b["selected_params_last_window"]
    assert a["oos"]["metrics"] == b["oos"]["metrics"]
    assert a["overfitting"]["dsr"] == b["overfitting"]["dsr"]
    assert all(c["passed"] for c in a["leakage_checks"] if c["check"] in ("holdout_isolated", "walk_forward_separation"))


# -- paper decisions use only bars available at the decision time ----------------------------------------


@pytest.fixture()
def lab(session):
    ids = {s: ingest(session, IngestRequest(adapter="synthetic", symbol=s, asset_class="other",
                                            options={"seed": 7, "n_bars": 700}), actor="test")["dataset_id"]
           for s in ("SYN-A", "SYN-B", "SYN-C")}
    session.commit()
    return ids


def _session(session, ids, name):
    cfg = SessionConfig(name=name, created_by="tester", allow_unvalidated_demo=True,
                        start=pd.Timestamp("2021-06-21", tz="UTC").to_pydatetime(),
                        strategies=[{"strategy_id": "DEMO:ts_momentum", "dataset_ids": [ids["SYN-A"]],
                                     "params": {"lookback": 20}, "allocation": 0.3},
                                    {"strategy_id": "DEMO:pair_mean_reversion", "dataset_ids": [ids["SYN-B"], ids["SYN-C"]],
                                     "params": {"window": 60, "entry_z": 1.0}, "allocation": 0.3}])
    ps = service.create_session(session, cfg)
    service.start(session, ps, "tester")
    session.commit()
    return ps


def _signals(session, sid):
    return [(s.strategy_id, str(s.bar_ts), sorted(s.targets.items())) for s in
            session.scalars(select(PaperSignal).where(PaperSignal.session_id == sid)
                            .order_by(PaperSignal.bar_ts, PaperSignal.strategy_id))]


def test_paper_signals_do_not_change_when_future_bars_are_poisoned(session, lab, monkeypatch):
    clean = _session(session, lab, "clean")
    engine.run(session, clean, 30)
    cursor = engine.utc(clean.cursor_ts)
    real_load = engine.load_runtime

    def poisoned(s, ps):
        rt = real_load(s, ps)
        for ir in rt.instr.values():
            fut = (ir.frame["ts"] > cursor).to_numpy()
            for col in ("open", "high", "low", "close"):
                ir.frame.loc[fut, col] = ir.frame.loc[fut, col] * 3.0
        return rt

    monkeypatch.setattr(engine, "load_runtime", poisoned)
    dirty = _session(session, lab, "poisoned")
    engine.run(session, dirty, 30)
    assert _signals(session, clean.id) and _signals(session, clean.id) == _signals(session, dirty.id)


# -- the paper risk gate applies the same market limit as the portfolio backtest ---------------------------


def test_paper_gate_caps_gross_exposure_per_market_like_the_backtest():
    ctx = risk_gate.GateContext(
        as_of="t", session_status="RUNNING", kill_switch=False, reconciliation_ok=True, strategy_status="DEMO",
        allow_demo=True, data_status="pass", data_age_hours=0.0, max_staleness_hours=96, equity=100_000,
        day_start_equity=100_000, peak_equity=100_000, margin_used=0.0,
        positions={"Y": 40_000.0, "Z": 30_000.0}, asset_class={"X": "equity", "Y": "equity", "Z": "fx"},
        is_spot_crypto=False, prev_dollar_volume=None, max_participation=0.05, max_leverage_contract=5.0)
    limits = PolicyLimits(group=GroupLimits(max_market_gross=0.8))
    r = risk_gate.evaluate(instrument="X", current_units=0, delta_units=500, price_acct=100.0, lot_units=1,
                           min_units=1, ctx=ctx, limits=limits)
    # equity market: 40k already held, room for 40k more of X; the fx position does not count
    assert r.approved and r.quantity == 400
    chk = next(c for c in r.checks if c["rule"] == "max_market_gross:equity")
    assert chk["decision"] == "blocked" and chk["limit"] == 0.8


# -- D5: warm-up bars are not part of the rank reference of the volatility and pair p-values ----------------


def test_rank_pvalues_ignore_warm_up_bars(syn):
    from qal.anomaly.detectors import DETECTORS
    from qal.features.basic import trailing_rank_pvalue

    det = DETECTORS["volatility_regime"]()
    out = det.run(syn["SYN-C"])
    lr = out.series.to_numpy()
    p = np.asarray(out.pvalues, float)
    w = det.params["rank_window"]
    first_lr = int(np.flatnonzero(np.isfinite(lr))[0])
    first_p = int(np.flatnonzero(np.isfinite(p))[0])
    assert first_p == first_lr + w  # a full window of real values before the first p-value
    fin = lr[np.isfinite(lr)]
    np.testing.assert_allclose(p[np.isfinite(p)], trailing_rank_pvalue(fin, w)[w:])

    pair = DETECTORS["pair_divergence"]()
    po = pair.run(syn["SYN-B"], syn["SYN-A"])
    z, pp = po.series.to_numpy(), np.asarray(po.pvalues, float)
    assert int(np.flatnonzero(np.isfinite(pp))[0]) == int(np.flatnonzero(np.isfinite(z))[0]) + pair.params["rank_window"]


# -- end-to-end chain ------------------------------------------------------------------------------------


def test_end_to_end_chain_runs_every_stage_and_stops_where_the_evidence_stops(session):
    from qal import e2e

    r = e2e.run(session, e2e.E2EConfig(options={"seed": 7, "n_bars": 1500}, max_hypotheses=2))
    status = r["summary"]
    assert list(status) == ["data", "quality", "anomaly", "event_study", "hypothesis", "validation", "strategy",
                            "portfolio", "risk", "paper", "monitoring", "alert"]
    assert "failed" not in status.values(), [s for s in r["stages"] if s["status"] == "failed"]
    assert status["monitoring"] == "not_implemented"
    paper = next(s for s in r["stages"] if s["stage"] == "paper")
    if status["validation"] == "blocked":
        assert r["validated_path_stops_at"] == "validation"
        assert paper["path"].startswith("demo") and all(x["refused"] for x in paper["refused_unvalidated"])
    assert "not an approval" in paper["note"]


# -- D7: a bar still forming at download time is not stored as a closed bar --------------------------------


def test_bar_still_forming_at_download_is_not_stored(session):
    from tests.test_ingest_engine import FrameAdapter, _frame

    f = _frame(300)
    last = f.index[-1]
    now = (last + pd.Timedelta(hours=12)).to_pydatetime()  # the last daily bar closes at last + 1 day
    r = ingest(session, IngestRequest(adapter="frame", symbol="OPEN", asset_class="equity"),
               adapter=FrameAdapter(f), now=now)
    from qal.models import Dataset, DataQualityEvent

    ds = session.get(Dataset, r["dataset_id"])
    assert ds.rows == 299 and pd.Timestamp(ds.end_ts).tz_localize(None) < last.tz_localize(None)
    ev = session.scalars(select(DataQualityEvent).where(DataQualityEvent.dataset_id == ds.id,
                                                        DataQualityEvent.check == "incomplete_bars_dropped")).all()
    assert len(ev) == 1 and ev[0].count == 1
    # once the bar has closed, the next ingest stores it
    later = (last + pd.Timedelta(days=2)).to_pydatetime()
    r2 = ingest(session, IngestRequest(adapter="frame", symbol="OPEN", asset_class="equity"),
                adapter=FrameAdapter(f), now=later)
    assert session.get(Dataset, r2["dataset_id"]).rows == 300
