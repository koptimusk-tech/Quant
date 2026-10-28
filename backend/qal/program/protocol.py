"""Evaluation protocol: fixed, hashed and stored before the program starts.

A protocol belongs to one candidate version and one mode. It fixes, before any bar of the evaluation period is
seen: the horizon (minimum observations and trades, maximum duration), the primary and secondary metrics, the
baseline, the cost, drawdown and exposure thresholds, the pause and completion conditions, the comparison method
and the handling of missing data. It is stored as an ``Experiment`` of kind ``paper_protocol`` and never edited:
a different protocol is a new record, and a program keeps the protocol it started with.

The horizon is not a universal fixed length. It is derived from the candidate itself:

* ``bars_for_trades = ceil(min_trades / trade_rate)``, with the trade rate of the candidate's out-of-sample
  walk-forward (trades per bar);
* ``bars_for_precision = ceil(ppy * (1 + SR_bar**2 / 2) / target_sharpe_se**2)``: bars needed for the standard
  error of the annualised Sharpe ratio (Lo, 2002, i.i.d. approximation) to reach ``target_sharpe_se``;
* ``horizon = max(min_observations_floor, bars_for_trades, bars_for_precision)``.

The decision is taken at the horizon (extended up to ``max_duration_bars`` only when the trades are still short,
a rule also fixed here). Interim looks can pause or stop the program for safety and integrity reasons; they never
declare success early.

Modes, and the label every result carries:

* ``A`` HISTORICAL_REPLAY - past bars processed one at a time. Not prospective. When the replay period was used by
  the research (in-sample or evaluated holdout) the label says so: it checks the operation, not the hypothesis.
* ``B`` PROSPECTIVE_PAPER - new data only, real (non-synthetic) datasets. A bar counts as prospective only if it
  closed after the protocol was frozen. Delays, gaps and data quality are logged per bar.
* ``C`` SYNTHETIC_SIMULATION - synthetic data. Tests the procedure, never evidence about markets.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from qal import audit
from qal.data.schema import bar_delta
from qal.data.store import ParquetStore
from qal.models import Dataset, Experiment
from qal.program import registry
from qal.research.costs import periods_per_year
from qal.versioning import code_version, stable_hash

KIND = "paper_protocol"
PROTOCOL_VERSION = "paper-protocol@1.0.0"
MODES = {"A": "HISTORICAL_REPLAY", "B": "PROSPECTIVE_PAPER", "C": "SYNTHETIC_SIMULATION"}
MODE_NOTES = {
    "A": "Historical replay: past bars processed one at a time. It is not prospective and not real-time trading.",
    "B": "Prospective paper trading on new data, simulated: no order reaches any broker. Only bars that closed after "
         "the protocol was frozen count as prospective.",
    "C": "Synthetic simulation: tests the procedure only; it is not evidence about any market.",
}
PRIMARY_METRIC = ("mean net return per bar in excess of the baseline, with a 95% moving-block bootstrap confidence "
                  "interval")
SECONDARY_METRICS = ("annualised Sharpe with its standard error", "maximum drawdown", "annualised volatility",
                     "hit rate per bar", "trades", "turnover (filled orders)", "costs paper / backtest",
                     "tracking difference and correlation with the same-bar backtest", "fill rate",
                     "implementation shortfall", "gross exposure")
COMPARISON_METHOD = ("Same-bar comparison: the backtest of the same candidate version (same parameters, universe, "
                     "costs and conventions) is run on exactly the bars the paper program processed. Differences per "
                     "bar are summarised with a moving-block bootstrap; a difference that cannot be explained by a "
                     "measured component (costs) is logged as unreconciled, without asserting a cause.")
MISSING_DATA = ("A bar that fails the data checks pauses the program before it is used. A person may skip it "
                "(logged); skipped and missing bars are counted and the share of missing bars is a completion "
                "condition. Missing bars are never filled with synthetic values.")
PAUSE_CAUSES = {
    "stale_or_corrupt_data": "a bar failed the data checks, or the live feed is stale beyond the protocol limit",
    "risk_limit_breach": "a one-bar loss beyond the candidate limit, or orders rejected by the risk gate repeatedly",
    "drawdown": "drawdown beyond the candidate's pause level",
    "drawdown_stop": "drawdown beyond the candidate's stop level (a reject criterion; cannot be resumed)",
    "exposure": "gross exposure above the candidate limit",
    "anomalous_costs": "a fill whose cost is above the protocol's anomalous-cost threshold",
    "engine_errors": "repeated engine errors in consecutive steps",
    "inconsistent_orders_positions_pnl": "orders, fills, positions, cash and P&L do not reconcile",
    "persistence_failure": "what was stored does not match what was processed (rows missing after a commit)",
    "kill_switch": "the kill switch was activated by a person",
    "manual_pause": "a person paused the session",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class ProtocolError(ValueError):
    pass


class ProtocolIn(BaseModel):
    candidate_id: str
    mode: Literal["A", "B", "C"]
    created_by: str = Field(min_length=2, max_length=64)
    rationale: str = Field(min_length=20, max_length=4000, description="Why these settings, written before the run")
    replay_start: datetime | None = Field(None, description="A/C: first bar; default the reserved final period")
    replay_end: datetime | None = None
    min_trades: int = Field(20, ge=3, le=10_000)
    min_observations_floor: int = Field(60, ge=20)
    target_sharpe_se: float = Field(1.0, ge=0.1, le=5, description="Annualised Sharpe standard error to reach")
    max_duration_bars: int = Field(1500, ge=30, le=100_000)
    baseline: Literal["no_trade", "buy_and_hold"] = "no_trade"
    rolling_window: int = Field(63, ge=10, le=2000)
    tracking_tolerance_bps: float = Field(2.0, gt=0, description="|mean paper - backtest| per bar, in bps")
    min_return_correlation: float = Field(0.8, ge=-1, le=1)
    min_signal_agreement: float = Field(0.95, ge=0, le=1)
    min_fill_rate: float = Field(0.8, ge=0, le=1)
    max_cost_ratio: float = Field(1.5, gt=0, description="Accept: paper costs / backtest costs at most this")
    reject_cost_ratio: float = Field(3.0, gt=0, description="Reject: paper costs / backtest costs above this")
    max_drawdown_ratio: float = Field(1.5, gt=0, description="Accept: paper drawdown / backtest drawdown")
    anomalous_cost_bps: float = Field(100.0, gt=0, description="Pause: a fill costing more than this")
    max_missing_frac: float = Field(0.05, ge=0, le=1)
    max_consecutive_errors: int = Field(3, ge=1, le=100)
    max_rejected_bars: int = Field(3, ge=1, le=100, description="Pause: bars in a row with risk-gate rejections")
    max_staleness_hours: float = Field(96.0, gt=0, description="B: no new bar for longer pauses the program")

    @model_validator(mode="after")
    def _check(self) -> ProtocolIn:
        if self.reject_cost_ratio <= self.max_cost_ratio:
            raise ValueError("reject_cost_ratio must be above max_cost_ratio")
        if self.min_observations_floor > self.max_duration_bars:
            raise ValueError("min_observations_floor above max_duration_bars")
        if self.replay_start and self.replay_end and self.replay_end <= self.replay_start:
            raise ValueError("replay_end must be after replay_start")
        if self.mode == "B" and (self.replay_start or self.replay_end):
            raise ValueError("mode B trades new data only: no replay period")
        return self


def plan_horizon(cand: dict[str, Any], p: ProtocolIn) -> dict[str, Any]:
    oos = cand["results"]["oos"]
    bars, trades = oos.get("bars") or 0, oos.get("n_trades") or 0
    ppy = periods_per_year(cand["timeframe"], cand["assets"][0]["asset_class"])
    if not bars or not trades:
        raise ProtocolError("the candidate has no out-of-sample trades: its trade frequency is unknown, so no horizon "
                            "can be planned")
    rate = trades / bars
    sr_bar = (oos.get("sharpe_net") or 0.0) / math.sqrt(ppy)
    b_trades = math.ceil(p.min_trades / rate)
    b_prec = math.ceil(ppy * (1 + sr_bar ** 2 / 2) / p.target_sharpe_se ** 2)
    horizon = max(p.min_observations_floor, b_trades, b_prec)
    plan = {"trade_rate_per_bar": rate, "oos_bars": bars, "oos_trades": trades, "periods_per_year": ppy,
            "oos_sharpe_net": oos.get("sharpe_net"), "bars_for_trades": b_trades, "bars_for_precision": b_prec,
            "floor": p.min_observations_floor, "horizon_bars": horizon, "max_duration_bars": p.max_duration_bars,
            "extension_rule": "if trades < min_trades at the horizon, continue until they are reached or "
                              "max_duration_bars, whichever comes first; the decision is never brought forward",
            "formula": "max(floor, ceil(min_trades / trade_rate), ceil(ppy * (1 + SR_bar^2 / 2) / target_se^2))"}
    if horizon > p.max_duration_bars:
        raise ProtocolError(f"the planned horizon is {horizon} bars ({b_trades} for the trades, {b_prec} for the "
                            f"precision) and exceeds max_duration_bars {p.max_duration_bars}: raise the maximum or "
                            "accept a lower precision explicitly")
    return plan


def _replay_window(s: Session, cand: dict[str, Any], p: ProtocolIn) -> dict[str, Any]:
    first = s.get(Dataset, cand["assets"][0]["dataset_id"])
    df = ParquetStore.read(first.path)
    ts = pd.to_datetime(df["ts"], utc=True)
    res = cand["results"]
    hold = pd.Timestamp(res["holdout_start"]) if res.get("holdout_start") else None
    start = pd.Timestamp(p.replay_start, tz="UTC") if p.replay_start and p.replay_start.tzinfo is None else (
        pd.Timestamp(p.replay_start) if p.replay_start else hold)
    if start is None:
        raise ProtocolError("the candidate has no reserved final period: give replay_start")
    end = pd.Timestamp(p.replay_end, tz="UTC") if p.replay_end and p.replay_end.tzinfo is None else (
        pd.Timestamp(p.replay_end) if p.replay_end else ts.iloc[-1])
    n = int(((ts >= start) & (ts <= end)).sum())
    untouched = hold is not None and start >= hold and not res.get("holdout_evaluated")
    return {"start": str(start), "end": str(end), "bars_available": n,
            "overlaps_research": not untouched,
            "note": "the reserved final period, never used to select the parameters" if untouched else
            "this period was used by the research (in-sample or an evaluated holdout): the replay checks the "
            "operation, not the hypothesis"}


def create(s: Session, p: ProtocolIn) -> Experiment:
    rec, body = build(s, p)
    e = Experiment(kind=KIND, status="frozen", algorithm_version=PROTOCOL_VERSION, code_version=code_version(),
                   config=body, config_hash=stable_hash(body), data_refs=list(rec.data_refs),
                   results={"candidate_id": rec.id}, created_at=pd.Timestamp(body["frozen_at"]).to_pydatetime())
    s.add(e)
    s.flush()
    audit.record(s, p.created_by, "paper_protocol_frozen", "experiment", e.id, candidate=rec.id, mode=p.mode)
    return e


def build(s: Session, p: ProtocolIn) -> tuple[Experiment, dict[str, Any]]:
    """Validate a protocol and compute its horizon (also used to preview it without storing anything)."""
    if registry.is_agent(p.created_by):
        raise ProtocolError("agents and automated processes cannot define a protocol")
    rec = registry.get(s, p.candidate_id)
    if not registry.verify(rec):
        raise ProtocolError("the candidate record does not match its hash: it cannot be used")
    cand = rec.config
    if p.mode not in cand["allowed_modes"]:
        raise ProtocolError(f"mode {p.mode} ({MODES[p.mode]}) is not allowed for this candidate "
                            f"({'synthetic' if cand['is_synthetic'] else 'real'} data allows {cand['allowed_modes']})")
    horizon = plan_horizon(cand, p)
    window = None
    if p.mode in ("A", "C"):
        window = _replay_window(s, cand, p)
        if window["bars_available"] < horizon["horizon_bars"]:
            raise ProtocolError(f"the replay period has {window['bars_available']} bars and the protocol needs "
                                f"{horizon['horizon_bars']}: the result could only be inconclusive")
    else:
        step = bar_delta(cand["timeframe"])
        days = horizon["horizon_bars"] * step / pd.Timedelta(days=1)
        if cand["assets"][0]["asset_class"] != "crypto" and cand["timeframe"] in ("1d",):
            days *= 7 / 5
        horizon["expected_calendar_days"] = round(days, 1)
    frozen = utcnow()
    body = {"protocol_version": PROTOCOL_VERSION, **p.model_dump(mode="json"), "candidate_hash": rec.config_hash,
            "candidate_name": cand["name"], "candidate_version": cand["version"],
            "admission_kind": cand["admission"]["kind"], "mode_label": MODES[p.mode], "mode_note": MODE_NOTES[p.mode],
            "horizon": horizon, "replay": window, "risk_limits": cand["risk_limits"],
            "primary_metric": PRIMARY_METRIC, "secondary_metrics": list(SECONDARY_METRICS),
            "comparison_method": COMPARISON_METHOD, "missing_data": MISSING_DATA,
            "pause_causes": PAUSE_CAUSES, "frozen_at": frozen.isoformat(),
            "acceptance": acceptance_text(p, cand), "rejection": rejection_text(p, cand)}
    return rec, body


def acceptance_text(p: ProtocolIn, cand: dict[str, Any]) -> list[str]:
    return [f"sample complete: at least the planned horizon and {p.min_trades} trades",
            f"primary metric: the 95% CI of the excess over {p.baseline} lies above zero",
            (f"backtest consistency: |mean paper - backtest| per bar within {p.tracking_tolerance_bps} bps (CI), "
             f"correlation >= {p.min_return_correlation}, signal agreement >= {p.min_signal_agreement}"),
            f"costs: paper / backtest <= {p.max_cost_ratio}", f"fill rate >= {p.min_fill_rate}",
            (f"drawdown: paper / backtest <= {p.max_drawdown_ratio} and never beyond the stop level "
             f"{cand['risk_limits']['drawdown_stop']:.0%}"),
            f"missing bars <= {p.max_missing_frac:.0%}", "no pause left unresolved and every reconciliation passed"]


def rejection_text(p: ProtocolIn, cand: dict[str, Any]) -> list[str]:
    return [f"drawdown beyond the stop level {cand['risk_limits']['drawdown_stop']:.0%} (any time)",
            f"with the sample complete: the 95% CI of the excess over {p.baseline} lies below zero",
            ("with the sample complete: the mean difference with the same-bar backtest lies outside the tolerance "
             "with its whole CI (the simulation does not reproduce the candidate)"),
            f"with the sample complete: paper / backtest costs above {p.reject_cost_ratio}"]


def get(s: Session, pid: str) -> Experiment:
    e = s.get(Experiment, pid)
    if e is None or e.kind != KIND:
        raise LookupError("protocol not found")
    return e


def verify(e: Experiment) -> bool:
    return e.kind == KIND and stable_hash(e.config) == e.config_hash


def frozen_at(e: Experiment) -> pd.Timestamp:
    return pd.Timestamp(e.config["frozen_at"])
