"""Position sizing: from target exposures (fractions of capital) to tradable quantities.

Methods: fixed fraction, volatility target, risk budget and liquidity adjustment, all using data available up
to the decision bar (volatility from returns that ended *before* the decision). Quantities respect the
instrument's contract: multiplier, lot size, minimum lot, tick size and maximum leverage. Contract details
come from ``Instrument.metadata_json["contract"]`` when present; otherwise conservative asset-class
assumptions are used and labelled as such.

A stop-loss limits the *intended* loss, not the realised one: prices can gap through a stop, so the sizing
report shows the loss at the stop and the loss if the price gaps by the largest historical gap.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

SIZING_METHODS = ("fixed", "vol_target", "risk_budget", "liquidity")


class ContractSpec(BaseModel):
    multiplier: float = Field(1.0, gt=0, description="Units of the underlying per contract or lot unit")
    lot_size: float = Field(1.0, gt=0, description="Quantity step")
    min_lot: float = Field(1.0, gt=0)
    tick_size: float = Field(0.01, gt=0)
    max_leverage: float = Field(1.0, gt=0)
    source: str = "assumed"


def default_contract(asset_class: str, symbol: str = "") -> ContractSpec:
    if asset_class == "fx":
        jpy = "JPY" in symbol.upper()
        # 1 lot = 100,000 units of base currency; brokers allow 0.01 lots. Retail leverage caps vary (ESMA 30:1).
        return ContractSpec(multiplier=100_000, lot_size=0.01, min_lot=0.01, tick_size=0.001 if jpy else 0.00001,
                            max_leverage=30.0)
    if asset_class == "crypto":
        return ContractSpec(multiplier=1, lot_size=1e-5, min_lot=1e-5, tick_size=0.01, max_leverage=2.0)
    if asset_class == "future":
        return ContractSpec(multiplier=50, lot_size=1, min_lot=1, tick_size=0.25, max_leverage=10.0)
    return ContractSpec(multiplier=1, lot_size=1, min_lot=1, tick_size=0.01, max_leverage=1.0)


def contract_for(metadata: dict[str, Any] | None, asset_class: str, symbol: str) -> ContractSpec:
    spec = (metadata or {}).get("contract")
    if spec:
        return ContractSpec(**{**spec, "source": "instrument metadata"})
    return default_contract(asset_class, symbol)


def trailing_vol(returns: pd.Series, as_of: pd.Timestamp, window: int = 60, ppy: float = 252.0) -> float | None:
    """Annualised volatility of returns strictly before ``as_of`` (no information from the decision bar on)."""
    r = returns[returns.index < as_of].dropna().tail(window)
    if len(r) < max(10, window // 3):
        return None
    return float(r.std(ddof=1) * math.sqrt(ppy))


def target_exposure(method: str, *, capital: float, fraction: float = 0.1, vol: float | None = None,
                    target_vol: float = 0.1, budget: float | None = None, portfolio_vol: float | None = None,
                    dollar_volume: float | None = None, max_participation: float = 0.05,
                    base: float | None = None) -> dict[str, Any]:
    """Exposure as a fraction of capital for one position, with the reason for its size."""
    if method == "fixed":
        return {"exposure": fraction, "basis": f"fixed {fraction:.2%} of capital"}
    if method == "vol_target":
        if not vol:
            return {"exposure": 0.0, "basis": "no trailing volatility available: not sized"}
        return {"exposure": target_vol / vol, "basis": f"target {target_vol:.1%} / trailing vol {vol:.1%}"}
    if method == "risk_budget":
        if not vol or budget is None or not portfolio_vol:
            return {"exposure": 0.0, "basis": "missing budget or volatility"}
        return {"exposure": budget * portfolio_vol / vol,
                "basis": f"budget {budget:.1%} of portfolio vol {portfolio_vol:.1%} / position vol {vol:.1%}"}
    if method == "liquidity":
        e = base if base is not None else fraction
        if not dollar_volume:
            return {"exposure": e, "basis": "no usable traded volume: liquidity cap not evaluable"}
        cap = max_participation * dollar_volume / capital
        return {"exposure": min(e, cap), "basis": f"min(requested, {max_participation:.0%} of median dollar volume)"}
    raise ValueError(f"unknown sizing method {method!r}; use one of {SIZING_METHODS}")


def round_price(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 10)


def to_quantity(exposure: float, *, capital: float, price: float, spec: ContractSpec) -> dict[str, Any]:
    """Round a target exposure to whole lots (towards zero) and report the residual and the leverage check."""
    if price <= 0 or not np.isfinite(price):
        raise ValueError("price must be positive")
    notional = exposure * capital
    raw_lots = notional / (price * spec.multiplier)
    lots = math.copysign(math.floor(abs(raw_lots) / spec.lot_size + 1e-9) * spec.lot_size, raw_lots)
    below_min = 0 < abs(lots) < spec.min_lot or (abs(raw_lots) > 0 and lots == 0)
    if below_min:
        lots = 0.0
    lots = round(lots, 10)
    actual = lots * price * spec.multiplier
    lev_ok = abs(actual) <= spec.max_leverage * capital + 1e-6
    return {"target_notional": notional, "lots": lots, "units": lots * spec.multiplier, "notional": actual,
            "exposure": actual / capital, "rounding_residual": notional - actual, "below_min_lot": below_min,
            "price": round_price(price, spec.tick_size), "leverage_ok": lev_ok, "contract": spec.model_dump()}


def stop_loss_risk(exposure: float, *, capital: float, stop_distance: float, returns: pd.Series) -> dict[str, Any]:
    """Loss at the stop vs. loss if the price gaps through it by the largest adverse historical one-bar move.

    ``stop_distance`` is a fraction of price. The worst gap is taken from the returns passed in (history up
    to the decision), so it is a lower bound for future gaps, not a maximum.
    """
    r = returns.dropna()
    worst = float(-r.min()) if exposure > 0 else float(r.max()) if len(r) else 0.0
    worst = max(worst, 0.0)
    at_stop = abs(exposure) * stop_distance * capital
    gap = abs(exposure) * max(stop_distance, worst) * capital
    return {"loss_at_stop": at_stop, "loss_if_gap": gap, "worst_historical_bar_move": worst,
            "note": "A stop-loss does not guarantee a maximum loss: gaps, slippage and halted markets can exceed it."}
