"""Research state machine. Transitions are deterministic and only the policy engine may promote.

LIVE_ELIGIBLE does not allow automatic trading; LIVE_ENABLED needs independent authorisation
(Gate F), which is not implemented, so it is unreachable by design in this version.
"""

from __future__ import annotations

STATES = (
    "DISCOVERED", "INVESTIGATING", "HYPOTHESIS_FORMULATED", "BACKTEST_PENDING", "VALIDATION_FAILED",
    "VALIDATION_PASSED", "PAPER_TRADING", "RISK_APPROVED", "LIVE_ELIGIBLE", "LIVE_ENABLED", "SUSPENDED", "REJECTED",
)

# (from, to) -> who may perform it. "policy" = only the deterministic policy engine after gates.
TRANSITIONS: dict[tuple[str, str], str] = {
    ("HYPOTHESIS_FORMULATED", "BACKTEST_PENDING"): "any",
    ("VALIDATION_FAILED", "BACKTEST_PENDING"): "any",  # re-run (same frozen contract; trials accumulate)
    ("VALIDATION_PASSED", "BACKTEST_PENDING"): "any",
    ("BACKTEST_PENDING", "VALIDATION_FAILED"): "policy",
    ("BACKTEST_PENDING", "VALIDATION_PASSED"): "policy",
    ("BACKTEST_PENDING", "HYPOTHESIS_FORMULATED"): "policy",  # run crashed
    ("VALIDATION_PASSED", "PAPER_TRADING"): "policy",  # requires Gate E (Phase 6)
    ("PAPER_TRADING", "RISK_APPROVED"): "policy",
    ("RISK_APPROVED", "LIVE_ELIGIBLE"): "policy",
    ("LIVE_ELIGIBLE", "LIVE_ENABLED"): "policy",  # requires Gate F (explicit human authorisation)
}
for s in STATES:
    if s not in ("REJECTED",):
        TRANSITIONS[(s, "REJECTED")] = "any"
        TRANSITIONS[(s, "SUSPENDED")] = "any"
TRANSITIONS[("SUSPENDED", "HYPOTHESIS_FORMULATED")] = "any"

# Promotions that need gates which do not exist yet are refused with an explicit reason.
UNAVAILABLE_GATES = {
    "PAPER_TRADING": "Gate E (portfolio risk) and the paper trading engine arrive in Phase 6",
    "RISK_APPROVED": "Hierarchical Risk Manager arrives in Phase 6",
    "LIVE_ELIGIBLE": "Live eligibility requires Phase 6 risk controls",
    "LIVE_ENABLED": "Gate F (explicit operational authorisation) and execution adapters are not implemented",
}


class TransitionError(ValueError):
    pass


def check_transition(current: str, target: str, actor_kind: str) -> None:
    """actor_kind: "policy" for the policy engine, anything else for users/agents."""
    if target not in STATES:
        raise TransitionError(f"unknown state {target}")
    rule = TRANSITIONS.get((current, target))
    if rule is None:
        raise TransitionError(f"transition {current} -> {target} is not allowed")
    if rule == "policy" and actor_kind != "policy":
        raise TransitionError(f"{current} -> {target} can only be decided by the policy engine")
    if target in UNAVAILABLE_GATES:
        raise TransitionError(f"blocked: {UNAVAILABLE_GATES[target]}")
