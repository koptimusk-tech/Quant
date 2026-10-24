"""Portfolio researcher agent: proposes strategy combinations; the engine computes; the risk manager decides.

The agent is deterministic (rules over stored evidence, no language model) and has no authority: it cannot
approve a portfolio, change a limit, pick a policy other than the active one, or include a strategy the
eligibility rules rejected. Every proposal, computation and risk decision is logged as an AgentMessage of
the proposals run, with references to the stored experiments.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from qal import audit
from qal.models import AgentMessage, Experiment
from qal.portfolio.risk_manager import PolicyError, default_policy, policy_label
from qal.portfolio.service import PortfolioRunIn, load, run_portfolio
from qal.portfolio.strategies import EligibilityCriteria, catalog
from qal.versioning import code_version, stable_hash

AGENT = "portfolio_researcher"


class ProposalRequest(BaseModel):
    include_synthetic: bool = Field(False, description="Use labelled synthetic strategies (engine testing only)")
    strategy_ids: list[str] = Field(default_factory=list, description="Restrict the candidates (default: all eligible)")
    corr_threshold: float = Field(0.7, gt=0, lt=1)
    mc_paths: int = Field(500, ge=100, le=5000)
    limits: dict[str, Any] | None = Field(None, description="Refused: agents cannot set limits")
    policy_id: str | None = Field(None, description="Refused: agents use the active policy")


def _msg(session: Session, run: Experiment, seq: list[int], sender: str, receiver: str, kind: str, key: str,
         payload: dict[str, Any], evidence: list[dict[str, Any]] | None = None) -> None:
    seq[0] += 1
    session.add(AgentMessage(run_id=run.id, seq=seq[0], sender=sender, receiver=receiver, kind=kind,
                             subject_key=key[:64], payload=payload, evidence=evidence or []))


def propose(session: Session, req: ProposalRequest, *, actor: str = f"agent:{AGENT}") -> Experiment:
    if req.limits is not None or req.policy_id is not None:
        audit.record(session, actor, "agent_policy_change_refused", "risk_policy", None,
                     requested={"limits": req.limits is not None, "policy_id": req.policy_id})
        raise PolicyError("agents cannot set limits or choose a risk policy; the active policy applies")
    policy = default_policy(session)
    crit = EligibilityCriteria(allow_synthetic=req.include_synthetic)
    recs = [r for r in catalog(session, crit, include_synthetic=req.include_synthetic)
            if not req.strategy_ids or r.id in req.strategy_ids]
    run = Experiment(kind="portfolio_proposals", status="running", algorithm_version=f"{AGENT}@1.0.0",
                     code_version=code_version(), config=req.model_dump(mode="json"),
                     config_hash=stable_hash(req.model_dump(mode="json")), data_refs=[], seed=None, n_tests=0,
                     results={})
    session.add(run)
    session.flush()
    seq = [0]
    eligible = [r for r in recs if r.eligible]
    for r in recs:
        if not r.eligible:
            _msg(session, run, seq, "eligibility_rules", AGENT, "objection", r.id,
                 {"strategy": r.id, "reasons": r.reasons, "note": "rejected strategies cannot be proposed"})
    proposals = _proposals(session, eligible, req)
    results = []
    for p in proposals:
        _msg(session, run, seq, AGENT, "risk_engine", "proposal", p["key"],
             {"strategies": p["strategy_ids"], "methods": p["methods"], "rationale": p["rationale"]})
        cfg = PortfolioRunIn(strategy_ids=p["strategy_ids"], methods=p["methods"], criteria=crit,
                             label=f"agent proposal: {p['key']}", mc_paths=req.mc_paths, stress_backtests=False)
        exp = run_portfolio(session, cfg, actor=actor)
        res = exp.results or {}
        m = ((res.get("primary") or {}).get("metrics")) or {}
        _msg(session, run, seq, "risk_engine", "risk_manager", "computation", p["key"],
             {"experiment_id": exp.id, "status": exp.status, "error": exp.error,
              "metrics": {k: m.get(k) for k in ("sharpe_net", "volatility_ann", "max_drawdown", "total_return_net")}},
             [{"experiment_id": exp.id}])
        last = ((res.get("primary") or {}).get("weights_history") or [{}])[-1]
        blocked = last.get("blocked_rules", []) if last else []
        verdict = ("failed to run" if exp.status != "succeeded" else
                   "within limits" if not blocked else "limits enforced: weights reduced by the risk manager")
        _msg(session, run, seq, "risk_manager", "human", "decision", p["key"],
             {"experiment_id": exp.id, "verdict": verdict, "blocked_rules_last_rebalance": blocked,
              "policy_version": policy_label(policy),
              "note": "The risk manager enforces limits; approving a portfolio is a human decision. No capital "
                      "is allocated and nothing trades."}, [{"experiment_id": exp.id}])
        results.append({"key": p["key"], "experiment_id": exp.id, "status": exp.status, "verdict": verdict,
                        "blocked_rules": blocked, "strategies": p["strategy_ids"], "methods": p["methods"],
                        "rationale": p["rationale"],
                        "metrics": {k: m.get(k) for k in ("sharpe_net", "volatility_ann", "max_drawdown")}})
    run.results = {"proposals": results, "candidates": len(recs), "eligible": len(eligible),
                   "is_synthetic": any(r.is_synthetic for r in eligible), "policy_version": policy_label(policy),
                   "note": "Agents propose; the engine computes; the risk manager decides. Agents cannot approve."}
    run.n_tests = len(results)
    run.status = "succeeded" if eligible else "failed"
    run.error = None if eligible else "no eligible strategies to combine"
    run.finished_at = datetime.now(UTC)
    audit.record(session, actor, "portfolio_proposals_finished", "experiment", run.id, n=len(results))
    return run


def _proposals(session: Session, eligible: list[Any], req: ProposalRequest) -> list[dict[str, Any]]:
    if not eligible:
        return []
    ids = [r.id for r in eligible]
    out = [{"key": "all-risk-budget", "strategy_ids": ids, "methods": ["risk_budget", "equal_weight"],
            "rationale": "every eligible strategy with equal risk contributions; equal weight for comparison"}]
    if len(ids) < 3:
        return out
    cfg = PortfolioRunIn(strategy_ids=ids, criteria=EligibilityCriteria(allow_synthetic=req.include_synthetic))
    _, chosen, series, _ = load(session, cfg)
    import pandas as pd

    R = pd.DataFrame({s: series[s].net for s in chosen}).dropna(how="any")
    if len(R) < 60:
        return out
    C = R.corr()
    sharpe = {s: float(R[s].mean() / R[s].std(ddof=1) * np.sqrt(252)) if R[s].std(ddof=1) > 0 else 0.0 for s in R}
    drop, pairs = set(), []
    for i, a in enumerate(R.columns):
        for b in R.columns[i + 1:]:
            if C.loc[a, b] > req.corr_threshold:
                pairs.append((a, b, float(C.loc[a, b])))
                drop.add(a if sharpe[a] < sharpe[b] else b)
    if drop:
        keep = [s for s in ids if s not in drop]
        out.append({"key": "drop-redundant", "strategy_ids": keep, "methods": ["risk_budget"],
                    "rationale": f"drop {sorted(drop)}: correlation above {req.corr_threshold} with another strategy "
                                 f"({[(a, b, round(c, 2)) for a, b, c in pairs]}), keeping the one with higher "
                                 "historical Sharpe (in-sample observation, not a forecast)"})
    mean_corr = C.where(~np.eye(len(C), dtype=bool)).mean()
    low = [s for s in ids if mean_corr.get(s, 1) < 0.3]
    if 2 <= len(low) < len(ids):
        out.append({"key": "low-correlation-invvol", "strategy_ids": low, "methods": ["inverse_vol"],
                    "rationale": "strategies whose average correlation with the others is below 0.3"})
    return out
