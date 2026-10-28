"""Deterministic, versioned decision engine of the review.

Rules, evaluated in this order (the first that applies decides):

1. ``INCONCLUSIVE`` - no protocol was registered before the session: there are no predefined criteria to judge it
   against.
2. ``PAUSED_REVIEW`` - an integrity check failed in a way that can change the results (evaluation blocked).
3. ``REJECTED`` - a reject criterion of the original protocol is met (the drawdown stop at any time; with the
   sample complete, the excess over the baseline below zero with its whole interval, the simulation not
   reproducing the backtest, or costs above the reject ratio).
4. ``PAUSED_REVIEW`` - a pause of the program is open.
5. ``CONTINUE_PAPER`` - the program has not finished: the protocol has not concluded.
6. ``INCONCLUSIVE`` - stopped early, or finished without meeting every acceptance criterion.
7. ``COMPLETED`` - finished with every acceptance criterion met, but the evidence cannot support the hypothesis:
   synthetic data, a historical replay, an exploratory admission, or a readiness check not met (independent
   trades, stability, invalidating incidents) or the multiple-testing adjusted p-value not below 0.05.
8. ``SUPPORTED_FOR_FURTHER_RESEARCH`` - finished with every acceptance criterion met on prospective bars of real
   data, a validated admission, every readiness check met and the Holm-adjusted p-value below 0.05.

No state authorises real trading, and ``SUPPORTED_FOR_FURTHER_RESEARCH`` only says that continuing to research
the hypothesis under the documented conditions is justified. A person can record a manual review that confirms the
state or chooses a more conservative one; it is stored as a new decision and never replaces this one. Agents and
LLMs cannot change a classification.
"""

from __future__ import annotations

from typing import Any

ENGINE_VERSION = "review-decision@1.0.0"
STATES = ("CONTINUE_PAPER", "PAUSED_REVIEW", "REJECTED", "INCONCLUSIVE", "COMPLETED",
          "SUPPORTED_FOR_FURTHER_RESEARCH")
ALPHA = 0.05
MIN_PROSPECTIVE_SHARE = 0.9
ALLOWED_MANUAL = {
    "SUPPORTED_FOR_FURTHER_RESEARCH": ("SUPPORTED_FOR_FURTHER_RESEARCH", "COMPLETED", "INCONCLUSIVE", "PAUSED_REVIEW",
                                       "REJECTED"),
    "COMPLETED": ("COMPLETED", "INCONCLUSIVE", "PAUSED_REVIEW", "REJECTED"),
    "INCONCLUSIVE": ("INCONCLUSIVE", "PAUSED_REVIEW", "REJECTED"),
    "CONTINUE_PAPER": ("CONTINUE_PAPER", "PAUSED_REVIEW", "REJECTED"),
    "PAUSED_REVIEW": ("PAUSED_REVIEW", "REJECTED"),
    "REJECTED": ("REJECTED",),
}
NOT_AUTHORISATION = ("No state authorises real trading or sending any order. SUPPORTED_FOR_FURTHER_RESEARCH only "
                     "justifies more research on the hypothesis under the documented conditions.")


def _rule(name: str, applies: bool, detail: str) -> dict[str, Any]:
    return {"rule": name, "applies": bool(applies), "detail": detail}


def decide(ctx: dict[str, Any]) -> dict[str, Any]:
    """``ctx`` holds: has_protocol, evidence (class and counts), integrity, program_status, criteria, readiness,
    open_pauses, admission_kind, is_synthetic, adjusted_p. Pure function: same inputs, same output."""
    rules: list[dict[str, Any]] = []
    ev = ctx["evidence"]
    integ = ctx["integrity"]
    crit = ctx.get("criteria") or {}
    ready = {r["check"]: r for r in ctx.get("readiness") or []}
    status = ctx.get("program_status")
    adj_p = ctx.get("adjusted_p")
    actions: list[str] = []
    uncertainties: list[str] = list(ctx.get("uncertainties") or [])

    def done(state: str, reason: str) -> dict[str, Any]:
        return {"state": state, "reason": reason, "rules": rules, "recommended_actions": actions or [
            "keep collecting evidence under the same protocol"], "uncertainties": uncertainties,
            "engine_version": ENGINE_VERSION, "not_an_authorisation": NOT_AUTHORISATION}

    r = _rule("no_registered_protocol", not ctx["has_protocol"], "a protocol fixed before the session is required to "
              "evaluate it")
    rules.append(r)
    if r["applies"]:
        actions.append("start a validation program with a protocol frozen before the evaluation period")
        return done("INCONCLUSIVE", "no protocol was registered before this session, so there are no predefined "
                                    "criteria to judge it against")
    r = _rule("integrity_blocked", integ["status"] == "BLOCKED", f"integrity {integ['status']}: "
              f"{', '.join(integ['blocking']) or 'no material failure'}")
    rules.append(r)
    if r["applies"]:
        actions.append("investigate the failing integrity checks before reading any result")
        return done("PAUSED_REVIEW", "an integrity check failed in a way that can change the results: "
                    + ", ".join(integ["blocking"]))
    rej = crit.get("reject_triggered") or []
    r = _rule("reject_criterion", bool(rej), ", ".join(rej) or "none met")
    rules.append(r)
    if r["applies"]:
        actions.append("close the protocol; a modified strategy is a new hypothesis or version with its own protocol")
        return done("REJECTED", "a reject criterion fixed before the start is met: " + ", ".join(rej))
    r = _rule("open_pause", ctx.get("open_pauses", 0) > 0, f"{ctx.get('open_pauses', 0)} open pause(s)")
    rules.append(r)
    if r["applies"]:
        actions.append("resolve the open pause with the audited procedure (person, reason, resolution)")
        return done("PAUSED_REVIEW", "the program has an open pause that needs investigation")
    r = _rule("protocol_not_concluded", status not in ("FINISHED", "STOPPED_EARLY"), f"program {status}")
    rules.append(r)
    if r["applies"]:
        actions.append("continue the program until its planned horizon")
        uncertainties.append("the protocol has not concluded: interim results are provisional")
        return done("CONTINUE_PAPER", "the protocol has not concluded; interim results do not support a conclusion")
    unmet = crit.get("unmet") or []
    r = _rule("stopped_early_or_criteria_unmet", status == "STOPPED_EARLY" or bool(unmet),
              "stopped early" if status == "STOPPED_EARLY" else f"unmet: {', '.join(unmet) or 'none'}")
    rules.append(r)
    if r["applies"]:
        actions.append("a further program, if any, needs a new protocol; the period observed stays in the record")
        return done("INCONCLUSIVE", ("the program was stopped before its horizon" if status == "STOPPED_EARLY" else
                                     "the protocol finished without meeting its acceptance criteria: "
                                     + ", ".join(unmet)) + "; the data does not allow a defensible conclusion")
    limits = []
    if ctx["is_synthetic"] or ev["class"] == "SYNTHETIC":
        limits.append("synthetic data: tests the procedure only")
    if ev["class"] != "PROSPECTIVE" or ev.get("prospective_share", 0) < MIN_PROSPECTIVE_SHARE:
        limits.append(f"evidence class {ev['class']} ({ev.get('prospective_bars', 0)} prospective bar(s)): data known "
                      "before the protocol is not prospective evidence")
    if ctx.get("admission_kind") == "exploratory":
        limits.append("exploratory admission: not validation evidence")
    for k in ("independent_trades", "stable_enough", "no_invalidating_incident"):
        if k in ready and ready[k]["met"] is not True:
            limits.append(f"readiness check {k} not met: {ready[k]['detail']}")
    if adj_p is None or adj_p >= ALPHA:
        limits.append(f"multiple-testing adjusted p {adj_p} not below {ALPHA}")
    r = _rule("evidence_supports_hypothesis", not limits, "; ".join(limits) or "all conditions met")
    rules.append(r)
    if not r["applies"]:
        actions.append("the protocol is closed; any further evaluation needs a new protocol on prospective data")
        uncertainties.extend(limits)
        return done("COMPLETED", "the protocol finished with its acceptance criteria met, but the evidence does not "
                                 "support the hypothesis: " + "; ".join(limits))
    actions.append("continue researching the hypothesis on new prospective data and other markets; nothing here "
                   "authorises real trading")
    return done("SUPPORTED_FOR_FURTHER_RESEARCH", "every predefined criterion and readiness check is met on prospective "
                                                  f"real data, adjusted p = {adj_p:.3g}")


def allowed_conclusion(state: str, ev_class: str) -> dict[str, list[str]]:
    can, cannot = [], [
        "that the strategy will be profitable in the future or with real money",
        "that a p-value is the probability that the strategy works",
        "anything about real execution: fills, spreads and slippage were simulated with estimated costs"]
    if state == "SUPPORTED_FOR_FURTHER_RESEARCH":
        can.append("the prospective simulated results met every predefined criterion; researching the hypothesis "
                   "further is justified under the documented conditions")
    elif state == "COMPLETED":
        can.append("the protocol finished with its acceptance criteria met in simulation")
        cannot.append("that the hypothesis is supported: the evidence class or the readiness checks do not allow it")
    elif state == "REJECTED":
        can.append("a reject criterion fixed before the start was met in this sample")
        cannot.append("that the hypothesis is false in general: the conclusion is limited to this protocol and sample")
    elif state == "INCONCLUSIVE":
        can.append("what was observed in the period, with its uncertainty")
        cannot.append("that the strategy works or does not work: the data does not allow a defensible conclusion")
    elif state == "CONTINUE_PAPER":
        can.append("the interim results, labelled provisional")
        cannot.append("any conclusion before the protocol's horizon; a short unfavourable period does not show the "
                      "strategy fails")
    elif state == "PAUSED_REVIEW":
        cannot.append("anything from the results until the pause or integrity problem is resolved")
    if ev_class != "PROSPECTIVE":
        cannot.append(f"that the results are prospective evidence: the evidence class is {ev_class}")
    return {"can_conclude": can, "cannot_conclude": cannot}
