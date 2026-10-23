"""Deterministic hard gates (policy engine). No agent or API caller can override a decision.

Gate A data quality, B statistical validity, C robustness, D economic viability. Gates E and F
(portfolio risk, operational authorisation) arrive in Phase 6/7 and currently always block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from qal.research.contract import HypothesisContract

POLICY_VERSION = "2.0.0"


@dataclass
class GateResult:
    gate: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def _f(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def _check(cond: bool | None, ok: str, fail: str, reasons: list[str]) -> bool:
    if cond is None:
        reasons.append(f"not evaluable: {fail}")
        return False
    reasons.append(("✓ " + ok) if cond else ("✗ " + fail))
    return bool(cond)


def evaluate_gates(contract: HypothesisContract, report: dict[str, Any], dataset_quality: dict[str, str]) -> list[GateResult]:
    s = contract.success
    oos = report["oos"]["metrics"]
    rob = report["robustness"]
    ovf = report["overfitting"]
    leak = report["leakage_checks"]
    out: list[GateResult] = []

    r: list[str] = []
    ok = _check(all(v != "fail" for v in dataset_quality.values()), "all datasets pass data quality",
                f"dataset quality failure: {dataset_quality}", r)
    out.append(GateResult("A", ok, r, {"dataset_quality": dataset_quality}))

    r = []
    primary = oos.get(s.metric)
    checks = [
        _check(all(c["passed"] for c in leak), "leakage checks passed", "a leakage check failed", r),
        _check(oos.get("n_trades", 0) >= s.min_trades, f"{oos.get('n_trades')} OOS trades >= {s.min_trades}",
               f"only {oos.get('n_trades')} OOS trades (< {s.min_trades})", r),
        _check(None if primary is None else primary >= s.min_value, f"OOS {s.metric} {primary:.3f} >= {s.min_value}"
               if primary is not None else "", f"OOS {s.metric} {_f(primary)} < {s.min_value}", r),
        _check(None if ovf.get("dsr") is None else ovf["dsr"] >= s.dsr_min,
               f"Deflated Sharpe {ovf.get('dsr', 0):.3f} >= {s.dsr_min}", f"Deflated Sharpe {_f(ovf.get('dsr'))} < {s.dsr_min}", r),
        _check(None if rob.get("placebo_pvalue") is None else rob["placebo_pvalue"] <= s.placebo_max_pvalue,
               f"placebo p {_f(rob.get('placebo_pvalue'))} <= {s.placebo_max_pvalue}",
               f"placebo p {_f(rob.get('placebo_pvalue'))} > {s.placebo_max_pvalue}", r),
    ]
    if s.require_ci_lower_positive:
        checks.append(_check(rob.get("bootstrap", {}).get("sharpe_ci_low", -1) > 0, "bootstrap Sharpe CI above 0",
                             "bootstrap Sharpe CI includes 0", r))
    out.append(GateResult("B", all(checks), r, {"oos_metric": primary, "dsr": ovf.get("dsr"),
                                                  "placebo_pvalue": rob.get("placebo_pvalue")}))

    r = []
    dd = oos.get("max_drawdown")
    top5 = oos.get("top5_trades_share")
    pbo = ovf.get("pbo")
    checks = [
        _check(rob["positive_window_frac"] >= s.min_positive_window_frac,
               f"{rob['positive_window_frac']:.0%} of OOS windows positive", f"only {rob['positive_window_frac']:.0%} of OOS windows positive", r),
        _check(rob["robust_param_frac"] >= s.min_robust_param_frac,
               f"{rob['robust_param_frac']:.0%} of parameter variants positive", f"only {rob['robust_param_frac']:.0%} of parameter variants positive", r),
        _check(None if dd is None else abs(dd) <= s.max_drawdown, f"max drawdown {dd:.1%} within {s.max_drawdown:.0%}" if dd is not None else "",
               f"max drawdown {_f(dd)} beyond {s.max_drawdown}", r),
        _check(True if top5 is None else top5 <= s.max_top5_trades_share,
               "profits not concentrated in 5 trades" if top5 is not None else "no net profit to concentrate",
               f"top-5 trades are {top5:.0%} of net PnL" if top5 is not None else "", r),
        _check(True if pbo is None else pbo <= s.max_pbo, f"PBO {_f(pbo)} <= {s.max_pbo}" if pbo is not None else "PBO not applicable (single variant)",
               f"PBO {_f(pbo)} > {s.max_pbo}", r),
    ]
    out.append(GateResult("C", all(checks), r, {"positive_window_frac": rob["positive_window_frac"],
                                                  "robust_param_frac": rob["robust_param_frac"], "pbo": pbo}))

    r = []
    cs = {round(c["multiplier"], 2): c for c in rob["cost_sensitivity"]}
    at = cs.get(round(s.cost_multiplier_survive, 2))
    n_bars = report["oos"]["metrics"].get("bars") or 1
    breach_frac = report["oos"]["capacity_breaches"] / n_bars
    checks = [
        _check(None if at is None else at["mean_net"] > 0, f"edge survives {s.cost_multiplier_survive}x costs",
               f"edge disappears at {s.cost_multiplier_survive}x costs", r),
        _check(breach_frac <= s.max_capacity_breach_frac, f"capacity breaches {breach_frac:.1%} of bars",
               f"liquidity/capacity breaches on {breach_frac:.1%} of bars", r),
    ]
    out.append(GateResult("D", all(checks), r, {"cost_sensitivity": rob["cost_sensitivity"], "capacity_breach_frac": breach_frac}))
    return out
