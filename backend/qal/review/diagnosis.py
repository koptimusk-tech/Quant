"""Structured diagnosis when results are worse than expected.

"Worse than expected" means at least one of: the paper mean return per bar below the same-bar backtest beyond the
protocol tolerance, the excess over the baseline with its whole interval below zero, the paper mean below the
research out-of-sample mean per bar, or a reject criterion met.

Each possible cause gets one of three statuses:

* ``CONFIRMED`` - the data shows the cause is present (for instance an integrity failure, a configuration
  difference, costs above the protocol limit, missing bars above the limit, or a sample below the horizon);
  confirmed presence is not a measured share of the shortfall unless the evidence says so;
* ``COMPATIBLE`` - consistent with the data but not demonstrated;
* ``NOT_EVALUABLE`` - the data needed is not available, or nothing in it points either way.

For a few mechanical causes the data can also exclude the cause (the configurations match, the integrity checks
pass, the costs are not above the backtest's, the sample is complete). Those are reported as
``RULED_OUT_BY_DATA`` rather than forced into one of the three statuses.

The diagnosis never changes the strategy. A modification is a new hypothesis or candidate version with its own
protocol and its own evaluation.
"""

from __future__ import annotations

from typing import Any

CONFIRMED, COMPATIBLE, NOT_EVALUABLE = "CONFIRMED", "COMPATIBLE", "NOT_EVALUABLE"
RULED_OUT = "RULED_OUT_BY_DATA"


def _c(cause: str, status: str, evidence: str, to_distinguish: str) -> dict[str, Any]:
    return {"cause": cause, "status": status, "evidence": evidence, "data_to_distinguish": to_distinguish}


def worse_than_expected(trk: dict[str, Any], cand: dict[str, Any], proto: dict[str, Any], unc: dict[str, Any],
                        crit: dict[str, Any]) -> list[str]:
    why = []
    d = trk.get("diff_vs_backtest") or {}
    if d.get("mean_bps") is not None and d["mean_bps"] < -proto["tracking_tolerance_bps"]:
        why.append(f"paper below the same-bar backtest by {-d['mean_bps']:.2f} bps per bar")
    ex = unc.get("excess_ci95")
    if ex and ex[1] < 0:
        why.append(f"excess over the baseline below zero with its whole interval {ex}")
    oos = ((cand.get("results") or {}).get("oos") or {}).get("mean_net")
    pm = (trk.get("paper") or {}).get("mean")
    if oos is not None and pm is not None and pm < oos:
        why.append(f"paper mean {pm * 1e4:.2f} bps per bar below the research out-of-sample mean {oos * 1e4:.2f} bps "
                   "(another period: context)")
    if crit.get("reject_triggered"):
        why.append("reject criteria met: " + ", ".join(crit["reject_triggered"]))
    return why


def diagnose(trk: dict[str, Any], cmp: dict[str, Any] | None, cand: dict[str, Any], proto: dict[str, Any],
             unc: dict[str, Any], crit: dict[str, Any], integ: dict[str, Any], metrics: dict[str, Any],
             prog_res: dict[str, Any], mode: str, regime: list[dict[str, Any]]) -> dict[str, Any]:
    why = worse_than_expected(trk, cand, proto, unc, crit)
    if not why:
        return {"triggered": False, "why": [], "causes": [],
                "note": "Results are not worse than expected by the review's definition: no diagnosis is needed."}
    cmp = cmp or {}
    res = cand.get("results") or {}
    n, hz = trk.get("bars", 0), proto["horizon"]["horizon_bars"]
    causes = []
    shifts = [r for r in regime if r.get("shift")]
    causes.append(_c("market regime change", COMPATIBLE if shifts else NOT_EVALUABLE,
                     "volatility during the period vs the prior 250 bars: "
                     + (", ".join(f"{r['instrument']} x{r['vol_ratio_session_vs_prior_250']}" for r in regime)
                        or "not available"),
                     "results by regime over a longer sample; the same strategy on periods with a similar regime"))
    pbo, dsr = res.get("pbo"), res.get("dsr")
    causes.append(_c("historical overfitting",
                     COMPATIBLE if (pbo is not None and pbo > 0.5) or (dsr is not None and dsr < 0.5) else NOT_EVALUABLE,
                     f"PBO {pbo}, deflated Sharpe {dsr}, {res.get('n_trials')} configurations tried in the research",
                     "a longer prospective sample; PBO and DSR recomputed with every configuration tried"))
    ratio = (cmp.get("costs") or {}).get("ratio")
    causes.append(_c("underestimated costs",
                     NOT_EVALUABLE if ratio is None else CONFIRMED if ratio > proto["max_cost_ratio"] else
                     COMPATIBLE if ratio > 1 else RULED_OUT,
                     f"paper/backtest costs {ratio}; both sides use configured estimates, not observed costs, so costs "
                     "higher than the estimates in real markets remain possible",
                     "observed spreads and commissions for the instruments (not available: no real executions)"))
    fr = (cmp.get("fills") or {}).get("fill_rate")
    causes.append(_c("unrealistic slippage or fills", COMPATIBLE if fr is not None and fr < proto["min_fill_rate"]
                     else NOT_EVALUABLE,
                     f"simulated fill rate {fr}; slippage comes from the cost model; no real fills exist to compare",
                     "real or broker-simulated fills for the same orders (not available)"))
    errors = prog_res.get("errors", [])
    data_p = [p for p in prog_res.get("pauses", []) if p["cause"] == "stale_or_corrupt_data"]
    miss = prog_res.get("missing_bars", 0)
    delays = [x.get("processing_delay_h") for x in prog_res.get("feed_log", [])
              if x.get("processing_delay_h") is not None]
    late = mode == "B" and bool(delays) and max(delays) > 24
    causes.append(_c("data quality or latency",
                     CONFIRMED if data_p or miss / max(1, n + miss) > proto["max_missing_frac"] else
                     COMPATIBLE if miss or late else NOT_EVALUABLE,
                     f"{miss} missing or skipped bar(s), {len(data_p)} data pause(s), max processing delay "
                     f"{max(delays) if delays else None} h" + ("" if mode == "B" else " (replay: latency not measured)"),
                     "the provider's revisions and the arrival time of every bar"))
    sig = cmp.get("signal_frequency") or {}
    sdiff = (sig["paper_changes_per_100_bars"] / sig["backtest_changes_per_100_bars"]
             if sig.get("backtest_changes_per_100_bars") else None)
    oos_rate = (proto.get("horizon") or {}).get("trade_rate_per_bar")
    trade_rate = trk.get("trades", 0) / n if n else None
    shifted = ((sig.get("agreement") is not None and sig["agreement"] < proto["min_signal_agreement"])
               or (sdiff is not None and not 0.5 <= sdiff <= 2)
               or (bool(oos_rate) and trade_rate is not None and not 0.5 <= trade_rate / oos_rate <= 2))
    causes.append(_c("change in the signal distribution", COMPATIBLE if shifted else NOT_EVALUABLE,
                     f"signal agreement {sig.get('agreement')}, paper/backtest change frequency {sdiff}, trade rate "
                     f"{trade_rate} per bar vs research {oos_rate}",
                     "the distribution of the signal inputs in research vs paper"))
    trades = [t["net_pnl"] for t in (trk.get("trade_list") or [])]
    tot = sum(abs(x) for x in trades)
    top = sum(sorted((abs(x) for x in trades), reverse=True)[:3]) / tot if len(trades) >= 5 and tot else None
    causes.append(_c("concentration in a few events", COMPATIBLE if top is not None and top > 0.5 else NOT_EVALUABLE,
                     f"the 3 largest trades are {top:.0%} of the absolute trade P&L" if top is not None else
                     "fewer than 5 trades", "more trades; results without the largest events"))
    conc = ((metrics.get("risk") or {}).get("concentration_by_asset") or {}).get("value") or {}
    assets = conc.get("assets") or []
    big = max((a["share_abs"] or 0 for a in assets), default=0)
    causes.append(_c("dependence on particular assets", COMPATIBLE if len(assets) >= 2 and big > 0.7 else
                     NOT_EVALUABLE, f"largest asset share of absolute P&L {big:.0%} over {len(assets)} asset(s)",
                     "the same hypothesis on other, less correlated assets"))
    causes.append(_c("implementation errors", CONFIRMED if integ["blocking"] else
                     COMPATIBLE if errors or integ["status"] == "LIMITED" else RULED_OUT,
                     f"integrity {integ['status']} ({', '.join(integ['blocking']) or 'no material failure'}); "
                     f"{len(errors)} engine error(s)", "the failing checks' evidence and the engine error log"))
    conv_bad = [c["check"] for c in cmp.get("conventions", []) if not c["passed"]]
    causes.append(_c("configuration differences", NOT_EVALUABLE if not cmp else CONFIRMED if conv_bad else RULED_OUT,
                     f"conventions that differ: {conv_bad}" if conv_bad else "paper and backtest conventions checked",
                     "the session and candidate configurations"))
    small = n < hz or trk.get("trades", 0) < proto["min_trades"]
    causes.append(_c("insufficient sample size", CONFIRMED if small else
                     COMPATIBLE if (unc.get("effective_n") or 0) < 100 else RULED_OUT,
                     f"{n} of {hz} bars, {trk.get('trades', 0)} of {proto['min_trades']} trades, "
                     f"{unc.get('effective_n') and round(unc['effective_n'])} effective observations",
                     "the remaining bars of the protocol"))
    return {"triggered": True, "why": why, "causes": causes,
            "note": "No cause is asserted beyond its status. The strategy is not modified: a change is a new "
                    "hypothesis or candidate version with its own protocol and evaluation."}
