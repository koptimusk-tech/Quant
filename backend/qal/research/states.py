"""Research state machine. Transitions are deterministic and only the policy engine may promote.

Research path of a hypothesis::

    DISCOVERED -> HYPOTHESIS_FORMULATED -> EXPERIMENT_PENDING -> IN_SAMPLE_EVALUATED
        -> OUT_OF_SAMPLE_EVALUATED -> VALIDATION_PASSED | VALIDATION_FAILED
    VALIDATION_PASSED -> PAPER_TRADING_ELIGIBLE   (policy, after the holdout test and explicit human approval)

``BACKTEST_PENDING`` is the name used before this version for ``EXPERIMENT_PENDING``; rows stored with it
keep working (same transitions). PAPER_TRADING_ELIGIBLE only marks a hypothesis as a candidate: nothing
trades. PAPER_TRADING / RISK_APPROVED / LIVE_* need gates that are not implemented, so they stay unreachable.
"""

from __future__ import annotations

STATES = (
    "DISCOVERED", "INVESTIGATING", "HYPOTHESIS_FORMULATED", "EXPERIMENT_PENDING", "BACKTEST_PENDING",
    "IN_SAMPLE_EVALUATED", "OUT_OF_SAMPLE_EVALUATED", "VALIDATION_FAILED", "VALIDATION_PASSED",
    "PAPER_TRADING_ELIGIBLE", "PAPER_TRADING", "RISK_APPROVED", "LIVE_ELIGIBLE", "LIVE_ENABLED", "SUSPENDED",
    "REJECTED",
)

# (from, to) -> who may perform it. "policy" = only the deterministic policy engine after its checks.
TRANSITIONS: dict[tuple[str, str], str] = {
    ("HYPOTHESIS_FORMULATED", "EXPERIMENT_PENDING"): "any",
    ("VALIDATION_FAILED", "EXPERIMENT_PENDING"): "any",  # re-run (same frozen contract; trials accumulate)
    ("VALIDATION_PASSED", "EXPERIMENT_PENDING"): "any",
    ("EXPERIMENT_PENDING", "IN_SAMPLE_EVALUATED"): "policy",
    ("EXPERIMENT_PENDING", "HYPOTHESIS_FORMULATED"): "policy",  # run crashed
    ("IN_SAMPLE_EVALUATED", "OUT_OF_SAMPLE_EVALUATED"): "policy",
    ("IN_SAMPLE_EVALUATED", "HYPOTHESIS_FORMULATED"): "policy",
    ("OUT_OF_SAMPLE_EVALUATED", "VALIDATION_FAILED"): "policy",
    ("OUT_OF_SAMPLE_EVALUATED", "VALIDATION_PASSED"): "policy",
    ("OUT_OF_SAMPLE_EVALUATED", "HYPOTHESIS_FORMULATED"): "policy",
    ("VALIDATION_PASSED", "PAPER_TRADING_ELIGIBLE"): "policy",  # holdout + approval checks in research.promotion
    ("PAPER_TRADING_ELIGIBLE", "PAPER_TRADING"): "policy",  # requires Gate E (not implemented)
    ("VALIDATION_PASSED", "PAPER_TRADING"): "policy",
    ("PAPER_TRADING", "RISK_APPROVED"): "policy",
    ("RISK_APPROVED", "LIVE_ELIGIBLE"): "policy",
    ("LIVE_ELIGIBLE", "LIVE_ENABLED"): "policy",  # requires Gate F (explicit human authorisation)
}
# Rows written before EXPERIMENT_PENDING existed: the old pending state moves on like the new one.
for (_frm, _to), _who in list(TRANSITIONS.items()):
    if _frm == "EXPERIMENT_PENDING":
        TRANSITIONS[("BACKTEST_PENDING", _to)] = _who
TRANSITIONS[("BACKTEST_PENDING", "VALIDATION_FAILED")] = "policy"
TRANSITIONS[("BACKTEST_PENDING", "VALIDATION_PASSED")] = "policy"
for s in STATES:
    if s not in ("REJECTED",):
        TRANSITIONS[(s, "REJECTED")] = "any"
        TRANSITIONS[(s, "SUSPENDED")] = "any"
TRANSITIONS[("SUSPENDED", "HYPOTHESIS_FORMULATED")] = "any"

# Promotions that need gates which do not exist yet are refused with an explicit reason.
UNAVAILABLE_GATES = {
    "PAPER_TRADING": ("simulated paper sessions (Phase 7) run eligible strategies without changing their status; "
                      "the Gate E criteria for this status are not defined, so no evaluation promotes a strategy"),
    "RISK_APPROVED": "risk approval is a human decision outside the app; the Phase 6 risk manager only enforces limits",
    "LIVE_ELIGIBLE": "live eligibility is not implemented",
    "LIVE_ENABLED": "Gate F (explicit operational authorisation) and execution adapters are not implemented",
}


class TransitionError(ValueError):
    pass


def check_transition(current: str, target: str, actor_kind: str) -> None:
    """actor_kind: "policy" for the policy engine; users and agents cannot take policy transitions."""
    if target not in STATES:
        raise TransitionError(f"unknown state {target}")
    rule = TRANSITIONS.get((current, target))
    if rule is None:
        raise TransitionError(f"transition {current} -> {target} is not allowed")
    if rule == "policy" and actor_kind != "policy":
        raise TransitionError(f"{current} -> {target} can only be decided by the policy engine")
    if target in UNAVAILABLE_GATES:
        raise TransitionError(f"blocked: {UNAVAILABLE_GATES[target]}")
