"""Robust selection: separate scores per dimension, mandatory conditions, explicit research decisions.

Decisions (version ``SELECTION_VERSION``), in this order:

1. ``REJECTED`` - a critical integrity check failed (the experiment is INVALID) or, with an adequate sample, the
   out-of-sample net return is not positive: the hypothesis is refuted under the evaluated assumptions.
2. ``INCONCLUSIVE`` - too few out-of-sample bars or independent trades, or a positive estimate that does not clear
   the evidence conditions (baselines beaten with a CI above zero, lab-wide Benjamini-Hochberg p < 0.05, DSR at
   the contract's minimum).
3. ``NEEDS_RESEARCH`` - the evidence conditions hold but a robustness condition fails (costs x2, one-bar delay,
   parameter neighbourhood, concentration, PBO): the edge may be fragile.
4. ``OOS_SUPPORTED`` - evidence and robustness conditions hold. Out-of-sample support under the evaluated
   assumptions; not proof of future profit.
5. ``PAPER_ELIGIBLE`` - additionally: the isolated final period was evaluated once and meets the contract,
   explicit cost assumptions, all data quality ``pass`` and policy gates A-D passed. It is a label: the lab never
   changes a hypothesis status or a paper session; a person promotes through the existing approval.

Mandatory conditions are never compensated by a good score. The score orders candidates inside a decision and is
shown with every component; a high rank is not a guarantee of future returns.
"""

from __future__ import annotations

import math
from typing import Any

from qal.lab.pipeline import MIN_INDEPENDENT_TRADES, MIN_OOS_BARS

SELECTION_VERSION = "lab-selection@1.0.0"
DECISIONS = ("REJECTED", "INCONCLUSIVE", "NEEDS_RESEARCH", "OOS_SUPPORTED", "PAPER_ELIGIBLE")
TIER = {d: i for i, d in enumerate(DECISIONS)}
WEIGHTS = {"methodology": 0.15, "oos_evidence": 0.20, "net_performance": 0.15, "parameter_robustness": 0.15,
           "stability": 0.15, "uncertainty": 0.10, "capacity": 0.05, "complexity": 0.05}
ALPHA = 0.05
NOT_A_GUARANTEE = ("A decision and a rank describe the evidence under the evaluated data, costs and design. They are "
                   "not a forecast or a guarantee of future returns, and they approve nothing.")


def _cond(name: str, ok: bool | None, detail: str) -> dict[str, Any]:
    return {"condition": name, "met": bool(ok) if ok is not None else False, "evaluable": ok is not None,
            "detail": detail}


def decide(res: dict[str, Any], contract: dict[str, Any], *, adjusted_p: float | None) -> dict[str, Any]:
    """``res``: stored pipeline output. ``adjusted_p``: lab-wide BH-adjusted one-sided p of the OOS mean."""
    succ = contract.get("success", {})
    if res.get("invalid"):
        return {"decision": "REJECTED", "version": SELECTION_VERSION, "score": 0.0, "components": [],
                "reasons": [f"critical integrity check failed: {', '.join(res['invalid'])}"], "conditions": [],
                "warnings": [], "note": NOT_A_GUARANTEE}
    st, rob, base = res["E_statistics"], res["D_robustness"], res["B_baselines"]
    oos = st["oos"]
    bars, trades = oos.get("bars") or 0, oos.get("n_trades") or 0
    min_trades = max(MIN_INDEPENDENT_TRADES, int(succ.get("min_trades", 30)))
    warnings = _warnings(res, bars, trades, min_trades)

    sample = [_cond("oos_bars", bars >= MIN_OOS_BARS, f"{bars} out-of-sample bars (minimum {MIN_OOS_BARS})"),
              _cond("independent_trades", trades >= min_trades, f"{trades} trades (minimum {min_trades})")]
    mean = oos.get("mean_net")
    ci = st.get("mean_net_ci95")
    beaten = [b for b in base if b.get("beaten")]
    evidence = [
        _cond("positive_net_oos", None if mean is None else mean > 0 and ci is not None and ci[0] > 0,
              f"mean net {mean} per bar, 95% CI {ci}"),
        _cond("beats_all_baselines", len(beaten) == len(base) and bool(base),
              f"beaten: {[b['baseline'] for b in beaten]} of {[b['baseline'] for b in base]}"),
        _cond("lab_multiple_testing", None if adjusted_p is None else adjusted_p < ALPHA,
              f"lab-wide Benjamini-Hochberg p = {adjusted_p}"),
        _cond("deflated_sharpe", None if st.get("dsr") is None else st["dsr"] >= succ.get("dsr_min", 0.9),
              f"DSR {st.get('dsr')} with {st.get('n_trials')} trials (minimum {succ.get('dsr_min', 0.9)})"),
    ]
    costs = {round(c["multiplier"], 2): c for c in rob["costs"]}
    c2 = costs.get(round(float(succ.get("cost_multiplier_survive", 2.0)), 2))
    d1 = next((d for d in rob["delays"] if d["delay_bars"] == 1), None)
    top5 = oos.get("top5_trades_share")
    robust = [
        _cond("survives_cost_multiple", None if c2 is None else (c2.get("mean_net") or 0) > 0,
              f"mean net at x{succ.get('cost_multiplier_survive', 2.0)} costs: {None if c2 is None else c2.get('mean_net')}"),
        _cond("survives_one_bar_delay", None if d1 is None else (d1.get("mean_net") or 0) > 0,
              f"mean net with one extra bar of delay: {None if d1 is None else d1.get('mean_net')}"),
        _cond("parameter_neighbourhood", rob["parameters"]["positive_frac"] >= succ.get("min_robust_param_frac", 0.5),
              f"{rob['parameters']['positive_frac']:.0%} of grid variants positive in-sample"),
        _cond("not_concentrated", True if top5 is None else top5 <= succ.get("max_top5_trades_share", 0.6),
              f"best 5 trades = {top5} of net P&L"),
        _cond("pbo", True if st.get("pbo") is None else st["pbo"] <= succ.get("max_pbo", 0.5), f"PBO {st.get('pbo')}"),
    ]
    hold = res["C_temporal"].get("holdout")
    gates = res.get("policy_gates_A_D") or []
    integrity = res["A_integrity"]["checks"]
    paper = [
        _cond("holdout_once_and_met", None if hold is None else bool(
            hold.get("independent") and (hold["metrics"].get(succ.get("metric", "sharpe_net")) or -math.inf)
            >= succ.get("min_value", 0.5) and abs(hold["metrics"].get("max_drawdown") or 1) <= succ.get("max_drawdown", 0.35)),
            "final period not evaluated" if hold is None else
            f"access {hold.get('access_number')}, {succ.get('metric', 'sharpe_net')} "
            f"{hold['metrics'].get(succ.get('metric', 'sharpe_net'))}"),
        _cond("explicit_costs", next((c["passed"] for c in integrity if c["check"] == "explicit_costs"), False),
              "cost assumptions set for every dataset"),
        _cond("data_quality_pass", all(q == "pass" for q in (next((c["detail"] for c in integrity
                                                                     if c["check"] == "data_quality"), {}) or {}).values()),
              "every dataset with quality pass"),
        _cond("policy_gates_A_D", bool(gates) and all(g["passed"] for g in gates),
              "failed: " + ",".join(g["gate"] for g in gates if not g["passed"]) if gates else "no gates"),
    ]

    reasons: list[str] = []
    if not all(c["met"] for c in sample):
        decision = "INCONCLUSIVE"
        reasons.append("insufficient sample: " + "; ".join(c["detail"] for c in sample if not c["met"]))
    elif mean is not None and mean <= 0:
        decision = "REJECTED"
        reasons.append(f"out-of-sample mean net return {mean:.6f} <= 0 with an adequate sample: refuted under the "
                       "evaluated assumptions")
    elif not all(c["met"] for c in evidence):
        decision = "INCONCLUSIVE"
        reasons += [f"not met: {c['condition']} ({c['detail']})" for c in evidence if not c["met"]]
    elif not all(c["met"] for c in robust):
        decision = "NEEDS_RESEARCH"
        reasons += [f"fragile: {c['condition']} ({c['detail']})" for c in robust if not c["met"]]
    elif not all(c["met"] for c in paper):
        decision = "OOS_SUPPORTED"
        reasons.append("out-of-sample evidence and robustness conditions met")
        reasons += [f"not yet paper eligible: {c['condition']} ({c['detail']})" for c in paper if not c["met"]]
    else:
        decision = "PAPER_ELIGIBLE"
        reasons.append("all conditions met; a person still has to approve paper trading")
    comps = _components(res, adjusted_p, contract)
    score = round(sum(WEIGHTS[c["name"]] * c["value"] for c in comps), 4)
    return {"decision": decision, "version": SELECTION_VERSION, "score": score, "components": comps,
            "weights": WEIGHTS, "reasons": reasons, "warnings": warnings,
            "conditions": {"sample": sample, "evidence": evidence, "robustness": robust, "paper": paper},
            "breaks_under": rob.get("breaks_under", []), "note": NOT_A_GUARANTEE}


def _warnings(res: dict[str, Any], bars: int, trades: int, min_trades: int) -> list[str]:
    w = []
    if bars < MIN_OOS_BARS or trades < min_trades:
        w.append(f"Small sample: {bars} out-of-sample bars and {trades} trades.")
    hold = res["C_temporal"].get("holdout")
    if hold and not hold.get("independent"):
        w.append(f"Final test reused: access number {hold.get('access_number')}; it is not an independent test.")
    for c in res["A_integrity"]["checks"]:
        if c["check"] == "explicit_costs" and not c["passed"]:
            w.append("Cost data missing: asset-class default costs (estimates) were used for "
                     f"{', '.join(c['detail'].get('symbols_on_default_costs') or c['detail']['datasets_on_default_costs'])}.")
        if c["check"] == "universe_survivorship" and ("survivorship" in str(c["detail"]) or not c["passed"]):
            w.append("Survivorship bias cannot be ruled out for this universe.")
    return w


def _components(res: dict[str, Any], adjusted_p: float | None, contract: dict[str, Any]) -> list[dict[str, Any]]:
    st, rob = res["E_statistics"], res["D_robustness"]
    checks = res["A_integrity"]["checks"]
    gates = res.get("policy_gates_A_D") or []
    meth = (sum(c["passed"] for c in checks) + sum(g["passed"] for g in gates)) / max(1, len(checks) + len(gates))
    ev = 0.0 if adjusted_p is None else min(1.0, max(0.0, -math.log10(max(adjusted_p, 1e-12)) / 2))
    sr = st["oos"].get("sharpe_net") or 0.0
    perf = sr / (sr + 1) if sr > 0 else 0.0
    wins = [w for w in rob["windows"] if w.get("mean_net") is not None]
    years = [y for y in rob["years"] if y.get("mean_net") is not None]
    stab = 0.5 * (sum(w["mean_net"] > 0 for w in wins) / len(wins) if wins else 0) + \
        0.5 * (sum(y["mean_net"] > 0 for y in years) / len(years) if years else 0)
    lo, hi = (st.get("sharpe_ci95") or [None, None])
    unc = 0.0 if lo is None or hi is None else max(0.0, min(1.0, 0.5 + lo / max(hi - lo, 1e-9)))
    n_var = max(1, len(rob["parameters"]["grid"]))
    breaches = (res.get("capacity_breach_frac") or 0.0)
    return [
        {"name": "methodology", "value": round(meth, 4), "source": "share of integrity checks and gates A-D passed"},
        {"name": "oos_evidence", "value": round(ev, 4), "source": f"lab-wide BH p = {adjusted_p}"},
        {"name": "net_performance", "value": round(perf, 4), "source": f"OOS net Sharpe {sr:.3f} -> s/(s+1)"},
        {"name": "parameter_robustness", "value": round(rob["parameters"]["positive_frac"], 4),
         "source": "share of grid variants with positive in-sample net mean"},
        {"name": "stability", "value": round(stab, 4), "source": "positive walk-forward windows and calendar years"},
        {"name": "uncertainty", "value": round(unc, 4), "source": f"bootstrap Sharpe CI {lo}..{hi}"},
        {"name": "capacity", "value": round(max(0.0, 1 - 10 * breaches), 4),
         "source": f"capacity breaches {breaches:.1%} of bars"},
        {"name": "complexity", "value": round(1 / (1 + math.log2(n_var)), 4),
         "source": f"{n_var} variant(s) explored; more variants, more selection risk"},
    ]


def rank_key(d: dict[str, Any]) -> tuple[int, float]:
    return (-TIER.get(d.get("decision", "REJECTED"), 0), -float(d.get("score") or 0.0))


__all__ = ["DECISIONS", "NOT_A_GUARANTEE", "SELECTION_VERSION", "WEIGHTS", "decide", "rank_key"]
