"""Simulated paper trading (Phase 7).

Everything in this package is a simulation. There is no broker, exchange or account client here and none
may be added: orders are filled only by :mod:`qal.paper.execution` against stored market data. A test
(``tests/test_paper.py::test_isolated_from_real_execution``) fails if any module in the package imports a
network, broker or exchange library.
"""

PAPER_ONLY = True
