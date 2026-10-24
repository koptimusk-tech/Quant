"""Common strategy contract for portfolio construction, eligibility rules and the strategy catalog.

A *strategy* here is a validated hypothesis seen as a return stream: its walk-forward out-of-sample net
returns (each test window traded with the variant chosen on its own training bars), its executed exposures
per instrument, its cost model and capacity. Anomalies and hypotheses that did not pass validation are not
strategies; they appear in the catalog only as rejected, with the reasons.

When no real strategy is eligible the engine can be exercised with **synthetic** strategies. They are built
from a seeded factor model, carry ``is_synthetic=True`` and the ``SYN:`` id prefix, and say so in every
result. They are never evidence about real markets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.models import Dataset, Experiment, Hypothesis
from qal.research.contract import HypothesisContract
from qal.versioning import stable_hash

ELIGIBLE_STATUSES = ("VALIDATION_PASSED", "PAPER_TRADING_ELIGIBLE")
SERIES_VERSION = "1"


class EligibilityCriteria(BaseModel):
    """Configured rules a strategy must meet before a portfolio may include it."""

    allowed_statuses: list[str] = Field(default_factory=lambda: list(ELIGIBLE_STATUSES))
    require_gates_passed: bool = True
    min_oos_bars: int = Field(250, ge=20, description="Walk-forward out-of-sample bars")
    min_oos_sharpe: float = Field(0.0, description="Net OOS Sharpe floor on top of the validation gates")
    max_oos_drawdown: float = Field(0.35, gt=0, le=1)
    max_capacity_breach_frac: float = Field(0.05, ge=0, le=1)
    exclude_quality: list[str] = Field(default_factory=lambda: ["fail"])
    allow_synthetic: bool = Field(False, description="Synthetic strategies only exercise the engine")


@dataclass
class StrategyRecord:
    id: str
    version: int
    name: str
    source: str  # hypothesis | synthetic
    universe: list[str]
    instruments: list[dict[str, Any]]
    frequency: str
    capital: float
    costs: dict[str, dict[str, Any]]
    liquidity: dict[str, Any]
    data_requirements: list[str]
    benchmark: str
    validation: dict[str, Any]
    limitations: list[str]
    approval_status: str
    is_synthetic: bool
    hypothesis_id: str | None = None
    family_id: str | None = None
    group: str = ""  # market / asset class used for group limits
    counterparty: str = "unassigned"
    data_quality: dict[str, str] = field(default_factory=dict)
    risk_budget: float | None = None
    eligible: bool = False
    reasons: list[str] = field(default_factory=list)
    history: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class StrategySeries:
    """Daily series of one strategy. Index: UTC calendar date of the bar's availability.

    ``net``/``gross``/``cost`` are fractions of the strategy's capital, compounded within the day.
    ``exposure`` holds the executed position per instrument at the end of the day (fraction of capital).
    ``cost_bps`` is the per-side trading cost (bps of notional) of each instrument, used to charge
    portfolio rebalancing.
    """

    id: str
    net: pd.Series
    gross: pd.Series
    cost: pd.Series
    exposure: pd.DataFrame
    cost_bps: dict[str, float]
    asset_class: dict[str, str]
    dollar_volume: dict[str, float | None]
    is_synthetic: bool


# -- catalog ----------------------------------------------------------------------------------------
def _latest_validation(session: Session, hyp_id: str) -> Experiment | None:
    return session.scalar(select(Experiment).where(Experiment.hypothesis_id == hyp_id,
                                                   Experiment.kind == "validation",
                                                   Experiment.status == "succeeded")
                          .order_by(Experiment.created_at.desc()).limit(1))


def record_from_hypothesis(session: Session, hyp: Hypothesis) -> StrategyRecord:
    c = HypothesisContract.model_validate(hyp.contract)
    exp = _latest_validation(session, hyp.id)
    res = (exp.results or {}) if exp else {}
    instruments, quality, costs = [], {}, {}
    synthetic = False
    freq, group = "", ""
    from qal.research.costs import default_cost_model

    for ds_id in c.dataset_ids:
        ds = session.get(Dataset, ds_id)
        if ds is None:
            continue
        sym = ds.instrument.symbol
        instruments.append({"symbol": sym, "dataset_id": ds.id, "asset_class": ds.instrument.asset_class,
                            "market": ds.instrument.market, "content_hash": ds.content_hash})
        quality[sym] = ds.quality_status
        costs[sym] = (c.costs.get(ds_id) or default_cost_model(ds.instrument.asset_class)).model_dump()
        synthetic |= bool(ds.is_synthetic)
        freq = ds.frequency
        group = ds.instrument.asset_class
    oos = (res.get("oos") or {})
    metrics = oos.get("metrics") or {}
    gates = res.get("gates") or []
    eq = oos.get("equity") or []
    return StrategyRecord(
        id=f"H:{hyp.id}", version=hyp.version, name=hyp.title, source="hypothesis",
        universe=[i["symbol"] for i in instruments], instruments=instruments, frequency=freq, capital=c.capital,
        costs=costs,
        liquidity={"max_participation": {k: v["max_participation"] for k, v in costs.items()},
                   "capacity_breaches_oos": oos.get("capacity_breaches")},
        data_requirements=list(c.data_requirements) or [f"{len(instruments)} dataset(s) at {freq}"],
        benchmark=c.benchmark,
        validation={"experiment_id": exp.id if exp else None, "status": hyp.status,
                    "oos_metrics": {k: metrics.get(k) for k in ("bars", "sharpe_net", "mean_net", "max_drawdown",
                                                                "volatility_ann", "n_trades", "total_cost")},
                    "gates": gates, "decision": res.get("decision"),
                    "final_test_accesses": hyp.final_test_accesses},
        limitations=list(c.limitations), approval_status=hyp.status, is_synthetic=synthetic,
        hypothesis_id=hyp.id, family_id=hyp.family_id, group=group, data_quality=quality,
        history={"start": eq[0]["ts"] if eq else None, "end": eq[-1]["ts"] if eq else None,
                 "oos_bars": metrics.get("bars")},
    )


def check_eligibility(rec: StrategyRecord, crit: EligibilityCriteria) -> StrategyRecord:
    """Fill ``eligible`` and ``reasons``. Every unmet rule is listed (not only the first)."""
    reasons = []
    if rec.is_synthetic and not crit.allow_synthetic:
        reasons.append("synthetic data: allowed only when allow_synthetic is set (engine testing, not evidence)")
    if rec.source == "hypothesis":
        if rec.approval_status not in crit.allowed_statuses:
            reasons.append(f"status {rec.approval_status} is not one of {crit.allowed_statuses}")
        gates = rec.validation.get("gates") or []
        if crit.require_gates_passed and (not gates or not all(g.get("passed") for g in gates)):
            failed = [g["gate"] for g in gates if not g.get("passed")]
            reasons.append("validation gates not passed" + (f": {','.join(failed)}" if failed else " (no gates recorded)"))
    m = rec.validation.get("oos_metrics") or {}
    bars = m.get("bars") or 0
    if bars < crit.min_oos_bars:
        reasons.append(f"{bars} out-of-sample bars < {crit.min_oos_bars}")
    sr = m.get("sharpe_net")
    if sr is None or sr < crit.min_oos_sharpe:
        reasons.append(f"OOS net Sharpe {sr} < {crit.min_oos_sharpe}")
    dd = m.get("max_drawdown")
    if dd is None or abs(dd) > crit.max_oos_drawdown:
        reasons.append(f"OOS max drawdown {dd} beyond {crit.max_oos_drawdown}")
    breaches = rec.liquidity.get("capacity_breaches_oos")
    if bars and breaches is not None and breaches / bars > crit.max_capacity_breach_frac:
        reasons.append(f"capacity breaches {breaches}/{bars} bars > {crit.max_capacity_breach_frac:.0%}")
    bad = sorted(s for s, q in rec.data_quality.items() if q in crit.exclude_quality)
    if bad:
        reasons.append(f"data quality excluded for {bad}")
    rec.eligible, rec.reasons = not reasons, reasons
    return rec


def catalog(session: Session, crit: EligibilityCriteria, *, include_synthetic: bool = False,
            synthetic_seed: int = 7) -> list[StrategyRecord]:
    """Every validated (or attempted) hypothesis as a strategy record, eligible or not, plus synthetic ones."""
    hyps = session.scalars(select(Hypothesis).order_by(Hypothesis.created_at)).all()
    out = []
    for h in hyps:
        if _latest_validation(session, h.id) is None:
            continue  # never validated: a hypothesis, not a strategy
        out.append(check_eligibility(record_from_hypothesis(session, h), crit))
    if include_synthetic:
        out.extend(check_eligibility(r, crit) for r in synthetic_strategies(seed=synthetic_seed)[0])
    return out


# -- return series ----------------------------------------------------------------------------------
def _daily(oos: pd.DataFrame, pos: pd.DataFrame, names: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    day = pd.to_datetime(oos["available_ts"], utc=True).dt.normalize()
    g = pd.DataFrame({"net": oos["net"].to_numpy(), "gross": oos["gross"].to_numpy(),
                      "cost": oos["cost"].to_numpy(), "day": day.to_numpy()})
    agg = g.groupby("day").agg(net=("net", lambda x: np.prod(1 + x) - 1), gross=("gross", lambda x: np.prod(1 + x) - 1),
                               cost=("cost", "sum"))
    e = pos[names].copy()
    e["day"] = day.to_numpy()
    exp = e.groupby("day").last()
    return agg, exp


def series_for(session: Session, rec: StrategyRecord, data_dir: Path) -> StrategySeries:
    """Walk-forward OOS daily series of a hypothesis strategy, cached by contract and data hashes."""
    from qal.research.service import _validator

    hyp = session.get(Hypothesis, rec.hypothesis_id)
    key = stable_hash({"v": SERIES_VERSION, "contract": hyp.contract_hash,
                       "data": [i["content_hash"] for i in rec.instruments]})[:24]
    cache = Path(data_dir) / "portfolio_series" / f"{key}.parquet"
    names = rec.universe
    if cache.exists():
        df = pd.read_parquet(cache)
    else:
        validator, _, _ = _validator(session, hyp)
        wf = validator.walk_forward()
        agg, exp = _daily(wf["oos"], wf["oos_pos"], names)
        df = agg.join(exp)
        cache.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cache)
    df.index = pd.to_datetime(df.index, utc=True)
    dvol: dict[str, float | None] = {}
    for inst in rec.instruments:
        ds = session.get(Dataset, inst["dataset_id"])
        from qal.data.store import ParquetStore

        f = ParquetStore.read(ds.path)
        v = (f["close"] * f["volume"]).tail(250)
        dvol[inst["symbol"]] = float(v.median()) if v.notna().any() and v.median() > 0 else None
    cost_bps = {s: c["commission_bps"] + c["half_spread_bps"] + c["slippage_bps"] for s, c in rec.costs.items()}
    return StrategySeries(rec.id, df["net"], df["gross"], df["cost"], df[names], cost_bps,
                          {i["symbol"]: i["asset_class"] for i in rec.instruments}, dvol, rec.is_synthetic)


# -- synthetic strategies ---------------------------------------------------------------------------
SYN_NOTE = ("SYNTHETIC: generated from a seeded factor model to exercise the portfolio engine. "
            "Not evidence about any market; never present these numbers as results.")

_SYN_SPECS = [
    # id, name, instruments (symbol, weight sign), factor loadings (trend, carry, crash), idio vol, mean, start
    ("SYN:trend-eur", "SYN trend EUR", [("SYN-EURUSD", 1.0)], (1.0, 0.0, 0.0), 0.0020, 0.00035, 0),
    ("SYN:trend-gbp", "SYN trend GBP (redundant with EUR)", [("SYN-GBPUSD", 1.0)], (0.95, 0.0, 0.0), 0.0010, 0.00030, 0),
    ("SYN:carry", "SYN carry basket", [("SYN-AUDJPY", 1.0), ("SYN-USDCHF", -0.5)], (0.0, 1.0, 0.5), 0.0025, 0.00030, 0),
    ("SYN:meanrev", "SYN mean reversion", [("SYN-EURGBP", -1.0)], (-0.2, 0.0, 0.0), 0.0030, 0.00025, 0),
    ("SYN:vol-short", "SYN short volatility", [("SYN-USDJPY", -0.8)], (0.0, 0.3, 0.9), 0.0015, 0.00035, 0),
    ("SYN:late", "SYN late starter (partial history)", [("SYN-USDCAD", 1.0)], (0.2, 0.2, 0.0), 0.0025, 0.00030, 900),
    ("SYN:weak", "SYN weak (fails eligibility)", [("SYN-NZDUSD", 1.0)], (0.1, 0.0, 0.0), 0.0040, -0.00010, 0),
]


def synthetic_strategies(seed: int = 7, n_days: int = 2000, stress: tuple[int, int] = (1400, 1460)
                         ) -> tuple[list[StrategyRecord], dict[str, StrategySeries]]:
    """Seven labelled synthetic strategies with known structure.

    * ``trend-eur`` and ``trend-gbp`` load on the same factor (correlation about 0.9): redundant.
    * ``vol-short`` and ``carry`` load on a crash factor that is quiet except in the stress segment, where
      its volatility jumps and correlations rise sharply (diversification disappears).
    * ``late`` has data only from day 900 (incomplete history).
    * ``weak`` loses money after costs, so the default eligibility rules reject it.
    """
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2015-01-01", periods=n_days, tz="UTC")
    s0, s1 = stress
    trend = rng.standard_t(5, n_days) * 0.0025
    carry = rng.standard_t(5, n_days) * 0.002
    crash = rng.normal(0, 0.0005, n_days)
    crash[s0:s1] = rng.normal(-0.003, 0.010, s1 - s0)
    trend[s0:s1] *= 2.5
    recs, series = [], {}
    for sid, name, legs, (bt, bc, bx), ivol, mu, start in _SYN_SPECS:
        idio = rng.standard_t(5, n_days) * ivol
        gross = mu + bt * trend + bc * carry + bx * crash + idio
        turnover = np.abs(rng.normal(0, 0.15, n_days))
        cost = turnover * 1.5e-4
        net = gross - cost
        mask = np.arange(n_days) >= start
        idx = days[mask]
        sgn = np.sign(pd.Series(trend if bt else carry).rolling(20, min_periods=1).mean().shift(1).fillna(1).to_numpy())
        sgn[sgn == 0] = 1
        expo = pd.DataFrame({sym: (w * (sgn if sid.startswith("SYN:trend") else np.ones(n_days)))[mask] for sym, w in legs},
                            index=idx)
        syms = [s for s, _ in legs]
        costs = {s: {"commission_bps": 0.3, "half_spread_bps": 0.7, "slippage_bps": 0.5, "max_participation": 0.05}
                 for s in syms}
        r = pd.Series(net[mask], index=idx)
        eq = np.cumprod(1 + r.to_numpy())
        dd = float((eq / np.maximum.accumulate(eq) - 1).min())
        recs.append(StrategyRecord(
            id=sid, version=1, name=name, source="synthetic", universe=syms,
            instruments=[{"symbol": s, "asset_class": "fx", "market": "SYN-OTC", "dataset_id": None} for s in syms],
            frequency="1d", capital=1_000_000.0, costs=costs,
            liquidity={"max_participation": {s: 0.05 for s in syms}, "capacity_breaches_oos": 0},
            data_requirements=["synthetic factor model"], benchmark="cash",
            validation={"status": "SYNTHETIC", "experiment_id": None, "gates": [],
                        "oos_metrics": {"bars": int(mask.sum()),
                                        "sharpe_net": float(r.mean() / r.std(ddof=1) * np.sqrt(252)),
                                        "mean_net": float(r.mean()), "max_drawdown": dd,
                                        "volatility_ann": float(r.std(ddof=1) * np.sqrt(252))}},
            limitations=[SYN_NOTE], approval_status="SYNTHETIC", is_synthetic=True, group="fx",
            counterparty="SYN-BROKER-A" if sid != "SYN:carry" else "SYN-BROKER-B",
            data_quality={s: "pass" for s in syms},
            history={"start": str(idx[0]), "end": str(idx[-1]), "oos_bars": int(mask.sum())}))
        series[sid] = StrategySeries(sid, r, pd.Series(gross[mask], index=idx), pd.Series(cost[mask], index=idx),
                                     expo, {s: 1.5 for s in syms}, {s: "fx" for s in syms},
                                     {s: None for s in syms}, True)
    return recs, series
