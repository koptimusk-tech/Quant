"""Portfolio runs: eligibility -> construction methods -> risk manager -> backtest -> risk report -> stress.

Each run is stored as an Experiment of kind ``portfolio_backtest`` (failed and empty runs included) and every
rule the risk manager checked is stored as a RiskDecision of that run.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
from pydantic import Field
from sqlalchemy.orm import Session

from qal import audit
from qal.config import get_settings
from qal.models import Experiment, RiskPolicy
from qal.portfolio import backtest as bt
from qal.portfolio import construction, stress
from qal.portfolio.risk import (
    correlations,
    covariance,
    diversification,
    drawdown_stats,
    exposure_overlap,
    exposures,
    factor_exposure,
    marginal_analysis,
    risk_contributions,
)
from qal.portfolio.risk_manager import (
    ENGINE_VERSION,
    PolicyLimits,
    default_policy,
    policy_label,
    store_decisions,
)
from qal.portfolio.strategies import (
    SYN_NOTE,
    EligibilityCriteria,
    StrategyRecord,
    StrategySeries,
    catalog,
    series_for,
    synthetic_strategies,
)
from qal.versioning import code_version, stable_hash

COST_MULTIPLIERS = (0.0, 1.0, 2.0, 3.0)
OBSERVED = "historical (observed in the backtest)"
ESTIMATE = "estimate (from trailing data; not a forecast)"


class PortfolioRunIn(bt.PortfolioConfig):
    criteria: EligibilityCriteria = Field(default_factory=EligibilityCriteria)
    policy_id: str | None = None
    label: str = ""
    mc_paths: int = Field(2000, ge=100, le=20000)
    stress_backtests: bool = True
    synthetic_seed: int = 7


class RunError(ValueError):
    pass


def _downsample(df: pd.DataFrame, cols: list[str], n: int = 600) -> list[dict[str, Any]]:
    step = max(1, len(df) // n)
    return [{"ts": str(df["ts"].iloc[i].date()), **{c: float(df[c].iloc[i]) for c in cols}}
            for i in range(0, len(df), step)]


def _curve(bars: pd.DataFrame) -> pd.DataFrame:
    eq = (1 + bars["net"]).cumprod()
    return pd.DataFrame({"ts": bars["ts"], "equity": eq, "drawdown": eq / eq.cummax() - 1,
                         "leverage": bars["leverage"], "gross_exposure": bars["gross_exposure"]})


def load(session: Session, cfg: PortfolioRunIn) -> tuple[list[StrategyRecord], dict[str, StrategyRecord],
                                                          dict[str, StrategySeries], list[dict[str, Any]]]:
    """Catalog, the eligible requested strategies, their series, and the requested ones that were rejected."""
    wants_syn = any(s.startswith("SYN:") for s in cfg.strategy_ids)
    recs = catalog(session, cfg.criteria, include_synthetic=wants_syn, synthetic_seed=cfg.synthetic_seed)
    by_id = {r.id: r for r in recs}
    chosen, rejected = {}, []
    for sid in cfg.strategy_ids:
        r = by_id.get(sid)
        if r is None:
            rejected.append({"id": sid, "reasons": ["unknown strategy (never validated, or wrong id)"]})
        elif not r.eligible:
            rejected.append({"id": sid, "name": r.name, "reasons": r.reasons})
        else:
            chosen[sid] = r
    series: dict[str, StrategySeries] = {}
    if wants_syn:
        _, syn = synthetic_strategies(seed=cfg.synthetic_seed)
        series.update({k: v for k, v in syn.items() if k in chosen})
    data_dir = get_settings().data_dir
    for sid, r in chosen.items():
        if r.source == "hypothesis":
            series[sid] = series_for(session, r, data_dir)
    return recs, chosen, series, rejected


def run_portfolio(session: Session, cfg: PortfolioRunIn, *, actor: str = "api") -> Experiment:
    policy = session.get(RiskPolicy, cfg.policy_id) if cfg.policy_id else default_policy(session)
    if policy is None:
        raise RunError(f"unknown risk policy {cfg.policy_id}")
    limits = PolicyLimits.model_validate(policy.limits)
    version = policy_label(policy)
    conf = cfg.model_dump(mode="json")
    exp = Experiment(kind="portfolio_backtest", status="running",
                     algorithm_version=f"portfolio@1.0.0+{ENGINE_VERSION}+policy:{version}",
                     code_version=code_version(), config={**conf, "policy_id": policy.id, "policy_version": version},
                     config_hash=stable_hash({"cfg": conf, "policy": policy.policy_hash}), data_refs=[],
                     seed=cfg.seed, n_tests=len(cfg.methods))
    session.add(exp)
    session.flush()
    t0 = time.perf_counter()
    try:
        _, chosen, series, rejected = load(session, cfg)
        exp.data_refs = [{"strategy_id": s, "name": r.name, "is_synthetic": r.is_synthetic,
                          "instruments": [{"symbol": i["symbol"], "content_hash": i.get("content_hash")}
                                          for i in r.instruments]} for s, r in chosen.items()]
        if not chosen:
            raise RunError("no eligible strategies among the requested ones")
        exp.results = _compute(chosen, series, cfg, limits, version, rejected)
        n = store_decisions(session, exp.id, policy, exp.results.pop("_decisions"))
        exp.results["decisions_stored"] = n
        exp.status = "succeeded"
    except Exception as exc:
        exp.status = "failed"
        exp.error = f"{type(exc).__name__}: {exc}"
        exp.results = {**(exp.results or {}), "rejected": locals().get("rejected", [])}
    exp.results = {**(exp.results or {}), "duration_s": round(time.perf_counter() - t0, 3)}
    exp.finished_at = datetime.now(UTC)
    audit.record(session, actor, "portfolio_run_finished", "experiment", exp.id, status=exp.status,
                 policy=version, error=exp.error)
    return exp


def _compute(chosen: dict[str, StrategyRecord], series: dict[str, StrategySeries], cfg: PortfolioRunIn,
             limits: PolicyLimits, version: str, rejected: list[dict[str, Any]]) -> dict[str, Any]:
    ids = list(chosen)
    panel = bt.make_panel(series, chosen, ids)
    is_syn = any(r.is_synthetic for r in chosen.values())
    methods: dict[str, Any] = {}
    runs: dict[str, dict[str, Any]] = {}
    for m in cfg.methods:
        out = bt.run(panel, chosen, cfg, m, limits, version)
        runs[m] = out
        bars = out["bars"]
        methods[m] = {"description": construction.describe(m), "metrics": bt.metrics(bars),
                      "stability": bt.weight_stability(out["weights"]), "segments": bt.segments(bars, cfg.oos_fraction),
                      "blocked": _blocked_counts(out["decisions"]), "curve": _downsample(_curve(bars), ["equity", "drawdown"])}
    primary = cfg.methods[0]
    main = runs[primary]
    bars = main["bars"]
    # Benchmarks over the same calendar: naive equal weight (no vol target, no risk manager) and each strategy.
    naive = bt.run(panel, chosen, cfg, "equal_weight", limits, version, use_hrm=False, vol_target=False)
    bench = {"naive_equal_weight": bt.metrics(naive["bars"]), "cash": {"total_return_net": 0.0, "sharpe_net": 0.0},
             "strategies": {s: bt.metrics(pd.DataFrame({"ts": panel.dates, "gross": panel.gross[s].fillna(0).to_numpy(),
                                                        "net": panel.net[s].fillna(0).to_numpy(),
                                                        "cost": (panel.gross[s] - panel.net[s]).fillna(0).to_numpy(),
                                                        "gross_exposure": 1.0, "turnover": 0.0, "leverage": 1.0,
                                                        "rebalance_cost": 0.0}))
                            for s in ids}}
    # Cost sensitivity (strategy trading costs and rebalancing costs scaled together).
    cost_sens = []
    for k in COST_MULTIPLIERS:
        o = runs[primary] if k == cfg.cost_multiplier else bt.run(
            panel, chosen, cfg.model_copy(update={"cost_multiplier": k}), primary, limits, version)
        mm = bt.metrics(o["bars"])
        cost_sens.append({"multiplier": k, "sharpe_net": mm.get("sharpe_net"), "total_return_net": mm.get("total_return_net"),
                          "cagr_net": mm.get("cagr_net")})
    # Risk report at the last decision (weights the engine would hold next), from trailing data only.
    last = main["weights"][-1] if main["weights"] else None
    target = {s: float((last or {}).get("final", {}).get(s, 0.0)) for s in ids}
    w = np.array([target[s] for s in ids])
    window = panel.net.iloc[-cfg.lookback_days:]
    act = [s for s in ids if window[s].notna().sum() >= 20]
    C = covariance(window[act], cfg.cov_method).reindex(index=ids, columns=ids).fillna(0.0)
    rc = risk_contributions(w, C)
    last_expo = {s: {k: float(v) for k, v in panel.expo[s].iloc[-1].dropna().items()} for s in ids}
    ex = exposures(target, last_expo, panel.asset_class, {s: chosen[s].group for s in ids})
    port_hist = bars.set_index("ts")["net"]
    hypo = stress.hypothetical(target, panel.net, last_expo, panel.asset_class, panel.cost_bps)
    if cfg.stress_backtests:
        hypo.extend(_backtest_scenarios(panel, chosen, cfg, primary, limits, version, bars))
    liquidity = []
    for sym, e in ex["instrument"].items():
        dv = panel.dollar_volume.get(sym)
        liquidity.append({"instrument": sym, "exposure": e, "median_dollar_volume": dv,
                          "capacity_capital": (panel.max_participation.get(sym, 0.05) * dv / abs(e)) if dv and e else None,
                          "note": None if dv else "no usable traded volume (FX tick volume or synthetic): not evaluable"})
    report = {
        "as_of": last["ts"] if last else None, "weights": target, "basis": ESTIMATE,
        "ex_ante_vol": rc["vol"], "risk_contributions": dict(zip(ids, rc["pct"], strict=True)),
        "marginal_risk": dict(zip(ids, rc["marginal"], strict=True)),
        "diversification": diversification(w, C) if w.sum() > 0 else {},
        "exposures": ex, "liquidity": liquidity,
        "marginal_analysis": marginal_analysis(window, C, w) if w.sum() > 0 and len(ids) > 1 else [],
    }
    hist_risk = {
        "basis": OBSERVED, "correlations": correlations(panel.net[ids]), "factors": factor_exposure(panel.net[ids]),
        "exposure_overlap": exposure_overlap({s: series[s].exposure for s in ids}, panel.asset_class),
        "drawdown": drawdown_stats(port_hist),
        "realised_vol": float(bars["net"].std(ddof=1) * np.sqrt(252)) if len(bars) > 2 else None,
    }
    stress_out = {"disclaimer": stress.DISCLAIMER, "historical": stress.historical(port_hist),
                  "hypothetical": hypo,
                  "monte_carlo": [stress.monte_carlo(target, panel.net, n_paths=cfg.mc_paths, seed=cfg.seed),
                                  stress.monte_carlo(target, panel.net, n_paths=cfg.mc_paths, seed=cfg.seed,
                                                     stressed=True)]}
    checks = [bt.lookahead_check(panel, chosen, cfg, primary, limits, version, main),
              {"check": "execution_lag", "passed": all(_lag_ok(main, cfg)), "executions": len(main["executed"]), "description":
               "Weights decided at a close are first held on a later day (decision + 1 + delay)"}]
    return {
        "is_synthetic": is_syn, "synthetic_note": SYN_NOTE if is_syn else None,
        "policy_version": version, "primary_method": primary,
        "strategies": [{"id": s, "name": chosen[s].name, "is_synthetic": chosen[s].is_synthetic,
                        "group": chosen[s].group, "counterparty": chosen[s].counterparty,
                        "history": chosen[s].history} for s in ids],
        "rejected": rejected,
        "period": {"start": str(panel.dates[0].date()), "end": str(panel.dates[-1].date()), "days": len(panel.dates)},
        "methods": methods,
        "primary": {"metrics": methods[primary]["metrics"], "weights_history": main["weights"][-120:],
                    "n_rebalances": len(main["weights"]),
                    "series": _downsample(_curve(bars), ["equity", "drawdown", "leverage", "gross_exposure"]),
                    "daily_blocks": main["daily_blocks"][-200:]},
        "benchmarks": bench, "cost_sensitivity": cost_sens,
        "risk": report, "historical_risk": hist_risk, "stress": stress_out, "leakage_checks": checks,
        "limits": limits.model_dump(),
        "notes": ["Historical figures describe the backtest; ex-ante figures are estimates from trailing data.",
                  "Correlations observed in calm periods are not assumed to hold in stress.",
                  "No orders are generated and no account is touched: research only."],
        "_decisions": main["decisions"],
    }


def _lag_ok(out: dict[str, Any], cfg: PortfolioRunIn) -> list[bool]:
    return [ex >= dec + 1 + cfg.execution_delay_days for dec, ex in out["executed"]] or [True]


def _blocked_counts(decisions: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in decisions:
        if d["decision"] == "blocked":
            out[d["rule"]] = out.get(d["rule"], 0) + 1
    return out


def _backtest_scenarios(panel: bt.Panel, chosen: dict[str, StrategyRecord], cfg: PortfolioRunIn, method: str,
                        limits: PolicyLimits, version: str, base: pd.DataFrame) -> list[dict[str, Any]]:
    """Hypothetical frictions replayed through the full backtest (same data, same decisions rules)."""
    base_ret = float((1 + base["net"]).prod() - 1)
    worst = base["net"].rolling(5).sum().idxmin() if len(base) > 10 else 0
    start = base["ts"].iloc[max(int(worst) - 4, 0)]
    end = base["ts"].iloc[min(int(worst) + 10, len(base) - 1)]
    scen = [
        ("data delayed 2 days", {"execution_delay_days": 2}, "Decisions reach the market two days late"),
        ("30% of rebalances fail", {"execution_failure_prob": 0.3}, "Simulated execution failures (seeded)"),
        ("costs x2 and +2 bps slippage", {"cost_multiplier": 2.0, "extra_slippage_bps": 2.0}, "Higher trading costs"),
        ("market interruption around the worst week", {"interruptions": [(str(start.date()), str(end.date()))]},
         "No trading for about two weeks: positions frozen through the worst observed week"),
    ]
    out = []
    for name, upd, desc in scen:
        o = bt.run(panel, chosen, cfg.model_copy(update=upd), method, limits, version)
        r = float((1 + o["bars"]["net"]).prod() - 1)
        m = bt.metrics(o["bars"])
        out.append({"name": name, "kind": "hypothetical", "basis": "historical replay with a friction",
                    "pnl": r - base_ret, "total_return_net": r, "max_drawdown": m.get("max_drawdown"),
                    "description": desc + " (change in total return vs. the base backtest)"})
    return out
