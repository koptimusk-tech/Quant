"""Classification of a program: rules that can be checked, then a documented human review.

Rule states, in order:

* ``PAUSED_REVIEW`` - a pause is open (the cause, its evidence and time are logged); a reject criterion that is
  already met is reported with it.
* ``REJECTED`` - a reject criterion of the protocol is met: the drawdown stop at any time; with the sample complete,
  the excess over the baseline below zero with its whole CI, the simulation not reproducing the candidate (mean
  difference with the same-bar backtest outside the tolerance with its whole CI) or costs above the reject ratio.
* ``CONTINUE_PAPER`` - the horizon is not reached and nothing stops the program.
* ``COMPLETED`` - the horizon is reached and every acceptance criterion is met. It means the protocol finished
  with its criteria met under simulation; it is not an authorisation to trade and nothing here can trade.
* ``INCONCLUSIVE`` - the horizon (or the maximum duration, or the end of the replay data) is reached, or the program
  was stopped early, without meeting the acceptance or reject criteria. Never forced either way.

A person (never an agent) records the classification with a written rationale. The review may confirm the rule
state or choose a more conservative one (``ALLOWED``); it can never upgrade it.
"""

from __future__ import annotations

from typing import Any

STATES = ("CONTINUE_PAPER", "PAUSED_REVIEW", "REJECTED", "INCONCLUSIVE", "COMPLETED")
ALLOWED = {"COMPLETED": ("COMPLETED", "INCONCLUSIVE", "REJECTED"),
           "INCONCLUSIVE": ("INCONCLUSIVE", "REJECTED"),
           "REJECTED": ("REJECTED",),
           "CONTINUE_PAPER": ("CONTINUE_PAPER", "PAUSED_REVIEW", "REJECTED"),
           "PAUSED_REVIEW": ("PAUSED_REVIEW", "REJECTED")}


def _c(name: str, kind: str, met: bool | None, detail: str) -> dict[str, Any]:
    return {"criterion": name, "kind": kind, "met": bool(met) if met is not None else False,
            "evaluable": met is not None, "detail": detail}


def criteria(prog: dict[str, Any], proto: dict[str, Any], trk: dict[str, Any], cmp: dict[str, Any] | None,
             cand: dict[str, Any]) -> list[dict[str, Any]]:
    rl = cand["risk_limits"]
    n, trades = trk.get("bars", 0), trk.get("trades", 0)
    sample = n >= proto["horizon"]["horizon_bars"] and trades >= proto["min_trades"]
    paper = trk.get("paper") or {}
    ex = (trk.get("excess_vs_baseline") or {}).get("mean_ci95")
    dbt = trk.get("diff_vs_backtest") or {}
    dci = dbt.get("mean_ci95_bps")
    tol = proto["tracking_tolerance_bps"]
    comparable = bool(cmp and cmp.get("comparable"))
    costs = (cmp or {}).get("costs") or {}
    ratio = costs.get("ratio")
    sigf = (cmp or {}).get("signal_frequency") or {}
    fills = (cmp or {}).get("fills") or {}
    dd = (cmp or {}).get("drawdown") or {}
    worst = min([paper.get("max_drawdown") or 0.0] + [p.get("evidence", {}).get("drawdown", 0.0)
                                                       for p in prog.get("pauses", [])])
    missing = prog.get("missing_bars", 0) / max(1, n + prog.get("missing_bars", 0))
    open_pauses = [p for p in prog.get("pauses", []) if not p.get("resolved_at")]
    rec_ok = not any(p["cause"] == "inconsistent_orders_positions_pnl" and not p.get("resolved_at")
                     for p in prog.get("pauses", []))
    out = [
        _c("sample_complete", "accept", sample, f"{n} bars of {proto['horizon']['horizon_bars']} planned, {trades} "
           f"trades of {proto['min_trades']}"),
        _c("primary_excess_over_baseline", "accept", (ex[0] > 0) if ex else None,
           f"excess over {proto['baseline']}: 95% CI {ex}" if ex else "not enough bars for an interval"),
        _c("backtest_consistency", "accept", (comparable and dci is not None and -tol <= dci[0] and dci[1] <= tol
                                              and (dbt.get("correlation") or -1) >= proto["min_return_correlation"]
                                              and (sigf.get("agreement") or 0) >= proto["min_signal_agreement"])
           if dci else None,
           f"mean difference CI {dci} bps (tolerance ±{tol}), correlation {dbt.get('correlation')}, signal agreement "
           f"{sigf.get('agreement')}" + ("" if comparable else "; conventions differ: not comparable")),
        _c("costs", "accept", (ratio <= proto["max_cost_ratio"]) if ratio is not None else None,
           f"paper / backtest costs {ratio}"),
        _c("fill_rate", "accept", (fills["fill_rate"] >= proto["min_fill_rate"]) if fills.get("fill_rate") is not None
           else None, f"fill rate {fills.get('fill_rate')}"),
        _c("drawdown", "accept", (worst > -rl["drawdown_stop"] and (dd.get("ratio") is None or dd["ratio"] <=
                                                                    proto["max_drawdown_ratio"])) if paper else None,
           f"worst drawdown {worst:.2%}, paper / backtest {dd.get('ratio')}"),
        _c("missing_data", "accept", missing <= proto["max_missing_frac"], f"{missing:.1%} of bars missing or skipped"),
        _c("operation", "accept", not open_pauses and rec_ok, f"{len(open_pauses)} open pause(s)"),
        _c("drawdown_stop", "reject", worst <= -rl["drawdown_stop"], f"worst drawdown {worst:.2%} vs stop "
           f"{-rl['drawdown_stop']:.0%}"),
        _c("excess_below_baseline", "reject", (sample and ex[1] < 0) if ex else None,
           f"excess CI {ex}" + ("" if sample else " (sample incomplete: not applied)")),
        _c("does_not_reproduce_backtest", "reject", (sample and comparable and (dci[0] > tol or dci[1] < -tol))
           if dci else None, f"mean difference CI {dci} bps" + ("" if sample else " (sample incomplete: not applied)")),
        _c("costs_far_above_backtest", "reject", (sample and ratio > proto["reject_cost_ratio"]) if ratio is not None
           else None, f"paper / backtest costs {ratio} vs {proto['reject_cost_ratio']}"
           + ("" if sample else " (sample incomplete: not applied)")),
    ]
    return out


def rule_state(prog: dict[str, Any], proto: dict[str, Any], trk: dict[str, Any], cmp: dict[str, Any] | None,
               cand: dict[str, Any]) -> dict[str, Any]:
    crit = criteria(prog, proto, trk, cmp, cand)
    rejects = [c for c in crit if c["kind"] == "reject" and c["met"]]
    accepts = [c for c in crit if c["kind"] == "accept"]
    open_pauses = [p for p in prog.get("pauses", []) if not p.get("resolved_at")]
    ended = prog.get("status") in ("FINISHED", "STOPPED_EARLY")
    if rejects:
        state, reasons = "REJECTED", [c["detail"] for c in rejects]
    elif open_pauses and not ended:
        state, reasons = "PAUSED_REVIEW", [f"{p['cause']}: {p['message']}" for p in open_pauses]
    elif not ended:
        state, reasons = "CONTINUE_PAPER", [c["detail"] for c in accepts if c["criterion"] == "sample_complete"]
    elif prog.get("status") == "STOPPED_EARLY":
        state, reasons = "INCONCLUSIVE", [(f"stopped early by {prog.get('stopped', {}).get('by')}: "
                                           f"{prog.get('stopped', {}).get('reason')}; the whole period is kept")]
    elif all(c["met"] for c in accepts):
        state, reasons = "COMPLETED", ["every acceptance criterion met at the planned horizon"]
    else:
        state, reasons = "INCONCLUSIVE", [f"{c['criterion']}: {c['detail']}" for c in accepts if not c["met"]]
    note = ("COMPLETED means the protocol finished with its criteria met in simulation. It does not authorise real "
            "trading, it is not a forecast and nothing in this program can send an order.")
    if cand["admission"]["kind"] == "exploratory":
        note += " EXPLORATORY candidate: the result is not validation evidence."
    if cand["is_synthetic"]:
        note += " SYNTHETIC data: tests the procedure only."
    return {"state": state, "reasons": reasons, "criteria": crit, "provisional": trk.get("provisional", True),
            "note": note}
