"""Simulated orders: types, fields and the state machine.

States: CREATED -> PENDING | REJECTED; PENDING -> PARTIALLY_FILLED | FILLED | CANCELLED | EXPIRED;
PARTIALLY_FILLED -> PARTIALLY_FILLED | FILLED | CANCELLED | EXPIRED. FILLED, CANCELLED, REJECTED and EXPIRED
are final. Every transition is checked here; an illegal one raises instead of silently changing state.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from qal.models import PaperOrder
from qal.versioning import stable_hash

ORDER_TYPES = ("market", "limit", "stop", "stop_limit")
STATES = ("CREATED", "PENDING", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "EXPIRED")
OPEN_STATES = ("PENDING", "PARTIALLY_FILLED")
FINAL_STATES = ("FILLED", "CANCELLED", "REJECTED", "EXPIRED")

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "CREATED": ("PENDING", "REJECTED"),
    "PENDING": ("PARTIALLY_FILLED", "FILLED", "CANCELLED", "EXPIRED"),
    "PARTIALLY_FILLED": ("PARTIALLY_FILLED", "FILLED", "CANCELLED", "EXPIRED"),
}


class OrderStateError(ValueError):
    pass


class OrderIn(BaseModel):
    """A manual simulated order. It goes through the same risk gate as strategy orders."""

    strategy_id: str = Field("MANUAL", max_length=64)
    instrument: str
    side: str = Field(pattern="^(buy|sell)$")
    lots: float = Field(gt=0)
    order_type: str = Field("market", pattern="^(market|limit|stop|stop_limit)$")
    limit_price: float | None = Field(None, gt=0)
    stop_price: float | None = Field(None, gt=0)
    tif_bars: int | None = Field(5, ge=1, description="Bars the order stays active; null = good till cancelled")

    @model_validator(mode="after")
    def _prices(self) -> OrderIn:
        if self.order_type in ("limit", "stop_limit") and self.limit_price is None:
            raise ValueError(f"{self.order_type} needs limit_price")
        if self.order_type in ("stop", "stop_limit") and self.stop_price is None:
            raise ValueError(f"{self.order_type} needs stop_price")
        return self


def transition(order: PaperOrder, new: str, reason: str | None = None) -> None:
    if new not in TRANSITIONS.get(order.status, ()):
        raise OrderStateError(f"order {order.client_order_id}: {order.status} -> {new} is not allowed")
    order.status = new
    if new == "REJECTED":
        order.reject_reason = reason
    elif new in ("CANCELLED", "EXPIRED"):
        order.cancel_reason = reason


def client_order_id(session_id: str, strategy_id: str, instrument: str, decided_at: str, purpose: str,
                    seq: int = 0) -> str:
    """Deterministic idempotency key: re-processing the same bar cannot create a second order."""
    return "po-" + stable_hash([session_id, strategy_id, instrument, decided_at, purpose, seq])[:40]


def remaining(order: PaperOrder) -> float:
    return max(order.quantity - order.filled_quantity, 0.0)


def direction(side: str) -> int:
    return 1 if side == "buy" else -1
