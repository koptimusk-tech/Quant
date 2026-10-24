"""Hierarchical Risk Manager (HRM): deterministic limits at strategy, group and portfolio level.

The HRM does not depend on any language model. It receives proposed weights (from a construction method or
an agent proposal) and returns the weights it allows plus one decision per rule checked: approved or blocked,
the rule, the observed value, the limit, the reason, the as-of timestamp and the policy version.

Policies are versioned and immutable: a change creates a new version. Only a person can create or activate a
policy; agents and the API without a named human approver are refused, and nothing an agent sends can
change a limit, skip a rule or grant an exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from qal import audit
from qal.models import RiskDecision, RiskPolicy
from qal.portfolio.risk import exposures, risk_contributions
from qal.versioning import stable_hash

ENGINE_VERSION = "hrm@1.0.0"


class StrategyLimits(BaseModel):
    max_weight: float = Field(0.35, gt=0, le=2, description="Max capital allocated to one strategy")
    max_drawdown: float = Field(0.25, gt=0, le=1,
                                description="A strategy beyond this drawdown from its own peak gets no capital")
    min_history_days: int = Field(126, ge=20, description="Days of out-of-sample history before allocating")


class GroupLimits(BaseModel):
    max_instrument_gross: float = Field(0.6, gt=0, description="Look-through |exposure| per instrument")
    max_market_gross: float = Field(2.0, gt=0, description="Look-through gross exposure per market/asset class")
    max_currency_net: float = Field(0.6, gt=0, description="Net exposure per currency (FX legs)")
    max_group_weight: float = Field(1.5, gt=0, description="Capital allocated to one strategy group")
    max_counterparty_share: float = Field(0.8, gt=0, le=1, description="Share of capital per counterparty")


class PortfolioLimits(BaseModel):
    max_leverage: float = Field(2.0, gt=0, le=10, description="Sum of strategy allocations")
    max_gross_exposure: float = Field(3.0, gt=0, description="Look-through gross instrument exposure")
    max_net_exposure: float = Field(2.0, gt=0)
    target_vol: float | None = Field(0.08, gt=0, le=1, description="Ex-ante annual volatility target (trailing data)")
    max_ex_ante_vol: float = Field(0.12, gt=0, le=1)
    max_risk_share: float = Field(0.5, gt=0, le=1, description="Max share of portfolio risk from one strategy")
    max_daily_loss: float = Field(0.03, gt=0, le=1, description="A larger one-day loss de-risks the next day")
    max_drawdown: float = Field(0.15, gt=0, le=1)
    drawdown_action: Literal["halve", "flatten"] = "halve"
    cooldown_days: int = Field(20, ge=1)
    min_capacity_multiple: float = Field(1.0, ge=0, description="Liquidity capacity / capital")
    max_data_staleness_days: int = Field(5, ge=0)
    exclude_quality: list[str] = Field(default_factory=lambda: ["fail"])
    max_execution_failure_frac: float = Field(0.2, ge=0, le=1)


class PolicyLimits(BaseModel):
    strategy: StrategyLimits = Field(default_factory=StrategyLimits)
    group: GroupLimits = Field(default_factory=GroupLimits)
    portfolio: PortfolioLimits = Field(default_factory=PortfolioLimits)


class PolicyIn(BaseModel):
    name: str = Field("default", min_length=2, max_length=64)
    limits: PolicyLimits = Field(default_factory=PolicyLimits)
    approved_by: str = Field(min_length=2, max_length=64)
    actor_kind: str = Field("human", description="Only 'human' may create a policy")
    confirm: bool = False
    note: str = ""


class PolicyError(PermissionError):
    pass


def default_policy(session: Session) -> RiskPolicy:
    """The active policy, or the built-in default (recorded once, as created by the system defaults)."""
    p = session.scalar(select(RiskPolicy).where(RiskPolicy.active.is_(True)).order_by(RiskPolicy.created_at.desc()))
    if p:
        return p
    limits = PolicyLimits().model_dump()
    p = RiskPolicy(name="default", version=1, limits=limits, policy_hash=stable_hash(limits), created_by="system",
                   active=True, note="Built-in conservative defaults; a person can create a new version.")
    session.add(p)
    session.flush()
    audit.record(session, "system", "risk_policy_created", "risk_policy", p.id, version=1, hash=p.policy_hash)
    return p


def create_policy(session: Session, body: PolicyIn) -> RiskPolicy:
    if body.actor_kind != "human":
        raise PolicyError("only a person can create or change a risk policy; agents cannot")
    if not body.confirm:
        raise PolicyError("a new policy must be confirmed explicitly")
    last = session.scalar(select(RiskPolicy).where(RiskPolicy.name == body.name)
                          .order_by(RiskPolicy.version.desc()).limit(1))
    limits = body.limits.model_dump()
    for old in session.scalars(select(RiskPolicy).where(RiskPolicy.active.is_(True))).all():
        old.active = False
    p = RiskPolicy(name=body.name, version=(last.version + 1) if last else 1, limits=limits,
                   policy_hash=stable_hash(limits), created_by=body.approved_by, active=True, note=body.note)
    session.add(p)
    session.flush()
    audit.record(session, body.approved_by, "risk_policy_created", "risk_policy", p.id, version=p.version,
                 hash=p.policy_hash, name=p.name)
    return p


def policy_label(p: RiskPolicy) -> str:
    return f"{p.name}@v{p.version}#{p.policy_hash[:8]}"


@dataclass
class StrategyState:
    """What the HRM knows about one strategy at the decision time (all from data up to as_of)."""

    group: str
    counterparty: str
    quality: dict[str, str]
    last_available: pd.Timestamp | None
    history_days: int
    drawdown: float
    exposure: dict[str, float]  # instrument -> position per unit of strategy capital (last known)


@dataclass
class Context:
    as_of: pd.Timestamp
    strategies: dict[str, StrategyState]
    cov: pd.DataFrame
    asset_class: dict[str, str]
    dollar_volume: dict[str, float | None]
    max_participation: dict[str, float]
    capital: float
    execution_failure_frac: float = 0.0
    previous: dict[str, float] = field(default_factory=dict)


class HRM:
    def __init__(self, limits: PolicyLimits, policy_version: str):
        self.L = limits
        self.version = policy_version

    def _d(self, out: list[dict[str, Any]], ctx: Context, level: str, scope: str, rule: str, observed: Any,
           limit: Any, ok: bool, reason: str, action: str = "") -> None:
        out.append({"ts": str(ctx.as_of), "level": level, "scope": scope, "rule": rule,
                    "observed": None if observed is None else (round(float(observed), 6)
                                                               if isinstance(observed, (int, float, np.floating)) else observed),
                    "limit": limit, "decision": "approved" if ok else "blocked", "action": action, "reason": reason,
                    "policy_version": self.version})

    def enforce(self, proposed: dict[str, float], ctx: Context) -> tuple[dict[str, float], list[dict[str, Any]]]:
        """Apply every rule in a fixed order and return (allowed weights, decisions)."""
        w = {k: max(float(v), 0.0) for k, v in proposed.items()}
        dec: list[dict[str, Any]] = []
        S, G, P = self.L.strategy, self.L.group, self.L.portfolio

        if ctx.execution_failure_frac > P.max_execution_failure_frac:
            self._d(dec, ctx, "portfolio", "portfolio", "execution_failures", ctx.execution_failure_frac,
                    P.max_execution_failure_frac, False, "too many simulated execution failures: rebalance refused",
                    "keep previous allocation")
            return dict(ctx.previous) or {k: 0.0 for k in w}, dec

        # Strategy level ----------------------------------------------------------------------------
        for sid in list(w):
            st = ctx.strategies[sid]
            bad = sorted(s for s, q in st.quality.items() if q in P.exclude_quality)
            self._d(dec, ctx, "strategy", sid, "data_quality", ",".join(f"{k}:{v}" for k, v in st.quality.items()),
                    f"not in {P.exclude_quality}", not bad,
                    f"data quality {bad} excluded" if bad else "data quality acceptable",
                    "allocation set to 0" if bad else "")
            age = None if st.last_available is None else (ctx.as_of - st.last_available).days
            stale = age is None or age > P.max_data_staleness_days
            self._d(dec, ctx, "strategy", sid, "data_staleness_days", age, P.max_data_staleness_days, not stale,
                    "no recent data: the strategy cannot be sized" if stale else "data current",
                    "allocation set to 0" if stale else "")
            short = st.history_days < S.min_history_days
            self._d(dec, ctx, "strategy", sid, "min_history_days", st.history_days, S.min_history_days, not short,
                    "not enough history to estimate risk" if short else "history sufficient",
                    "allocation set to 0" if short else "")
            in_dd = st.drawdown < -S.max_drawdown
            self._d(dec, ctx, "strategy", sid, "strategy_drawdown", st.drawdown, -S.max_drawdown, not in_dd,
                    "strategy beyond its drawdown limit" if in_dd else "within drawdown limit",
                    "allocation set to 0" if in_dd else "")
            if bad or stale or short or in_dd:
                w[sid] = 0.0
            over = w[sid] > S.max_weight + 1e-12
            self._d(dec, ctx, "strategy", sid, "max_weight", w[sid], S.max_weight, not over,
                    "allocation above the per-strategy maximum" if over else "within limit",
                    f"capped {w[sid]:.4f} -> {S.max_weight}" if over else "")
            if over:
                w[sid] = S.max_weight

        # Group level --------------------------------------------------------------------------------
        groups = {sid: ctx.strategies[sid].group for sid in w}
        for g in sorted(set(groups.values())):
            members = [s for s in w if groups[s] == g]
            tot = sum(w[s] for s in members)
            ok = tot <= G.max_group_weight + 1e-12
            self._d(dec, ctx, "group", f"group:{g}", "max_group_weight", tot, G.max_group_weight, ok,
                    "strategy group over its allocation limit" if not ok else "within limit",
                    "" if ok else f"scaled x{G.max_group_weight / tot:.4f}")
            if not ok:
                for s in members:
                    w[s] *= G.max_group_weight / tot
        self._counterparty(w, ctx, dec)
        for _ in range(4):  # look-through limits interact; a few passes converge for long-only scaling
            changed = self._lookthrough(w, ctx, dec, record=False)
            if not changed:
                break
        self._lookthrough(w, ctx, dec, record=True)

        # Portfolio level -----------------------------------------------------------------------------
        ids = list(ctx.cov.columns)
        vec = np.array([w.get(s, 0.0) for s in ids])
        rc = risk_contributions(vec, ctx.cov)
        for _ in range(20):
            pct = np.asarray(rc["pct"])
            i = int(np.argmax(pct)) if len(pct) else 0
            if not len(pct) or pct[i] <= P.max_risk_share + 1e-9 or (vec > 0).sum() <= 1:
                break
            vec[i] *= max(P.max_risk_share / pct[i], 0.5)
            rc = risk_contributions(vec, ctx.cov)
        before = {s: w.get(s, 0.0) for s in ids}
        for s, v in zip(ids, vec, strict=True):
            w[s] = float(v)
        pct = np.asarray(rc["pct"])
        top = float(pct.max()) if len(pct) else 0.0
        changed = [s for s in ids if abs(before[s] - w[s]) > 1e-12]
        self._d(dec, ctx, "portfolio", "portfolio", "max_risk_share", top, P.max_risk_share,
                top <= P.max_risk_share + 1e-6 or (vec > 0).sum() <= 1,
                "risk concentrated in one strategy" if changed else "risk spread within limit",
                f"reduced {', '.join(changed)}" if changed else "")
        vol = rc["vol"]
        ok = vol <= P.max_ex_ante_vol + 1e-12
        self._d(dec, ctx, "portfolio", "portfolio", "max_ex_ante_vol", vol, P.max_ex_ante_vol, ok,
                "ex-ante volatility (trailing covariance estimate) above limit" if not ok else "within limit",
                "" if ok else f"scaled x{P.max_ex_ante_vol / vol:.4f}")
        if not ok:
            self._scale(w, P.max_ex_ante_vol / vol)
        lev = sum(w.values())
        ok = lev <= P.max_leverage + 1e-12
        self._d(dec, ctx, "portfolio", "portfolio", "max_leverage", lev, P.max_leverage, ok,
                "allocations exceed the leverage limit" if not ok else "within limit",
                "" if ok else f"scaled x{P.max_leverage / lev:.4f}")
        if not ok:
            self._scale(w, P.max_leverage / lev)
        ex = self._expo(w, ctx)
        for rule, obs, lim in (("max_gross_exposure", ex["gross"], P.max_gross_exposure),
                               ("max_net_exposure", abs(ex["net"]), P.max_net_exposure)):
            ok = obs <= lim + 1e-12
            self._d(dec, ctx, "portfolio", "portfolio", rule, obs, lim, ok,
                    "look-through exposure above limit" if not ok else "within limit",
                    "" if ok else f"scaled x{lim / obs:.4f}")
            if not ok:
                self._scale(w, lim / obs)
                ex = self._expo(w, ctx)
        self._liquidity(w, ctx, dec)
        return w, dec

    # -- helpers ----------------------------------------------------------------------------------
    @staticmethod
    def _scale(w: dict[str, float], f: float) -> None:
        for k in w:
            w[k] *= f

    def _expo(self, w: dict[str, float], ctx: Context) -> dict[str, Any]:
        return exposures(w, {s: ctx.strategies[s].exposure for s in w}, ctx.asset_class)

    def _counterparty(self, w: dict[str, float], ctx: Context, dec: list[dict[str, Any]]) -> None:
        G = self.L.group
        known = {s: ctx.strategies[s].counterparty for s in w if ctx.strategies[s].counterparty != "unassigned"}
        if len(known) < len(w):
            self._d(dec, ctx, "group", "counterparty:unassigned", "max_counterparty_share", None,
                    G.max_counterparty_share, True,
                    "no counterparty assigned to some strategies (research data): rule not evaluable for them")
        total = sum(w.values())
        if total <= 0:
            return
        for cp in sorted(set(known.values())):
            members = [s for s, c in known.items() if c == cp]
            a = sum(w[s] for s in members)
            b = total - a
            share = a / total
            ok = share <= G.max_counterparty_share + 1e-9
            if ok:
                self._d(dec, ctx, "group", f"counterparty:{cp}", "max_counterparty_share", share,
                        G.max_counterparty_share, True, "within limit")
                continue
            L = G.max_counterparty_share
            f = (L * b / (a * (1 - L))) if b > 0 else 0.0
            for s in members:
                w[s] *= f
            self._d(dec, ctx, "group", f"counterparty:{cp}", "max_counterparty_share", share, L, False,
                    "too much capital with one counterparty",
                    f"scaled x{f:.4f}" if b > 0 else "no other counterparty: allocation set to 0")

    def _lookthrough(self, w: dict[str, float], ctx: Context, dec: list[dict[str, Any]], *, record: bool) -> bool:
        G = self.L.group
        changed = False
        ex = self._expo(w, ctx)
        checks = [("instrument", ex["instrument"], G.max_instrument_gross, "max_instrument_gross"),
                  ("currency", ex["currency_net"], G.max_currency_net, "max_currency_net"),
                  ("market", ex["market_gross"], G.max_market_gross, "max_market_gross")]
        for kind, values, lim, rule in checks:
            for key in sorted(values):
                obs = abs(values[key])
                ok = obs <= lim + 1e-9
                if record:
                    self._d(dec, ctx, "group", f"{kind}:{key}", rule, obs, lim, ok,
                            "within limit" if ok else f"{kind} exposure above limit after scaling")
                if ok:
                    continue
                f = lim / obs
                for sid in w:
                    if self._touches(ctx.strategies[sid].exposure, kind, key, ctx.asset_class):
                        w[sid] *= f
                changed = True
                if not record:
                    self._d(dec, ctx, "group", f"{kind}:{key}", rule, obs, lim, False,
                            f"{kind} exposure above limit", f"contributing strategies scaled x{f:.4f}")
                ex = self._expo(w, ctx)
        return changed

    @staticmethod
    def _touches(expo: dict[str, float], kind: str, key: str, asset_class: dict[str, str]) -> bool:
        from qal.portfolio.risk import fx_legs

        for sym, e in expo.items():
            if not e:
                continue
            if kind == "instrument" and sym == key:
                return True
            if kind == "market" and asset_class.get(sym, "other") == key:
                return True
            if kind == "currency" and asset_class.get(sym) == "fx" and key in (fx_legs(sym) or ()):
                return True
        return False

    def _liquidity(self, w: dict[str, float], ctx: Context, dec: list[dict[str, Any]]) -> None:
        P = self.L.portfolio
        inst = self._expo(w, ctx)["instrument"]
        caps, unknown = [], []
        for sym, e in inst.items():
            dv = ctx.dollar_volume.get(sym)
            if not e:
                continue
            if dv is None or not np.isfinite(dv) or dv <= 0:
                unknown.append(sym)
                continue
            caps.append((ctx.max_participation.get(sym, 0.05) * dv / abs(e), sym))
        if unknown:
            self._d(dec, ctx, "portfolio", "liquidity:" + ",".join(sorted(unknown)), "min_capacity_multiple", None,
                    P.min_capacity_multiple, True,
                    "no usable traded volume (e.g. FX tick volume): capacity not evaluable, not assumed infinite")
        if not caps:
            return
        cap, sym = min(caps)
        mult = cap / ctx.capital
        ok = mult >= P.min_capacity_multiple
        self._d(dec, ctx, "portfolio", f"liquidity:{sym}", "min_capacity_multiple", mult, P.min_capacity_multiple, ok,
                "within liquidity capacity" if ok else "positions too large for the instrument's traded volume",
                "" if ok else f"scaled x{mult / P.min_capacity_multiple:.4f}")
        if not ok:
            self._scale(w, mult / P.min_capacity_multiple)


def store_decisions(session: Session, run_id: str, policy: RiskPolicy, decisions: list[dict[str, Any]],
                    *, max_rows: int = 20000) -> int:
    """Persist decisions (blocked ones first if over the cap; the cap is reported, never silent)."""
    rows = sorted(decisions, key=lambda d: d["decision"] != "blocked")[:max_rows]
    for d in rows:
        session.add(RiskDecision(run_id=run_id, policy_id=policy.id, policy_version=d["policy_version"],
                                 level=d["level"], scope=d["scope"][:128], rule=d["rule"],
                                 observed=None if d["observed"] is None else str(d["observed"]),
                                 limit_value=str(d["limit"]), decision=d["decision"], action=d["action"][:256],
                                 reason=d["reason"], as_of=d["ts"], created_at=datetime.now(UTC)))
    return len(rows)
