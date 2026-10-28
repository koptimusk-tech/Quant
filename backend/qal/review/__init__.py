"""Paper trading results review and strategy readiness decision (Phase 13).

Reads what the paper trading validation program (Phase 12) stored, checks its integrity, computes metrics with
their sample size and uncertainty, compares them with the backtest and the baseline, evaluates the protocol that
was fixed before the program started, and records a deterministic, versioned decision per strategy version.

Simulation only: nothing in this package can send an order to a broker, an exchange or an account. No state,
including ``COMPLETED`` and ``SUPPORTED_FOR_FURTHER_RESEARCH``, authorises real trading.
"""

PAPER_ONLY = True
