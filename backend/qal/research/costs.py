"""Transaction and holding cost model.

All numbers are *assumptions* declared in the hypothesis contract before any result is seen.
Defaults per asset class are deliberately conservative placeholders; replace them with
broker/venue-specific values (commission schedule, typical spread, funding history).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

BARS_PER_YEAR_DAILY = {"crypto": 365.0}


class CostModel(BaseModel):
    commission_bps: float = Field(1.0, ge=0, description="Per side, bps of traded notional")
    half_spread_bps: float = Field(2.0, ge=0, description="Half the quoted bid-ask spread, bps")
    slippage_bps: float = Field(2.0, ge=0, description="Fixed market impact per side, bps")
    slippage_range_frac: float = Field(0.05, ge=0, le=1,
                                       description="Extra slippage as a fraction of the previous bar's high-low range")
    borrow_bps_annual: float = Field(50.0, ge=0, description="Cost of holding short exposure, bps per year")
    financing_bps_annual: float = Field(0.0, ge=0, description="Cost of holding long exposure (margin/leverage)")
    funding_bps_daily: float = Field(0.0, ge=0,
                                     description="Perpetual futures funding paid on gross exposure, bps per day "
                                                 "(assumed paid in both directions: conservative)")
    rollover_bps_per_roll: float = Field(0.0, ge=0, description="Futures roll cost per roll")
    roll_every_bars: int | None = Field(None, ge=1)
    max_participation: float = Field(0.05, gt=0, le=1,
                                     description="Max traded notional as a fraction of the previous bar's dollar volume")

    def scaled(self, m: float) -> CostModel:
        """Every cost component multiplied by m (for cost-sensitivity analysis)."""
        d = self.model_dump()
        for k in ("commission_bps", "half_spread_bps", "slippage_bps", "slippage_range_frac", "borrow_bps_annual",
                  "financing_bps_annual", "funding_bps_daily", "rollover_bps_per_roll"):
            d[k] = d[k] * m
        d["slippage_range_frac"] = min(d["slippage_range_frac"], 1.0)
        return CostModel(**d)


DEFAULT_COSTS: dict[str, CostModel] = {
    "equity": CostModel(commission_bps=1.0, half_spread_bps=3.0, slippage_bps=2.0, borrow_bps_annual=100.0),
    "etf": CostModel(commission_bps=0.5, half_spread_bps=1.0, slippage_bps=1.0, borrow_bps_annual=50.0),
    "fx": CostModel(commission_bps=0.3, half_spread_bps=0.7, slippage_bps=0.5, borrow_bps_annual=0.0,
                    financing_bps_annual=100.0),
    "future": CostModel(commission_bps=0.5, half_spread_bps=1.0, slippage_bps=1.0, borrow_bps_annual=0.0,
                        rollover_bps_per_roll=2.0, roll_every_bars=63),
    "crypto": CostModel(commission_bps=7.5, half_spread_bps=2.0, slippage_bps=3.0, borrow_bps_annual=0.0,
                        funding_bps_daily=1.0),
    "index": CostModel(commission_bps=1.0, half_spread_bps=2.0, slippage_bps=2.0),
    "other": CostModel(),
}


def default_cost_model(asset_class: str) -> CostModel:
    return DEFAULT_COSTS.get(asset_class, CostModel())


def periods_per_year(frequency: str, asset_class: str) -> float:
    days = 365.0 if asset_class == "crypto" else 252.0
    per_day = {"1m": 1440 if asset_class == "crypto" else 390, "5m": 288 if asset_class == "crypto" else 78,
               "15m": 96 if asset_class == "crypto" else 26, "30m": 48 if asset_class == "crypto" else 13,
               "1h": 24 if asset_class == "crypto" else 6.5, "4h": 6 if asset_class == "crypto" else 1.625,
               "1d": 1, "1wk": 1 / 5 if asset_class != "crypto" else 1 / 7}[frequency]
    return days * per_day
