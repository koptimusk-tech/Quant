"""End-to-end audit run: data -> quality -> anomaly -> event study -> hypothesis -> validation -> strategy
eligibility -> portfolio -> risk -> paper -> monitoring -> alert.

Every stage calls the same services as the API (nothing is mocked) and records its status:

* ``ok``: the stage ran and produced what the next stage needs;
* ``blocked``: the stage ran but a control stopped the chain (e.g. no strategy passed the gates), which is a
  correct outcome, not a failure;
* ``not_implemented``: the component does not exist;
* ``failed``: the stage raised.

When the validated path is blocked, the downstream stages are still exercised with clearly labelled
UNVALIDATED DEMO strategies, so the machinery is checked without presenting anything as validated. The
report says which stages ran on the validated path and which only on the demo path.

Data: synthetic by default (deterministic, with injected events). ``symbols`` with ``adapter`` other than
synthetic downloads real history; the run never mixes the two. No order is sent anywhere: paper only.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qal.anomaly.engine import ScanConfig, run_scan
from qal.anomaly.event_study import run_event_study
from qal.config import get_settings
from qal.data.ingest import IngestRequest, ingest
from qal.data.store import ParquetStore
from qal.models import AnomalyDetection, Dataset, PaperEvent, RiskDecision
from qal.paper import engine as paper_engine
from qal.paper import evaluation as paper_evaluation
from qal.paper import service as paper_service
from qal.paper.config import SessionConfig
from qal.portfolio.service import PortfolioRunIn, run_portfolio
from qal.portfolio.strategies import EligibilityCriteria, catalog
from qal.research.contract import HypothesisContract
from qal.research.proposals import build_proposals
from qal.research.service import create_hypothesis, run_validation
from qal.versioning import code_version

VERSION = "1.0.0"


@dataclass
class E2EConfig:
    adapter: str = "synthetic"
    symbols: tuple[str, ...] = ("SYN-A", "SYN-B", "SYN-C", "SYN-D")
    asset_class: str = "other"
    options: dict[str, Any] = field(default_factory=lambda: {"seed": 7, "n_bars": 1500})
    max_hypotheses: int = 3
    paper_bars: int = 60


def _stage(report: list[dict[str, Any]], name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        out = fn()
    except Exception as exc:  # recorded, the chain goes on where it can
        out = {"status": "failed", "error": f"{type(exc).__name__}: {exc}",
               "trace": traceback.format_exc(limit=3)}
    out["stage"] = name
    out["seconds"] = round(time.perf_counter() - t0, 2)
    report.append(out)
    return out


def run(s: Session, cfg: E2EConfig | None = None) -> dict[str, Any]:
    cfg = cfg or E2EConfig()
    store = ParquetStore(get_settings().data_dir)
    st: list[dict[str, Any]] = []
    ctx: dict[str, Any] = {}

    def data() -> dict[str, Any]:
        ids = {}
        for sym in cfg.symbols:
            r = ingest(s, IngestRequest(adapter=cfg.adapter, symbol=sym, asset_class=cfg.asset_class,
                                        options=dict(cfg.options)), actor="e2e")
            ids[sym] = r["dataset_id"]
        s.commit()
        ctx["ids"] = ids
        rows = {sym: s.get(Dataset, i).rows for sym, i in ids.items()}
        return {"status": "ok", "datasets": ids, "rows": rows,
                "is_synthetic": all(s.get(Dataset, i).is_synthetic for i in ids.values())}

    def quality() -> dict[str, Any]:
        q = {sym: s.get(Dataset, i).quality_status for sym, i in ctx["ids"].items()}
        usable = [sym for sym, v in q.items() if v != "fail"]
        ctx["usable"] = usable
        return {"status": "ok" if usable else "blocked", "quality": q,
                "note": "Gate A: datasets with status fail are excluded from every later stage."}

    def anomaly() -> dict[str, Any]:
        ids = [ctx["ids"][x] for x in ctx["usable"]]
        pairs = [(ids[i + 1], ids[i]) for i in range(0, len(ids) - 1, 2)]
        exp = run_scan(s, ScanConfig(dataset_ids=ids, detectors={"return_shock": {}, "volume_anomaly": {},
                                                                 "volatility_regime": {}, "pair_divergence": {}},
                                     pairs=pairs, label="e2e audit"), store=store, actor="e2e")
        s.commit()
        n = s.scalar(select(func.count()).select_from(AnomalyDetection).where(AnomalyDetection.experiment_id == exp.id))
        sig = s.scalar(select(func.count()).select_from(AnomalyDetection).where(
            AnomalyDetection.experiment_id == exp.id, AnomalyDetection.adjusted_pvalue < 0.05))
        ctx["scan"] = exp.id
        return {"status": "ok" if exp.status == "succeeded" else "failed", "experiment_id": exp.id,
                "n_tests": exp.n_tests, "detections": n, "bh_significant_005": sig,
                "note": "Detections are statistically unusual bars (level 1). They are not signals."}

    def event_study() -> dict[str, Any]:
        out = []
        for sym in ctx["usable"][:2]:
            for det in ("return_shock", "volatility_regime"):
                e = run_event_study(s, ctx["ids"][sym], det, store=store, actor="e2e")
                out.append({"symbol": sym, "detector": det, "verdict": (e.results or {}).get("verdict"),
                            "experiment_id": e.id})
        s.commit()
        return {"status": "ok", "studies": out,
                "note": "oos_supported = level 3 (out-of-sample association); never a validated strategy."}

    def hypothesis() -> dict[str, Any]:
        props = [p for p in build_proposals(s) if not p["existing_hypotheses"]][: cfg.max_hypotheses]
        hyps = []
        for p in props:
            h = create_hypothesis(s, HypothesisContract.model_validate(p["contract"]), actor="e2e")
            hyps.append({"id": h.id, "title": h.title, "kind": p["kind"], "variants": p["variants"],
                         "source_anomalies": len(p["contract"].get("source_anomaly_ids", []))})
        s.commit()
        ctx["hyps"] = [h["id"] for h in hyps]
        return {"status": "ok" if hyps else "blocked", "hypotheses": hyps,
                "note": "Proposals come from deterministic rules over the stored anomalies; each is a frozen contract."}

    def validation() -> dict[str, Any]:
        from qal.models import Hypothesis

        rows = []
        for hid in ctx.get("hyps", []):
            h = s.get(Hypothesis, hid)
            e = run_validation(s, h, actor="e2e")
            r = e.results or {}
            rows.append({"hypothesis": hid, "status": e.status, "decision": r.get("decision"),
                         "failed_gates": [g["gate"] for g in r.get("gates", []) if not g["passed"]],
                         "oos_sharpe_net": ((r.get("oos") or {}).get("metrics") or {}).get("sharpe_net"),
                         "dsr": (r.get("overfitting") or {}).get("dsr"), "pbo": (r.get("overfitting") or {}).get("pbo"),
                         "oos_mean_net_by_cost_multiple": {
                             str(c["multiplier"]): c["mean_net"]
                             for c in (r.get("robustness") or {}).get("cost_sensitivity", [])},
                         "error": e.error})
            s.commit()
        passed = [r for r in rows if r["decision"] == "VALIDATION_PASSED"]
        return {"status": "ok" if passed else "blocked", "validations": rows,
                "note": "Gates A-D are deterministic; a blocked chain here is a correct outcome when the evidence "
                        "is not there."}

    def strategy() -> dict[str, Any]:
        recs = catalog(s, EligibilityCriteria(allow_synthetic=True))
        ctx["eligible"] = [r.id for r in recs if r.eligible]
        return {"status": "ok" if ctx["eligible"] else "blocked",
                "catalog": [{"id": r.id, "eligible": r.eligible, "reasons": r.reasons[:4]} for r in recs],
                "note": "allow_synthetic=True here only so a synthetic run can reach this check; no synthetic "
                        "result is evidence."}

    def portfolio() -> dict[str, Any]:
        validated = bool(ctx.get("eligible"))
        ids = ctx["eligible"] if validated else ["SYN:trend-eur", "SYN:carry", "SYN:meanrev"]
        crit = EligibilityCriteria(allow_synthetic=True)
        exp = run_portfolio(s, PortfolioRunIn(strategy_ids=ids, criteria=crit, mc_paths=200, stress_backtests=False,
                                              label="e2e audit"), actor="e2e")
        s.commit()
        ctx["portfolio"] = exp.id
        res = exp.results or {}
        return {"status": "ok" if exp.status == "succeeded" else "failed", "path": "validated" if validated else
                "demo (synthetic strategies)", "experiment_id": exp.id, "error": exp.error,
                "rejected": res.get("rejected"), "decisions_stored": res.get("decisions_stored")}

    def risk() -> dict[str, Any]:
        n = s.scalar(select(func.count()).select_from(RiskDecision).where(RiskDecision.run_id == ctx.get("portfolio")))
        blocked = s.scalar(select(func.count()).select_from(RiskDecision).where(
            RiskDecision.run_id == ctx.get("portfolio"), RiskDecision.decision == "blocked"))
        return {"status": "ok" if n else "failed", "decisions": n, "blocked": blocked,
                "note": "Hierarchical Risk Manager decisions recorded with rule, observed value, limit and version."}

    def paper() -> dict[str, Any]:
        from qal.models import Hypothesis

        validated = [i for i in ctx.get("eligible", []) if i.startswith("H:")]
        ids = ctx["ids"]
        syms = ctx["usable"]
        if validated:
            strategies = [{"strategy_id": validated[0], "allocation": 0.3}]
            demo = False
        else:
            strategies = [{"strategy_id": "DEMO:ts_momentum", "dataset_ids": [ids[syms[0]]],
                           "params": {"lookback": 20}, "allocation": 0.3}]
            demo = True
            # the validated path must refuse a hypothesis that did not pass
            refused = []
            for hid in ctx.get("hyps", [])[:1]:
                try:
                    paper_service.create_session(s, SessionConfig(
                        name="e2e refused", created_by="e2e", strategies=[{"strategy_id": f"H:{hid}", "allocation": 0.3}]))
                    refused.append({"hypothesis": hid, "refused": False})
                except paper_service.PaperError as exc:
                    refused.append({"hypothesis": hid, "refused": True, "reason": str(exc)[:200],
                                    "status": s.get(Hypothesis, hid).status})
            ctx["refused"] = refused
        df_end = pd.Timestamp(s.get(Dataset, ids[syms[0]]).end_ts)
        df_end = df_end.tz_localize("UTC") if df_end.tzinfo is None else df_end
        start = (df_end - pd.Timedelta(days=int(cfg.paper_bars * 1.5))).to_pydatetime()
        ps = paper_service.create_session(s, SessionConfig(name="e2e audit", created_by="e2e", start=start,
                                                           allow_unvalidated_demo=demo, strategies=strategies))
        paper_service.start(s, ps, "e2e")
        s.commit()
        out = paper_engine.run(s, ps, 10_000)
        ev = paper_evaluation.evaluate(s, ps)
        paper_service.store_evaluation(s, ps, ev)
        s.commit()
        ctx["paper"] = ps.id
        return {"status": "ok", "path": "validated" if validated else "demo (UNVALIDATED)", "session_id": ps.id,
                "bars": out["bars"], "session_status": out["status"], "verdict": ev["verdict"],
                "metrics": {k: ev.get("metrics", {}).get(k) for k in ("paper_return_net", "backtest_return_net",
                                                                      "return_correlation", "signal_agreement",
                                                                      "fill_rate", "orders_rejected")},
                "refused_unvalidated": ctx.get("refused"), "note": ev["note"]}

    def monitoring() -> dict[str, Any]:
        return {"status": "not_implemented",
                "note": "Phase 8 (model monitoring, drift detection, circuit breakers) was not built: its brief was "
                        "not completed. What exists: paper-session data checks (gaps, delays, staleness, bad bars) "
                        "with automatic pause, the kill switch, and the paper vs backtest evaluation."}

    def alert() -> dict[str, Any]:
        rows = s.scalars(select(PaperEvent).where(PaperEvent.session_id == ctx.get("paper"),
                                                  PaperEvent.level.in_(("warn", "error", "critical")))).all()
        return {"status": "ok" if ctx.get("paper") else "blocked", "paper_alerts": len(rows),
                "by_code": {k: int(v) for k, v in pd.Series([r.code for r in rows]).value_counts().items()},
                "note": "Only paper-session events exist; there is no drift or model alert (Phase 8 missing)."}

    for name, fn in (("data", data), ("quality", quality), ("anomaly", anomaly), ("event_study", event_study),
                     ("hypothesis", hypothesis), ("validation", validation), ("strategy", strategy),
                     ("portfolio", portfolio), ("risk", risk), ("paper", paper), ("monitoring", monitoring),
                     ("alert", alert)):
        out = _stage(st, name, fn)
        if out["status"] == "failed" and name in ("data", "quality", "anomaly"):
            break  # nothing downstream can run without data
    first_block = next((x["stage"] for x in st if x["status"] in ("blocked", "failed", "not_implemented")), None)
    return {"version": VERSION, "code_version": code_version(), "config": cfg.__dict__, "stages": st,
            "validated_path_stops_at": first_block,
            "summary": {x["stage"]: x["status"] for x in st},
            "note": "No real order is sent by any stage. Stages after a block run on labelled demo inputs only to "
                    "check the machinery."}


__all__ = ["E2EConfig", "run"]
