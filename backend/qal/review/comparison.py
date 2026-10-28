"""Comparison of paper results with the backtest, the out-of-sample research results and the baseline.

The same-bar comparison of Phase 12 (``program.compare``) is reused. Before any number is read, the compatibility
of the two sides is checked: strategy version, universe, timeframe, horizon, capital and position size, cost model,
execution assumptions, P&L method, period and regime. A side that is not compatible is reported as such and its
difference is not summarised.

A deviation is listed with the explanations that are possible and the data that would tell them apart; no cause is
asserted without evidence. The research out-of-sample results cover another period: they are shown for context
only, never tested against the paper results.
"""

from __future__ import annotations

from typing import Any

EXECUTION_DIFFERENCE = ("paper: orders filled at the next bar by the simulator (lots, participation cap, limits, "
                        "stops, risk gate, partial fills); backtest: close-to-close returns of the target weights "
                        "with the same cost model")


def compatibility(cmp: dict[str, Any] | None, cand: dict[str, Any], proto: dict[str, Any],
                  bars: int) -> list[dict[str, Any]]:
    conv = {c["check"]: c for c in (cmp or {}).get("conventions", [])}

    def c(name: str, key: str | None, ok: bool | None, detail: str) -> dict[str, Any]:
        base = conv.get(key) if key else None
        passed = base["passed"] if base is not None else ok
        return {"check": name, "compatible": passed, "detail": base["detail"] if base is not None else detail}

    return [
        c("strategy_version", "candidate_version", None, "same candidate version"),
        c("parameters", "strategy_and_parameters", None, "same parameters"),
        c("universe_and_assets", "universe", None, "same instruments"),
        c("timeframe", "timeframe", None, cand["timeframe"]),
        c("horizon", None, True, f"the backtest runs on exactly the {bars} bars the paper session processed"),
        c("capital_and_position_size", "allocation", None, "same allocation of the same capital"),
        c("cost_model", "costs", None, "the candidate's cost models"),
        c("cost_scenario", "cost_scenario", None, "base scenario"),
        {"check": "execution_assumptions", "compatible": True, "documented_difference": True,
         "detail": EXECUTION_DIFFERENCE + ": this difference is what the comparison measures"},
        {"check": "pnl_method", "compatible": True,
         "detail": "both net of the same costs, per bar, on the same capital; paper is marked to market each bar"},
        c("period_and_data", "data_snapshot", None, "same bars and data snapshot"),
        {"check": "regime", "compatible": True, "detail": "same bars, hence the same market regime on both sides"},
    ]


def deviations(cmp: dict[str, Any], proto: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    tol = proto["tracking_tolerance_bps"]
    ret = cmp.get("returns") or {}
    ci = ret.get("diff_ci95_bps")
    costs = cmp.get("costs") or {}
    fills = cmp.get("fills") or {}
    sig = cmp.get("signal_frequency") or {}
    if ci and (ci[0] > tol or ci[1] < -tol):
        out.append({
            "item": "net return per bar", "measured": f"paper - backtest {ci} bps (95% CI), tolerance ±{tol}",
            "possible_explanations": [
                f"costs: paper/backtest ratio {costs.get('ratio')}",
                (f"fills: fill rate {fills.get('fill_rate')}, partial {fills.get('partial')}, rejected "
                 f"{fills.get('rejected')}"),
                f"signals: agreement {sig.get('agreement')}",
                "lot rounding and participation caps of the simulator", "risk gate interventions"],
            "data_to_distinguish": "per-bar attribution of the gap to costs, unfilled quantity and signal differences "
                                   "(the costs part is measured; see 'unreconciled' for the remainder)",
            "cause": "not asserted"})
    if costs.get("ratio") is not None and costs["ratio"] > proto["max_cost_ratio"]:
        out.append({"item": "costs", "measured": f"paper/backtest costs {costs['ratio']:.2f} (max "
                    f"{proto['max_cost_ratio']})", "possible_explanations": [
                        "more traded notional in paper (partial fills completed over several bars, re-orders)",
                        "range-dependent slippage on volatile bars", "holding costs of positions open longer"],
                    "data_to_distinguish": "traded notional per bar on both sides and the cost per fill by component",
                    "cause": "not asserted"})
    if sig.get("agreement") is not None and sig["agreement"] < proto["min_signal_agreement"]:
        out.append({"item": "signals", "measured": f"agreement {sig['agreement']:.2f} (min "
                    f"{proto['min_signal_agreement']})", "possible_explanations": [
                        "history window shorter in paper than in the backtest",
                        "data revisions between the snapshot and the bars seen live", "missing or skipped bars"],
                    "data_to_distinguish": "the bars where decisions differ, with the data known at each decision",
                    "cause": "not asserted"})
    dd = cmp.get("drawdown") or {}
    if dd.get("ratio") is not None and dd["ratio"] > proto["max_drawdown_ratio"]:
        out.append({"item": "drawdown", "measured": f"paper/backtest drawdown {dd['ratio']:.2f} (max "
                    f"{proto['max_drawdown_ratio']})", "possible_explanations": [
                        "execution delay on fast moves", "exposure differences", "costs"],
                    "data_to_distinguish": "the drawdown episode bar by bar on both sides", "cause": "not asserted"})
    for u in cmp.get("unreconciled") or []:
        out.append({"item": f"unreconciled: {u['item']}", "measured": {k: v for k, v in u.items() if k != "cause"},
                    "possible_explanations": ["see the items above"], "data_to_distinguish": "as above",
                    "cause": u["cause"]})
    return out


def build(cmp: dict[str, Any] | None, trk: dict[str, Any], unc: dict[str, Any], cand: dict[str, Any],
          proto: dict[str, Any]) -> dict[str, Any]:
    bars = trk.get("bars", 0)
    compat = compatibility(cmp, cand, proto, bars)
    comparable = bool(cmp) and all(c["compatible"] for c in compat)
    oos = (cand.get("results") or {}).get("oos") or {}
    out: dict[str, Any] = {
        "compatibility": compat, "comparable": comparable,
        "backtest": None, "deviations": [],
        "baseline": {"baseline": proto["baseline"], "paper_mean": (trk.get("paper") or {}).get("mean"),
                     "baseline_mean": (trk.get("baseline_stats") or {}).get("mean"),
                     "excess_mean": (trk.get("excess_vs_baseline") or {}).get("mean"),
                     "excess_ci95": unc.get("excess_ci95"), "test": unc.get("excess_test")},
        "research_oos_context": {"period": "the research walk-forward, not the paper period", "bars": oos.get("bars"),
                                 "mean_net_per_bar": oos.get("mean_net"), "sharpe_net": oos.get("sharpe_net"),
                                 "n_trades": oos.get("n_trades"), "max_drawdown": oos.get("max_drawdown"),
                                 "note": "different period and conditions: context only, no test is made against it"},
    }
    if not comparable:
        out["note"] = ("Not comparable: " + ", ".join(c["check"] for c in compat if not c["compatible"]) +
                       ". Numerical differences with the backtest are not summarised.") if cmp else \
            "No bars processed: nothing to compare."
        return out
    out["backtest"] = {k: cmp.get(k) for k in ("returns", "volatility", "drawdown", "signal_frequency", "per_trade",
                                               "costs", "exposure", "per_asset", "per_asset_units", "regimes",
                                               "regime_rule", "fills", "unreconciled", "period")}
    out["deviations"] = deviations(cmp, proto)
    out["note"] = "Differences are attributed only to measured components; a comparison is not an approval."
    return out
