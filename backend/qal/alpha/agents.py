"""Four deterministic research agents and one bounded research cycle.

* Alpha Researcher: turns stored observations (anomaly scans, event studies, the relationship scan, factor
  universes) into ideas, each with the observation it comes from and what would refute it.
* Quant Analyst: turns an idea into a frozen hypothesis contract (variables, rule, horizon, costs, primary
  metric and acceptance thresholds, validation design with an isolated holdout), or reports what is missing.
* Adversarial Reviewer: objects to each formalised idea, citing stored evidence. A ``block`` objection stops
  registration; ``major`` and ``minor`` ones are recorded with the hypothesis.
* Research Coordinator: deduplicates, registers what survives review, runs the validation engine for the
  best-ranked hypotheses within the budget, and records every decision with its reason.

None of them computes an official statistic or changes a gate: results come from ``run_validation`` and the
policy engine, and every number in a message is copied from a stored record. No language model is called.
One cycle is a fixed pipeline (no open-ended debate) and stops at the budget.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qal import audit
from qal.alpha.factors import FACTORS, FUNDAMENTALS_GAP, design_for, price_universes
from qal.alpha.messages import (
    Budget,
    DataGap,
    Decision,
    Evidence,
    Formalization,
    Idea,
    Objection,
)
from qal.alpha.relationships import run_relationship_scan
from qal.anomaly.detectors import DETECTORS
from qal.anomaly.event_study import latest_verdicts
from qal.data.store import ParquetStore
from qal.models import AgentMessage, AnomalyDetection, Dataset, Experiment, Hypothesis
from qal.research.contract import HypothesisContract
from qal.research.costs import CostModel
from qal.research.proposals import (
    DEFAULT_MOMENTUM_PREMISE,
    HOLD_GRID,
    MOMENTUM_PREMISE,
    _common,
    _design,
    _label,
)
from qal.research.service import create_hypothesis, run_validation
from qal.versioning import code_version, stable_hash

VERSION = "1.0.0"
RESEARCHER, ANALYST, REVIEWER, COORDINATOR = ("alpha_researcher", "quant_analyst", "adversarial_reviewer",
                                              "research_coordinator")
MOMENTUM_GRID = {"1h": [24, 120, 480], "4h": [6, 30, 120], "1d": [20, 60, 120], "1wk": [4, 12, 26]}
INTRADAY = ("1m", "5m", "15m", "30m", "1h", "4h")
MIN_EVENTS = 10


# -- identity --------------------------------------------------------------------------------------
def mechanism_tag(strategy: dict[str, Any]) -> str:
    """What distinguishes two hypotheses on the same instruments with the same strategy type."""
    t = strategy.get("type", "")
    p, g = strategy.get("params", {}), strategy.get("param_grid", {})

    def val(k: str, default: Any = "") -> str:
        return "/".join(map(str, sorted(g[k], key=str))) if k in g else str(p.get(k, default))

    if t == "event":
        return f"{val('detector', 'return_shock')}:{val('mode', 'reversal')}"
    if t == "cross_sectional_factor":
        return val("factor", "momentum")
    if t == "ts_momentum":
        return val("regime", "all")
    return ""


def idea_key(strategy: dict[str, Any], instrument_ids: list[str]) -> str:
    raw = f"{strategy.get('type', '')}|{mechanism_tag(strategy)}|{','.join(sorted(instrument_ids))}"
    return "idea_" + hashlib.sha256(raw.encode()).hexdigest()[:20]


# -- context ---------------------------------------------------------------------------------------
@dataclass
class Context:
    session: Session
    budget: Budget
    datasets: list[Dataset]
    by_id: dict[str, Dataset]
    scan_params: dict[tuple[str, str], dict[str, Any]]
    anomalies: dict[tuple[str, str, str | None], list[AnomalyDetection]]
    verdicts: dict[tuple[str, str], dict[str, Any]]
    existing: dict[str, list[dict[str, Any]]]
    relationship_scan: Experiment | None
    universes: list[dict[str, Any]]
    costs: dict[str, CostModel] = field(default_factory=dict)  # by symbol, explicit account costs

    def ds_time(self, ds: Dataset) -> pd.Series:
        return pd.Series(ParquetStore.read(ds.path)["ts"])


def build_context(session: Session, budget: Budget, costs: dict[str, CostModel] | None = None,
                  actor: str = COORDINATOR, run_relationships: bool = True) -> Context:
    """``run_relationships=False`` reuses the latest stored relationship scan instead of computing one (read-only)."""
    q = select(Dataset).where(Dataset.is_latest.is_(True), Dataset.quality_status != "fail")
    if not budget.include_synthetic:
        q = q.where(Dataset.is_synthetic.is_(False))
    datasets = sorted(session.scalars(q).all(), key=lambda d: (d.instrument.symbol, d.frequency, d.id))
    by_id = {d.id: d for d in datasets}
    anomalies: dict[tuple[str, str, str | None], list[AnomalyDetection]] = {}
    for a in session.scalars(select(AnomalyDetection).where(AnomalyDetection.dataset_id.in_(list(by_id)))
                             .order_by(AnomalyDetection.event_ts, AnomalyDetection.id)).all():
        anomalies.setdefault((a.dataset_id, a.detector, a.related_dataset_id), []).append(a)
    # Parameters of the latest scan per (dataset, detector): ideas reuse them so evidence and rule match.
    scan_params: dict[tuple[str, str], dict[str, Any]] = {}
    for (ds_id, det, _), rows in anomalies.items():
        exp = session.get(Experiment, max(rows, key=lambda a: a.created_at).experiment_id)
        scan_params[(ds_id, det)] = {**DETECTORS[det].defaults,
                                     **((exp.config or {}).get("detectors", {}).get(det) or {})} if exp else {}
    ds_instrument = dict(session.execute(select(Dataset.id, Dataset.instrument_id)).all())
    trials = dict(session.execute(select(Hypothesis.family_id, func.count()).group_by(Hypothesis.family_id)).all())
    existing: dict[str, list[dict[str, Any]]] = {}
    for h in session.scalars(select(Hypothesis).order_by(Hypothesis.created_at)).all():
        c = h.contract or {}
        key = idea_key(c.get("strategy", {}), [ds_instrument.get(i, i) for i in c.get("dataset_ids", [])])
        existing.setdefault(key, []).append({"id": h.id, "title": h.title, "status": h.status,
                                             "versions_in_family": trials.get(h.family_id, 1)})
    if run_relationships:
        rel = run_relationship_scan(session, include_synthetic=budget.include_synthetic, actor=actor)
    else:
        rel = next((e for e in session.scalars(select(Experiment).where(
            Experiment.kind == "relationship_scan", Experiment.status == "succeeded")
            .order_by(Experiment.created_at.desc())).all()
            if (e.config or {}).get("include_synthetic") == budget.include_synthetic), None)
    return Context(session, budget, datasets, by_id, scan_params, anomalies,
                   latest_verdicts(session, list(by_id)), existing, rel,
                   price_universes(session, budget.include_synthetic), costs or {})


def _anomaly_evidence(ctx: Context, ds: Dataset, det: str, related: str | None = None) -> list[Evidence]:
    rows = ctx.anomalies.get((ds.id, det, related), [])
    out = []
    if rows:
        adj = [a.adjusted_pvalue for a in rows if a.adjusted_pvalue is not None]
        out.append(Evidence(kind="anomalies", ref=rows[-1].experiment_id,
                            summary=f"{len(rows)} {det} detections on {_label(ds)}",
                            values={"n": len(rows), "n_significant": sum(p < 0.05 for p in adj),
                                    "min_adjusted_pvalue": min(adj) if adj else None,
                                    "first": rows[0].event_ts.isoformat(), "last": rows[-1].event_ts.isoformat(),
                                    "scan_params": ctx.scan_params.get((ds.id, det), {})}))
    v = ctx.verdicts.get((ds.id, det))
    if v:
        out.append(Evidence(kind="event_study", ref=v["experiment_id"], summary=f"event study: {v['verdict']}",
                            values={"verdict": v["verdict"], "verdict_text": v.get("verdict_text")}))
    return out


# -- Alpha Researcher -------------------------------------------------------------------------------
class AlphaResearcher:
    name = RESEARCHER

    def propose(self, ctx: Context) -> tuple[list[Idea], list[DataGap]]:
        ideas: dict[str, Idea] = {}
        gaps: list[DataGap] = []

        def add(idea: Idea) -> None:
            if idea.key in ideas:  # same idea reached by two routes: one idea, both sources of evidence
                prev = ideas[idea.key]
                prev.evidence += [e for e in idea.evidence if e not in prev.evidence]
                prev.priority = max(prev.priority, idea.priority)
            else:
                ideas[idea.key] = idea

        for ds in ctx.datasets:
            for idea in self._shock_ideas(ctx, ds):
                add(idea)
            for idea in self._momentum_ideas(ctx, ds):
                add(idea)
            gaps.append(self._volume_gap(ctx, ds))
        for idea in self._divergence_ideas(ctx):
            add(idea)
        for idea in self._relationship_ideas(ctx):
            add(idea)
        f_ideas, f_gaps = self._factor_ideas(ctx)
        for idea in f_ideas:
            add(idea)
        gaps += f_gaps
        if not any(d.instrument.asset_class == "future" for d in ctx.datasets):
            gaps.append(DataGap(key="gap_futures_term_structure", route="anomaly", family="futures_spot",
                                reason="No futures data in the lab: basis, roll yield and term-structure ideas "
                                       "(futures vs spot, near vs far maturity) cannot be tested.",
                                needs=["Futures series per maturity with expiry dates", "The matching spot series",
                                       "Roll calendar and roll costs"]))
        out = sorted(ideas.values(), key=lambda i: (-i.priority, i.key))[: ctx.budget.max_ideas]
        return out, [g for g in gaps if g is not None]

    @staticmethod
    def _shock_ideas(ctx: Context, ds: Dataset) -> list[Idea]:
        rows = ctx.anomalies.get((ds.id, "return_shock", None), [])
        if len(rows) < MIN_EVENTS:
            return []
        p = ctx.scan_params.get((ds.id, "return_shock"), DETECTORS["return_shock"].defaults)
        hold = HOLD_GRID.get(ds.frequency, [2, 5, 10])
        ev = _anomaly_evidence(ctx, ds, "return_shock")
        n_sig = ev[0].values["n_significant"]
        verdict = ctx.verdicts.get((ds.id, "return_shock"), {}).get("verdict")
        prio = 0.2 + 0.3 * min(len(rows) / 100, 1) + (0.15 if n_sig else 0) \
            + {"oos_supported": 0.3, "in_sample_only": 0.0, "no_evidence": -0.2}.get(verdict or "", 0)
        obs = (f"The scans flagged {len(rows)} return shocks (robust |z| >= {p.get('threshold')}) on {_label(ds)}, "
               f"{n_sig} significant after multiple-testing control"
               + (f"; the last event study's verdict was {verdict}" if verdict else "; no event study yet") + ".")
        out = []
        for mode, family, direction, premise, mech in (
            ("reversal", "mean_reversion", "both",
             ("Extreme one-bar moves are partly caused by liquidity demand, so part of the move is reversed in "
             "the following bars once liquidity providers return."),
             ("Market orders that hit thin books push prices past fair value; the overshoot is absorbed when "
             "liquidity providers step back in, so returns after a shock have the opposite sign on average.")),
            ("continuation", "shock_continuation", "both",
             ("Extreme one-bar moves often carry new information that slower participants trade on afterwards, "
             "so the move continues in the following bars."),
             ("Information arrives to some participants first; the rest adjust positions over several bars, so "
             "returns after a shock keep the sign of the shock on average.")),
        ):
            strategy = {"type": "event", "params": {"detector": "return_shock", "mode": mode,
                                                    "threshold": float(p.get("threshold", 5.0)),
                                                    "window": int(p.get("window", 250))},
                        "param_grid": {"hold_bars": hold}}
            out.append(Idea(
                key=idea_key(strategy, [ds.instrument_id]), route="anomaly", family=family,
                title=f"{_label(ds)}: {mode} after return shocks", dataset_ids=[ds.id], origin_observation=obs,
                premise=premise, mechanism=mech, strategy=strategy, horizon_bars=max(hold),
                expected_direction=direction,
                entry_rule=f"At the first open after a bar with robust |z| >= {p.get('threshold')}, trade "
                           + ("against" if mode == "reversal" else "with") + " the shock",
                exit_rule=f"Exit after a fixed holding period ({', '.join(map(str, hold))} bars, chosen in-sample "
                          "in each walk-forward window)",
                features=[f"return_shock robust z-score (window {p.get('window')})"],
                refutation=["Net out-of-sample Sharpe at or below zero",
                            "Fewer than 30 out-of-sample trades", "Mean net return <= 0 at 2x costs",
                            "Placebo p-value above 0.1"],
                risks=["Spreads widen exactly when shocks happen", "Few independent events"],
                source_anomaly_ids=[a.id for a in rows[-20:]], evidence=ev, priority=round(prio, 4)))
        return out

    @staticmethod
    def _momentum_ideas(ctx: Context, ds: Dataset) -> list[Idea]:
        grid = MOMENTUM_GRID.get(ds.frequency)
        if not grid:
            return []
        ac = ds.instrument.asset_class
        base_ev = [Evidence(kind="dataset", ref=ds.id, summary=f"{_label(ds)}: {ds.rows} bars",
                            values={"rows": ds.rows, "quality_status": ds.quality_status})]
        out = []
        strategy = {"type": "ts_momentum", "params": {}, "param_grid": {"lookback": grid}}
        if ds.frequency in ("1d", "1wk"):
            out.append(Idea(
                key=idea_key(strategy, [ds.instrument_id]), route="prior", family="momentum",
                title=f"{_label(ds)}: time-series momentum", dataset_ids=[ds.id],
                origin_observation="No anomaly in the lab points to it: trend persistence is a documented effect "
                                   "across asset classes, tested here as a prior.",
                premise=MOMENTUM_PREMISE.get(ac, DEFAULT_MOMENTUM_PREMISE),
                mechanism="Slow-moving flows and gradual repricing keep returns positively autocorrelated at "
                          "multi-week horizons, so the sign of the trailing return predicts the next bar's sign.",
                strategy=strategy, horizon_bars=1, expected_direction="both",
                entry_rule="Hold the sign of the trailing lookback return from the next open",
                exit_rule="Re-evaluated every bar; the position flips when the trailing return changes sign",
                features=[f"{g}-bar trailing return sign" for g in grid],
                refutation=["Net out-of-sample Sharpe at or below zero", "Edge disappears when costs are doubled"],
                risks=["Sharp reversals around policy surprises", "Long flat periods where costs dominate"],
                evidence=base_ev, priority=0.35))
        vol = ctx.anomalies.get((ds.id, "volatility_regime", None), [])
        if len(vol) >= MIN_EVENTS:
            ev = _anomaly_evidence(ctx, ds, "volatility_regime") + base_ev
            p = ctx.scan_params.get((ds.id, "volatility_regime"), {})
            for regime, prem in (
                ("high_vol", ("Trends are stronger after volatility rises, because shocks that move volatility "
                             "also start repricing that takes weeks to complete.")),
                ("low_vol", ("Trends are cleaner in calm markets, because high-volatility periods are dominated by "
                            "noise and sharp reversals that whipsaw trend rules.")),
            ):
                st = {"type": "ts_momentum",
                      "params": {"regime": regime, "regime_short": int(p.get("short_window", 20)),
                                 "regime_long": int(p.get("long_window", 250))},
                      "param_grid": {"lookback": grid}}
                out.append(Idea(
                    key=idea_key(st, [ds.instrument_id]), route="anomaly", family="volatility",
                    title=f"{_label(ds)}: momentum only in {regime.replace('_', ' ')} regimes",
                    dataset_ids=[ds.id],
                    origin_observation=f"The scans flagged {len(vol)} volatility-regime changes on {_label(ds)} "
                                       f"(short/long realised volatility ratio, threshold {p.get('threshold')}).",
                    premise=prem,
                    mechanism="The volatility regime changes who trades and how fast information is absorbed, "
                              "which changes the autocorrelation of returns; the regime filter uses only the "
                              "trailing volatility ratio known at the close.",
                    strategy=st, horizon_bars=1, expected_direction="both",
                    entry_rule=f"Hold the sign of the trailing return from the next open while the "
                               f"{st['params']['regime_short']}/{st['params']['regime_long']}-bar volatility ratio is "
                               + ("above 1" if regime == "high_vol" else "at or below 1"),
                    exit_rule="Flat when the regime condition fails; otherwise re-evaluated every bar",
                    features=["trailing return sign", "short/long realised volatility ratio"],
                    refutation=["Net OOS Sharpe at or below zero",
                                "No better than unconditional momentum on the same instrument"],
                    risks=["Regime filter cuts the number of trades", "Volatility says nothing about direction"],
                    source_anomaly_ids=[a.id for a in vol[-20:]], evidence=ev, priority=0.3))
        return out

    @staticmethod
    def _volume_gap(ctx: Context, ds: Dataset) -> DataGap | None:
        if ds.instrument.asset_class == "fx":
            reason = ("FX volume from this provider is tick volume (number of price updates), not traded size; "
                      "volume/liquidity ideas are not tested on it.")
        elif not ctx.anomalies.get((ds.id, "volume_anomaly", None)):
            reason = "The scans found no usable volume anomalies (coverage below 80% or constant volume)."
        else:
            reason = ("Volume events mark activity, not price direction; a volume-conditioned rule is not "
                      "implemented yet, so these anomalies are not turned into a directional test.")
        return DataGap(key=f"gap_volume_{ds.id}", route="anomaly", family="volume", reason=reason,
                       needs=["Exchange-traded volume or order-book depth", "A volume-conditioned strategy rule"])

    @staticmethod
    def _pair_idea(ctx: Context, a: Dataset, b: Dataset, route: str, obs: str, ev: list[Evidence],
                   prio: float, sources: list[str]) -> Idea:
        strategy = {"type": "pair_mean_reversion", "params": {"exit_z": 0.5, "max_hold": 20},
                    "param_grid": {"entry_z": [2.0, 2.5]}}
        return Idea(
            key=idea_key(strategy, [a.instrument_id, b.instrument_id]), route=route,
            family="divergence" if route == "anomaly" else "mean_reversion",
            title=f"{_label(a)} vs {b.instrument.symbol}: spread mean reversion", dataset_ids=[a.id, b.id],
            origin_observation=obs,
            premise=f"{a.instrument.symbol} and {b.instrument.symbol} share common drivers, so temporary "
                    "divergences in their hedged relative price tend to close.",
            mechanism="Relative-value traders trade the two legs against each other, which pulls the hedged spread "
                      "back towards its rolling equilibrium after idiosyncratic shocks.",
            strategy=strategy, horizon_bars=20, expected_direction="spread_reversion",
            entry_rule="Open the spread when its rolling hedge-ratio z-score passes the entry level, at the next open",
            exit_rule="Close when the z-score is back inside 0.5 or after 20 bars",
            features=["rolling OLS hedge ratio", "spread z-score"],
            refutation=["Net OOS Sharpe at or below zero", "Spread does not revert within 20 bars on average",
                        "Correlation of the legs changes between halves of the sample"],
            risks=["Permanent break of the relationship", "Two legs double the costs"],
            source_anomaly_ids=sources, evidence=ev, priority=round(prio, 4))

    def _divergence_ideas(self, ctx: Context) -> list[Idea]:
        out = []
        for (ds_id, det, rel), rows in sorted(ctx.anomalies.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "")):
            if det != "pair_divergence" or rel is None or rel not in ctx.by_id or len(rows) < MIN_EVENTS:
                continue
            a, b = ctx.by_id[ds_id], ctx.by_id[rel]
            ev = _anomaly_evidence(ctx, a, det, rel)
            out.append(self._pair_idea(ctx, a, b, "anomaly",
                                       f"The scans flagged {len(rows)} divergences between {a.instrument.symbol} and "
                                       f"{b.instrument.symbol} ({a.frequency}).", ev, 0.3, [r.id for r in rows[-20:]]))
        return out

    def _relationship_ideas(self, ctx: Context) -> list[Idea]:
        if ctx.relationship_scan is None:
            return []
        out = []
        for r in (ctx.relationship_scan.results or {}).get("pairs", []):
            if r.get("status") != "ok" or not r.get("tradable_reversion"):
                continue
            a, b = ctx.by_id.get(r["dataset_a"]), ctx.by_id.get(r["dataset_b"])
            if a is None or b is None:
                continue
            ev = [Evidence(kind="relationship_test", ref=ctx.relationship_scan.id,
                           summary=f"{r['symbol_a']}/{r['symbol_b']} {r['frequency']}: cointegrated after BH",
                           values={k: r.get(k) for k in ("coint_adj_pvalue", "half_life_bars", "return_corr",
                                                         "corr_change_adj_pvalue", "bars", "research_end")})]
            out.append(self._pair_idea(
                ctx, a, b, "relationship",
                f"Relationship scan: {r['symbol_a']}/{r['symbol_b']} ({r['frequency']}) cointegrated on the research "
                f"period (BH-adjusted p {r['coint_adj_pvalue']:.3g}, residual half-life {r['half_life_bars']:.1f} "
                "bars).", ev, 0.45, []))
        return out

    @staticmethod
    def _factor_ideas(ctx: Context) -> tuple[list[Idea], list[DataGap]]:
        ideas, gaps = [], []
        for f in FACTORS.values():
            if f.data != "prices":
                gaps.append(DataGap(key=f"gap_factor_{f.name}", route="factor", family="factor",
                                    reason=f"{f.title}: {FUNDAMENTALS_GAP}", needs=[f.universe] + f.variables))
                continue
            if not ctx.universes:
                gaps.append(DataGap(key=f"gap_factor_{f.name}", route="factor", family="factor",
                                    reason=f"{f.title}: no universe of 3+ comparable daily datasets with enough "
                                           "common history.", needs=[f.universe]))
            for u in ctx.universes:
                strategy = {"type": "cross_sectional_factor", "params": dict(f.strategy_params),
                            "param_grid": {k: list(v) for k, v in f.param_grid.items()}}
                insts = [ctx.by_id[i].instrument_id for i in u["dataset_ids"]]
                ideas.append(Idea(
                    key=idea_key(strategy, insts), route="factor", family="factor",
                    title=f"{f.title}: {u['asset_class']} {u['frequency']} ({len(insts)} instruments)",
                    dataset_ids=u["dataset_ids"],
                    origin_observation=f"Factor module {f.name}: universe of {len(insts)} instruments "
                                       f"({', '.join(u['symbols'])}) with {u['common_bars']} common bars.",
                    premise=f.premise, mechanism=f.mechanism, strategy=strategy, horizon_bars=21,
                    expected_direction="long_short", entry_rule=f.formula, exit_rule=f"Rebalanced: {f.rebalance}",
                    features=f.variables, refutation=f.refutation,
                    risks=["Small universe: a few instruments drive the result", "Factor crowding and reversals"],
                    evidence=[Evidence(kind="factor_spec", ref=f.name, summary=f.title,
                                       values={k: u[k] for k in ("symbols", "common_bars", "start", "end",
                                                                 "holdout_start")})],
                    priority=0.4))
        return ideas, gaps


# -- Quant Analyst ----------------------------------------------------------------------------------
class QuantAnalyst:
    name = ANALYST

    def formalize(self, ctx: Context, idea: Idea) -> Formalization | DataGap:
        dss = [ctx.by_id[i] for i in idea.dataset_ids]
        design = self._design(ctx, idea, dss)
        if design is None:
            return DataGap(key=idea.key, route=idea.route, family=idea.family,
                           reason="Not enough history for walk-forward windows plus an isolated 20% holdout",
                           needs=["Longer history for " + ", ".join(_label(d) for d in dss)])
        assumptions = ["Signals use only bars available at the decision time; trades fill at the next open",
                       "Costs: per-dataset cost model (spread, commission, slippage, financing) on every trade",
                       "Walk-forward selection of the variant on train windows only; embargo >= horizon",
                       "Returns of overlapping windows are not independent: inference uses block bootstrap"]
        limitations = []
        if any(ctx.anomalies.get((d.id, det, None)) for d in dss for det in ("return_shock", "volatility_regime")) \
                and idea.route == "anomaly":
            limitations.append("The anomaly scan that motivated this idea read the whole dataset, holdout period "
                               "included; the holdout is therefore not fully independent of the idea's selection.")
        costs = {d.id: ctx.costs[d.instrument.symbol] for d in dss if d.instrument.symbol in ctx.costs}
        contract = _common(idea.title, idea.dataset_ids, idea.features, idea.strategy, idea.entry_rule,
                           idea.exit_rule, idea.horizon_bars, idea.premise, idea.mechanism, idea.risks,
                           idea.refutation, design, idea.source_anomaly_ids)
        contract.update({
            "origin": idea.route, "origin_observation": idea.origin_observation,
            "expected_direction": idea.expected_direction, "assumptions": assumptions,
            "data_requirements": [f"{_label(d)} ({d.rows} bars, quality {d.quality_status})" for d in dss],
            "limitations": limitations, "costs": {k: v.model_dump() for k, v in costs.items()},
        })
        valid = HypothesisContract.model_validate(contract)  # pre-registration: metric and thresholds fixed here
        signal = {"event": "event at bar t -> position from t+1 open for hold_bars",
                  "ts_momentum": "sign(sum of log returns over lookback) at t -> position from t+1 open",
                  "pair_mean_reversion": "rolling hedge-ratio spread z-score crosses entry -> spread position",
                  "cross_sectional_factor": "rank scores at rebalance bar t -> long top / short bottom from t+1"}
        return Formalization(key=idea.key, contract=valid.model_dump(mode="json"), variables=idea.features,
                             signal_rule=signal.get(idea.strategy["type"], idea.entry_rule),
                             assumptions=assumptions, data_requirements=contract["data_requirements"])

    @staticmethod
    def _design(ctx: Context, idea: Idea, dss: list[Dataset]) -> dict[str, Any] | None:
        if idea.strategy["type"] == "cross_sectional_factor":
            u = next((u for u in ctx.universes if u["dataset_ids"] == idea.dataset_ids), None)
            return design_for(u, idea.horizon_bars) if u else None
        if len(dss) == 1:
            return _design(dss[0], idea.horizon_bars)
        # Pairs: the holdout starts at 80% of the common bars, the same cut the relationship scan stopped at.
        common = None
        for d in dss:
            ts = pd.Index(ctx.ds_time(d))
            common = ts if common is None else common.intersection(ts)
        common = common.sort_values()
        research = int(len(common) * 0.8)
        train, test = int(min(max(research * 0.4, 50), 2000)), int(min(max(research * 0.1, 10), 500))
        embargo = max(idea.horizon_bars, 1)
        if research < train + embargo + 2 * test:
            return None
        return {"train_bars": train, "test_bars": test, "embargo_bars": embargo,
                "holdout_start": pd.Timestamp(common[research]).isoformat()}


# -- Adversarial Reviewer ---------------------------------------------------------------------------
class AdversarialReviewer:
    name = REVIEWER

    def review(self, ctx: Context, idea: Idea, form: Formalization, registered_in_cycle: set[str]) -> list[Objection]:
        c = HypothesisContract.model_validate(form.contract)
        dss = [ctx.by_id[i] for i in c.dataset_ids]
        hs = pd.Timestamp(c.validation.holdout_start)
        out: list[Objection] = []
        if idea.key in ctx.existing or idea.key in registered_in_cycle:
            prev = ctx.existing.get(idea.key, [])
            out.append(Objection(code="duplicate", severity="block",
                                 message="An equivalent hypothesis (same strategy, mechanism and instruments) is "
                                         "already registered; re-testing it under a new name would hide trials.",
                                 evidence=[Evidence(kind="hypothesis", ref=h["id"], summary=h["title"],
                                                    values={"status": h["status"]}) for h in prev]))
        syn = [d.is_synthetic for d in dss]
        if any(syn) and not all(syn):
            out.append(Objection(code="synthetic_mixed", severity="block",
                                 message="Mixes synthetic and real data in one test."))
        elif all(syn):
            out.append(Objection(code="synthetic_only", severity="major",
                                 message="Synthetic data only: a result says nothing about real markets."))
        for d in dss:
            if d.quality_status == "warn":
                out.append(Objection(code="data_quality_warn", severity="minor",
                                     message=f"{_label(d)} has data-quality warnings (gaps or outliers).",
                                     evidence=[Evidence(kind="dataset", ref=d.id, summary=_label(d),
                                                        values={"quality_status": d.quality_status})]))
        st = c.strategy
        if st.type == "event":
            det = st.params.get("detector", "return_shock")
            rows = ctx.anomalies.get((dss[0].id, det, None), [])
            pre = sum(pd.Timestamp(a.event_ts).tz_localize("UTC") < hs if pd.Timestamp(a.event_ts).tzinfo is None
                      else pd.Timestamp(a.event_ts) < hs for a in rows)
            ev = Evidence(kind="anomalies", ref=rows[-1].experiment_id if rows else dss[0].id,
                          summary=f"{pre} of {len(rows)} detections before the holdout",
                          values={"pre_holdout": pre, "total": len(rows), "min_trades": c.success.min_trades})
            if pre < c.success.min_trades:
                out.append(Objection(code="insufficient_events", severity="block",
                                     message=f"Only {pre} events before the holdout; the contract needs at least "
                                             f"{c.success.min_trades} out-of-sample trades, so the test cannot reach "
                                             "a conclusion (insufficient evidence).", evidence=[ev]))
            if max(st.param_grid.get("hold_bars", [st.params.get("hold_bars", 1)])) > 1:
                out.append(Objection(code="overlapping_holds", severity="minor",
                                     message="Holding periods longer than one bar overlap after clustered events; "
                                             "per-bar returns are autocorrelated (block bootstrap accounts for it)."))
        if st.type == "cross_sectional_factor":
            u = next((u for u in ctx.universes if u["dataset_ids"] == c.dataset_ids), None)
            reb = int(st.params.get("rebalance_bars", 21))
            n_reb = int(u["common_bars"] * 0.8 / reb) if u else 0
            if n_reb < c.success.min_trades:
                out.append(Objection(code="insufficient_rebalances", severity="block",
                                     message=f"{n_reb} rebalances before the holdout, fewer than the "
                                             f"{c.success.min_trades} the contract needs (insufficient evidence)."))
            if len(c.dataset_ids) < 6:
                out.append(Objection(code="small_universe", severity="major",
                                     message=f"Universe of {len(c.dataset_ids)} instruments: each quantile holds "
                                             "one or two names, so the factor is close to a bet on single "
                                             "instruments. Check the per-instrument OOS contribution."))
        if idea.route == "anomaly" and any(e.kind in ("anomalies", "event_study") for e in idea.evidence):
            out.append(Objection(code="origin_used_holdout_period", severity="major",
                                 message="The scans or event studies that motivated this idea read bars after "
                                         "holdout_start. The holdout stays untouched by the backtests, but it is not "
                                         "independent of the choice of idea; this is recorded in the contract."))
        for det in {e.values.get("verdict") for e in idea.evidence if e.kind == "event_study"} - {None}:
            if det in ("no_evidence", "in_sample_only"):
                out.append(Objection(code=f"event_study_{det}", severity="major",
                                     message=f"The event study on this signal concluded {det}: the starting "
                                             "observation did not hold out of sample.",
                                     evidence=[e for e in idea.evidence if e.kind == "event_study"]))
            elif det == "insufficient_sample":
                out.append(Objection(code="event_study_insufficient_sample", severity="minor",
                                     message="The event study had too few events to conclude."))
        for e in idea.evidence:
            if e.kind == "relationship_test" and (e.values.get("corr_change_adj_pvalue") or 1) < 0.05:
                out.append(Objection(code="unstable_relationship", severity="major",
                                     message="The correlation of the legs changed between the two halves of the "
                                             "research period (BH-adjusted).", evidence=[e]))
        if st.type == "pair_mean_reversion" and not any(e.kind == "relationship_test" for e in idea.evidence):
            out.append(Objection(code="not_cointegrated", severity="major",
                                 message="The relationship scan did not find this pair cointegrated after "
                                         "multiple-testing control; divergences may not close."))
        if not c.costs:
            out.append(Objection(code="default_costs", severity="major",
                                 message="Costs are asset-class defaults, not the account's real spread, "
                                         "commission and swap; a pass would not be a net-profitability claim."))
        if st.params.get("long_only") and c.benchmark == "cash":
            out.append(Objection(code="long_bias_vs_cash", severity="major",
                                 message="Long-only rule compared with cash: it may only be collecting the drift."))
        if any(d.instrument.asset_class == "fx" and d.frequency in INTRADAY for d in dss):
            out.append(Objection(code="fx_session_timing", severity="minor",
                                 message="Intraday FX bars use a fixed server UTC offset; daylight-saving shifts "
                                         "move session boundaries by one hour twice a year."))
        if idea.family == "volatility":
            out.append(Objection(code="volatility_not_direction", severity="minor",
                                 message="A volatility regime says nothing about direction; the edge, if any, must "
                                         "come from the momentum rule, and is compared with unconditional momentum."))
        n_var = st.n_variants()
        if n_var > 1:
            out.append(Objection(code="multiplicity", severity="minor",
                                 message=f"{n_var} variants are selected in-sample; the Deflated Sharpe counts them "
                                         "with every earlier version of the family."))
        return out


# -- Research Coordinator ---------------------------------------------------------------------------
class ResearchCoordinator:
    name = COORDINATOR

    def __init__(self) -> None:
        self.researcher, self.analyst, self.reviewer = AlphaResearcher(), QuantAnalyst(), AdversarialReviewer()

    def run_cycle(self, session: Session, budget: Budget | None = None, *, actor: str = COORDINATOR,
                  costs: dict[str, CostModel] | None = None, run_experiments: bool = True,
                  checkpoint: Callable[[], None] | None = None) -> Experiment:
        """``checkpoint`` (e.g. ``session.commit``) is called after each stage, so a long cycle does not hold the
        database write lock from start to end; by default the session is only flushed."""
        budget = budget or Budget()
        checkpoint = checkpoint or session.flush
        t0 = time.perf_counter()
        ctx = build_context(session, budget, costs, actor=f"agent:{COORDINATOR}")
        config = {"version": VERSION, "budget": budget.model_dump(), "run_experiments": run_experiments,
                  "explicit_costs": {k: v.model_dump() for k, v in sorted((costs or {}).items())}}
        state = {"data": sorted((d.id, d.content_hash) for d in ctx.datasets),
                 "existing": sorted(ctx.existing), "relationship_scan": ctx.relationship_scan.id
                 if ctx.relationship_scan else None}
        run = Experiment(kind="alpha_research_run", status="running", algorithm_version=f"alpha_cycle@{VERSION}",
                         code_version=code_version(), config=config,
                         config_hash=stable_hash({"config": config, "state": state}), seed=0,
                         data_refs=[{"dataset_id": i, "content_hash": h} for i, h in state["data"]])
        session.add(run)
        session.flush()
        checkpoint()
        seq = [0]

        def post(sender: str, receiver: str, kind: str, msg: BaseModel, key: str, hyp_id: str | None = None,
                 evidence: list[Evidence] | None = None) -> None:
            seq[0] += 1
            session.add(AgentMessage(run_id=run.id, seq=seq[0], sender=sender, receiver=receiver, kind=kind,
                                     subject_key=key[:64], hypothesis_id=hyp_id, payload=msg.model_dump(mode="json"),
                                     evidence=[e.model_dump(mode="json") for e in (evidence or [])]))

        ideas, gaps = self.researcher.propose(ctx)
        for g in gaps:
            post(RESEARCHER, COORDINATOR, "data_gap", g, g.key)
        decisions: list[Decision] = []

        def decide(d: Decision, hyp_id: str | None = None) -> None:
            decisions.append(d)
            post(COORDINATOR, "log", "decision", d, d.key, hyp_id)

        registered: list[tuple[Idea, Hypothesis]] = []
        keys_this_cycle: set[str] = set()
        for idea in ideas:
            post(RESEARCHER, ANALYST, "idea", idea, idea.key, evidence=idea.evidence)
            try:
                form = self.analyst.formalize(ctx, idea)
            except ValueError as exc:  # contract rejected by its own validation
                form = DataGap(key=idea.key, route=idea.route, family=idea.family, reason=f"invalid contract: {exc}",
                               needs=["A contract that passes validation"])
            if isinstance(form, DataGap):
                post(ANALYST, COORDINATOR, "data_gap", form, idea.key)
                decide(Decision(key=idea.key, action="not_testable", reason=form.reason))
                continue
            post(ANALYST, REVIEWER, "formalization", form, idea.key)
            objections = self.reviewer.review(ctx, idea, form, keys_this_cycle)
            for o in objections:
                post(REVIEWER, COORDINATOR, "objection", o, idea.key, evidence=o.evidence)
            blocks = [o for o in objections if o.severity == "block"]
            if any(o.code == "duplicate" for o in blocks):
                ex = ctx.existing.get(idea.key, [])
                decide(Decision(key=idea.key, action="duplicate", reason=blocks[0].message,
                                hypothesis_id=ex[0]["id"] if ex else None), ex[0]["id"] if ex else None)
                continue
            if blocks:
                decide(Decision(key=idea.key, action="rejected_by_review",
                                reason="; ".join(f"{o.code}: {o.message}" for o in blocks)))
                continue
            if len(registered) >= budget.max_hypotheses:
                decide(Decision(key=idea.key, action="deferred_budget",
                                reason=f"hypothesis budget of {budget.max_hypotheses} reached"))
                continue
            contract = HypothesisContract.model_validate(form.contract)
            major = [o for o in objections if o.severity == "major"]
            if major:  # the contract records the reviewer's major objections as limitations
                contract = contract.model_copy(update={"limitations": contract.limitations + [
                    f"{o.code}: {o.message}" for o in major if o.code != "origin_used_holdout_period"]})
            hyp = create_hypothesis(session, contract, actor=f"agent:{ANALYST}")
            session.flush()
            for m in session.scalars(select(AgentMessage).where(AgentMessage.run_id == run.id,
                                                                AgentMessage.subject_key == idea.key[:64])).all():
                m.hypothesis_id = hyp.id
            keys_this_cycle.add(idea.key)
            registered.append((idea, hyp))
            decide(Decision(key=idea.key, action="registered", hypothesis_id=hyp.id,
                            reason=f"{len(major)} major and {len(objections) - len(major)} minor objection(s) "
                                   "recorded; no blocking objection"), hyp.id)

        checkpoint()
        n_exp = variants = 0
        if run_experiments:
            for idea, hyp in registered:  # already in priority order
                n_var = HypothesisContract.model_validate(hyp.contract).strategy.n_variants()
                elapsed = time.perf_counter() - t0
                why = None
                if n_exp >= budget.max_experiments:
                    why = f"experiment budget of {budget.max_experiments} reached"
                elif variants + n_var > budget.max_variants:
                    why = f"variant budget of {budget.max_variants} would be exceeded"
                elif elapsed >= budget.max_seconds:
                    why = f"time budget of {budget.max_seconds:.0f}s reached"
                if why:
                    decide(Decision(key=idea.key, action="experiment_skipped", hypothesis_id=hyp.id, reason=why),
                           hyp.id)
                    continue
                exp = run_validation(session, hyp, actor=f"agent:{COORDINATOR}")
                session.flush()
                n_exp += 1
                variants += n_var
                res = exp.results or {}
                m = (res.get("oos") or {}).get("metrics", {})
                ev = Evidence(kind="experiment", ref=exp.id, summary=f"validation {exp.status}: {hyp.status}",
                              values={"decision": res.get("decision"), "sharpe_net_oos": m.get("sharpe_net"),
                                      "mean_net_oos": m.get("mean_net"), "n_trades_oos": m.get("n_trades"),
                                      "max_drawdown_oos": m.get("max_drawdown"),
                                      "dsr": (res.get("overfitting") or {}).get("dsr"),
                                      "blocked_gates": [g["gate"] for g in res.get("gates", []) if not g["passed"]],
                                      "error": exp.error})
                post(COORDINATOR, "log", "experiment_result",
                     Decision(key=idea.key, action="experiment_run" if exp.status == "succeeded" else
                              "experiment_failed", hypothesis_id=hyp.id, experiment_id=exp.id,
                              reason=f"{hyp.status} (decided by the policy engine)"), idea.key, hyp.id, [ev])
                decisions.append(Decision(key=idea.key, action="experiment_run" if exp.status == "succeeded"
                                          else "experiment_failed", hypothesis_id=hyp.id, experiment_id=exp.id,
                                          reason=hyp.status))
                checkpoint()

        count = lambda a: sum(d.action == a for d in decisions)
        run.n_tests = variants
        run.results = {
            "n_ideas": len(ideas), "n_data_gaps": len(gaps) + count("not_testable"),
            "n_registered": count("registered"), "n_duplicates": count("duplicate"),
            "n_rejected_by_review": count("rejected_by_review"), "n_deferred": count("deferred_budget"),
            "n_experiments": n_exp, "n_variants_backtested": variants,
            "n_validation_passed": sum(h.status == "VALIDATION_PASSED" for _, h in registered),
            "n_validation_failed": sum(h.status == "VALIDATION_FAILED" for _, h in registered),
            "relationship_scan_id": ctx.relationship_scan.id if ctx.relationship_scan else None,
            "llm_tokens": 0, "duration_s": round(time.perf_counter() - t0, 3),
            "note": "Research priority is a heuristic ordering. Predictive evidence comes only from the validation "
                    "engine; validated net profitability also needs the holdout test, explicit costs and approval.",
        }
        run.status = "succeeded"
        run.finished_at = datetime.now(UTC)
        audit.record(session, actor, "alpha_cycle_finished", "experiment", run.id, **{
            k: v for k, v in run.results.items() if k.startswith("n_")})
        return run
