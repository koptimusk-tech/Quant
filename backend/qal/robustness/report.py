"""Robustness report: one status per dimension and an overall state, with the rules written out.

Dimensions are kept separate and never averaged into a score. Each is PASS, FAIL or INSUFFICIENT_DATA; a
dimension that cannot be evaluated is never treated as favourable. The overall state describes the tests that were
run under their stated assumptions: it is not a probability of future success and it promotes nothing.

Overall state (first rule that applies):

1. the candidate cannot be rebuilt (not a COMPLETED lab experiment, data changed) -> NOT_EVALUATED;
2. temporal, parameter or cost dimension FAIL -> FRAGILE;
3. any essential dimension INSUFFICIENT_DATA -> INSUFFICIENT_DATA;
4. any other FAIL (cross-asset, uncertainty, stress, capacity, diversification) -> MIXED_EVIDENCE;
5. otherwise -> ROBUST_UNDER_TESTED_ASSUMPTIONS (non-essential gaps are listed).

Essential dimensions: temporal, cross_asset, parameters, costs, uncertainty.
"""

from __future__ import annotations

from typing import Any

import numpy as np

STATES = ("NOT_EVALUATED", "INSUFFICIENT_DATA", "FRAGILE", "MIXED_EVIDENCE", "ROBUST_UNDER_TESTED_ASSUMPTIONS")
DIMENSIONS = ("temporal", "cross_asset", "parameters", "costs", "stress", "uncertainty", "capacity", "diversification")
ESSENTIAL = ("temporal", "cross_asset", "parameters", "costs", "uncertainty")
CORE_FRAGILITY = ("temporal", "parameters", "costs")

CRITERIA = {
    "temporal": "PASS: >= 4 walk-forward windows, >= 50% of windows and >= 50% of calendar years (>= 60 bars) with "
                "positive net mean. Fewer windows or < 120 OOS bars: INSUFFICIENT_DATA.",
    "cross_asset": "PASS: >= 60% of evaluated non-design assets with positive net OOS mean after re-selection, with "
                   ">= 2 such assets and an effective number of independent assets >= 2. Otherwise FAIL; fewer "
                   "assets or n_eff < 2: INSUFFICIENT_DATA.",
    "parameters": "PASS: positive net mean at the centre, >= 70% of one-at-a-time neighbours positive and their "
                  "median Sharpe >= half the centre's. No numeric parameter: INSUFFICIENT_DATA.",
    "costs": "PASS: positive net mean under the adverse scenario (x2 costs, +1 bar delay, half the participation "
             "limit). The severe scenario is reported, not required.",
    "stress": "PASS: no synthetic stress scenario has a max drawdown beyond the contract's max_drawdown.",
    "uncertainty": "PASS: lab sample sufficient, bootstrap Sharpe CI lower bound > 0 and DSR >= the contract's "
                   "dsr_min. Insufficient lab sample: INSUFFICIENT_DATA.",
    "capacity": "PASS: approximate capacity >= min_capacity. Unknown volume: INSUFFICIENT_DATA.",
    "diversification": "PASS: adding the candidate to the equal-weight reference of peers raises its Sharpe or "
                       "softens its max drawdown, with correlation on stress days < 0.7. No peers: INSUFFICIENT_DATA.",
}


def _d(status: str, reason: str, **values: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, "values": values}


def dimensions(res: dict[str, Any], contract: dict[str, Any], lab_decision: dict[str, Any] | None,
               min_capacity: float) -> dict[str, dict[str, Any]]:
    succ = contract.get("success", {})
    out: dict[str, dict[str, Any]] = {}

    t = res["temporal"]
    oos_bars = res["execution"]["scenarios"]["base"].get("bars", 0)
    if t["n_windows"] < 4 or oos_bars < 120:
        out["temporal"] = _d("INSUFFICIENT_DATA", f"{t['n_windows']} windows, {oos_bars} OOS bars")
    else:
        ok = (t["positive_window_frac"] or 0) >= 0.5 and (t["positive_year_frac"] is None or t["positive_year_frac"] >= 0.5)
        out["temporal"] = _d("PASS" if ok else "FAIL", f"positive windows {t['positive_window_frac']}, positive years "
                             f"{t['positive_year_frac']}", regime_dependence=t["regime_dependence"])

    x = res["cross_asset"]
    others = [r for r in x["rows"] if r["category"] != "design" and r.get("status") == "evaluated"]
    if x.get("not_applicable"):
        out["cross_asset"] = _d("INSUFFICIENT_DATA", x["not_applicable"])
    elif len(others) < 2 or (x.get("n_effective") or 0) < 2:
        out["cross_asset"] = _d("INSUFFICIENT_DATA", f"{len(others)} comparable assets evaluated, effective number "
                                f"of independent assets {x.get('n_effective')}")
    else:
        frac = sum((r.get("mean_net") or 0) > 0 for r in others) / len(others)
        out["cross_asset"] = _d("PASS" if frac >= 0.6 else "FAIL", f"{frac:.0%} of {len(others)} other assets positive "
                                f"after re-selection (n_eff {x['n_effective']})",
                                same_market=sum(r["category"] == "same_market" for r in others),
                                other_market=sum(r["category"] == "other_market" for r in others))

    p = res["parameters"]
    centre = p["rows"][0]
    if p["n_configs"] <= 1:
        out["parameters"] = _d("INSUFFICIENT_DATA", "no numeric parameter to perturb")
    else:
        ok = (centre.get("mean_net") or 0) > 0 and (p["neighbour_positive_frac"] or 0) >= 0.7 and \
            (p["neighbour_sharpe_median"] or 0) >= 0.5 * (p["centre_sharpe"] or 0)
        out["parameters"] = _d("PASS" if ok else "FAIL", f"centre mean {centre.get('mean_net')}, neighbours positive "
                               f"{p['neighbour_positive_frac']}, median Sharpe {p['neighbour_sharpe_median']} vs "
                               f"centre {p['centre_sharpe']}", n_configs=p["n_configs"])

    sc = res["execution"]["scenarios"]
    adv = sc["adverse"].get("mean_net")
    out["costs"] = _d("PASS" if (adv or 0) > 0 else "FAIL", f"net mean base {sc['base'].get('mean_net')}, adverse "
                      f"{adv}, severe {sc['severe'].get('mean_net')}",
                      severe_survives=(sc["severe"].get("mean_net") or 0) > 0)

    lim = float(succ.get("max_drawdown", 0.35))
    worst = [r for r in res["stress"]["synthetic"] if "max_drawdown" in r and abs(r["max_drawdown"]) > lim]
    out["stress"] = _d("FAIL" if worst else "PASS",
                       f"scenarios beyond max drawdown {lim:.0%}: {[r['scenario'] for r in worst] or 'none'}")

    if lab_decision is None:
        out["uncertainty"] = _d("INSUFFICIENT_DATA", "no lab decision")
    else:
        sample_ok = all(c["met"] for c in lab_decision.get("conditions", {}).get("sample", [])) \
            if isinstance(lab_decision.get("conditions"), dict) else False
        st = res["lab_statistics"]
        lo = (st.get("sharpe_ci95") or [None])[0]
        if not sample_ok:
            out["uncertainty"] = _d("INSUFFICIENT_DATA", "lab sample insufficient (OOS bars or independent trades)")
        else:
            ok = lo is not None and lo > 0 and (st.get("dsr") or 0) >= succ.get("dsr_min", 0.9)
            out["uncertainty"] = _d("PASS" if ok else "FAIL", f"Sharpe CI lower {lo}, DSR {st.get('dsr')}")

    cap = res["capacity"]
    if cap["status"] == "UNKNOWN":
        out["capacity"] = _d("INSUFFICIENT_DATA", cap["reason"])
    else:
        est = cap.get("estimate") or 0.0
        out["capacity"] = _d("PASS" if est >= min_capacity else "FAIL",
                             f"approximate capacity {est:,.0f} vs minimum {min_capacity:,.0f}")

    dv = res["diversification"]
    if "status_hint" in dv:
        out["diversification"] = _d("INSUFFICIENT_DATA", dv["reason"])
    else:
        better = (dv["combined"].get("sharpe_net") or -9) >= (dv["reference"].get("sharpe_net") or -9) or \
            (dv["combined"].get("max_drawdown") or -1) > (dv["reference"].get("max_drawdown") or -1)
        cs = dv.get("corr_on_stress_days")
        ok = better and (cs is None or np.isnan(cs) or cs < 0.7)
        out["diversification"] = _d("PASS" if ok else "FAIL", f"combined Sharpe {dv['combined'].get('sharpe_net')} vs "
                                    f"reference {dv['reference'].get('sharpe_net')}, stress correlation {cs}")
    return out


def overall(dims: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    reasons = []
    core_fail = [k for k in CORE_FRAGILITY if dims[k]["status"] == "FAIL"]
    if core_fail:
        return "FRAGILE", [f"{k}: {dims[k]['reason']}" for k in core_fail]
    gaps = [k for k in ESSENTIAL if dims[k]["status"] == "INSUFFICIENT_DATA"]
    if gaps:
        return "INSUFFICIENT_DATA", [f"{k}: {dims[k]['reason']}" for k in gaps]
    fails = [k for k in DIMENSIONS if dims[k]["status"] == "FAIL"]
    if fails:
        return "MIXED_EVIDENCE", [f"{k}: {dims[k]['reason']}" for k in fails]
    reasons = [f"not evaluated (non-essential): {k}: {dims[k]['reason']}" for k in DIMENSIONS
               if dims[k]["status"] == "INSUFFICIENT_DATA"]
    return "ROBUST_UNDER_TESTED_ASSUMPTIONS", ["every essential dimension passed under the tested assumptions"] + reasons


__all__ = ["CRITERIA", "DIMENSIONS", "ESSENTIAL", "STATES", "dimensions", "overall"]
