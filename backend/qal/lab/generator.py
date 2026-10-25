"""Hypothesis generator of the Experiment Lab: experiment specs within bounded families, from the data loaded.

It reuses the Research Lab's deterministic proposal rules (``qal.research.proposals``: momentum, events after
return shocks, correlated pairs) and adds the families those rules do not cover (single-instrument mean reversion,
volatility-regime conditioning, cross-sectional price factors). Every spec states the mechanism, the signal, entry
and exit, horizon, universe, costs and risks, the baselines to beat and what would invalidate it.

Rules against near-duplicates: one spec per (family, strategy, universe); a spec whose key already has a lab
experiment is not proposed again (re-running it would only add selection risk). The generator reports how many
specs and variants it proposed and how many it skipped, so the multiple-testing burden is visible before running.

No language model is involved. A generated spec is a research proposal, not evidence.
"""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from qal.lab.service import KIND
from qal.lab.spec import ExperimentSpec
from qal.models import Dataset, Experiment
from qal.research.proposals import _design, _label, build_proposals
from qal.versioning import stable_hash

GENERATOR_VERSION = "lab-generator@1.0.0"
KIND_TO_FAMILY = {"momentum": "momentum", "event": "anomaly", "pair": "pairs"}


def _key(family: str, strategy: str, ds_ids: list[str], s: Session) -> str:
    inst = sorted(s.get(Dataset, d).instrument_id for d in ds_ids)
    return stable_hash([family, strategy, inst])[:20]


def _existing_keys(s: Session) -> set[str]:
    return {(e.config or {}).get("generator_key") for e in
            s.scalars(select(Experiment).where(Experiment.kind == KIND)).all()} - {None}


def _mean_reversion(base: dict[str, Any], ds: Dataset) -> dict[str, Any] | None:
    design = _design(ds, 10)
    if design is None:
        return None
    c = copy.deepcopy(base)
    c.update(title=f"{_label(ds)}: short-term mean reversion", features=["20-bar z-score of the log price"],
             strategy={"type": "mean_reversion", "params": {"window": 20, "exit_z": 0.5, "max_hold": 10},
                       "param_grid": {"entry_z": [1.5, 2.0, 2.5]}},
             entry_rule="At the next open after the log price is entry_z rolling deviations from its 20-bar mean, "
                        "trade back towards the mean",
             exit_rule="Exit when the z-score is back inside 0.5 or after 10 bars", horizon_bars=10,
             premise="Short-term overreaction to liquidity demand pushes prices away from their recent level, and "
                     "the excess is reabsorbed within days once the order flow is absorbed.",
             mechanism="Liquidity providers are paid to absorb temporary imbalances; prices revert as they unwind "
                       "inventory, which makes large short-term deviations partly predictable.",
             risks=["Trending markets keep extending the move", "Costs eat small reversion profits"],
             invalidation=["Net out-of-sample mean return at or below zero", "No improvement over the permuted signal"],
             validation=design)
    return c


def _regime(base: dict[str, Any]) -> dict[str, Any]:
    c = copy.deepcopy(base)
    c["title"] = base["title"].replace("time-series momentum", "momentum only in high-volatility regimes")
    c["strategy"] = {"type": "ts_momentum", "params": {"regime": "high_vol"},
                     "param_grid": base["strategy"]["param_grid"]}
    c["features"] = base["features"] + ["20/250-bar realised volatility ratio > 1"]
    c["premise"] = ("Trends are stronger when volatility is high because de-risking flows and forced selling are "
                    "persistent, so momentum should work better in those regimes than on average.")
    c["entry_rule"] = "Hold the sign of the trailing return only while 20-bar volatility exceeds the 250-bar one"
    c["invalidation"] = ["The regime filter does not beat the same rule without it (ablation)",
                         "Net out-of-sample mean return at or below zero"]
    return c


def _factor(datasets: list[Dataset]) -> list[dict[str, Any]]:
    out = []
    groups: dict[tuple[str, str], list[Dataset]] = {}
    for d in datasets:
        groups.setdefault((d.frequency, d.instrument.asset_class), []).append(d)
    for (freq, _), g in groups.items():
        if len(g) < 3 or freq != "1d":
            continue
        g = sorted(g, key=lambda d: d.rows)[:12]
        design = _design(g[0], 21)
        if design is None:
            continue
        names = ", ".join(d.instrument.symbol for d in g)
        out.append({
            "title": f"{'SYNTHETIC ' if g[0].is_synthetic else ''}Cross-sectional momentum ({names})",
            "premise": "Assets that outperformed their peers over the past year keep outperforming for some months, "
                       "because investors under-react to gradual news.",
            "mechanism": "Under-reaction and slow capital flows make relative strength persist at horizons of months; "
                         "the last month is skipped to avoid short-term reversal.",
            "source_anomaly_ids": [], "dataset_ids": [d.id for d in g], "features": ["12-1 month relative return"],
            "strategy": {"type": "cross_sectional_factor", "params": {"factor": "momentum", "skip": 21},
                         "param_grid": {"lookback": [126, 252]}},
            "entry_rule": "Every 21 bars, long the top third and short the bottom third by trailing return",
            "exit_rule": "Rebalanced every 21 bars; positions held until the next rebalance", "horizon_bars": 21,
            "benchmark": "cash", "risks": ["Momentum crashes after market rebounds", "Small universes are noisy"],
            "invalidation": ["Net out-of-sample mean return at or below zero", "No improvement over the permuted signal"],
            "validation": design})
    return out


def generate(s: Session, *, families: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
    fams = set(families or ["momentum", "mean_reversion", "anomaly", "pairs", "regime", "factor"])
    existing = _existing_keys(s)
    specs, skipped = [], []
    props = build_proposals(s)
    by_ds = {}
    for p in props:
        fam = KIND_TO_FAMILY.get(p["kind"])
        c = p["contract"]
        if p["kind"] == "momentum":
            by_ds[c["dataset_ids"][0]] = c
        if fam in fams:
            specs.append((fam, c, None, p["why"]))
    datasets = [d for d in s.scalars(select(Dataset).where(Dataset.is_latest.is_(True))).all()
                if d.quality_status != "fail"]
    for d in datasets:
        base = by_ds.get(d.id)
        if base is None:
            continue
        if "mean_reversion" in fams and (c := _mean_reversion(base, d)) is not None:
            specs.append(("mean_reversion", c, None, "Short-term reversion is the counterpart of momentum at "
                                                     "horizons of days; tested against its own permuted signal."))
        if "regime" in fams:
            specs.append(("regime", _regime(base), {"regime": "all"},
                          "Tests whether conditioning momentum on the volatility regime adds anything over the same "
                          "rule without the filter (ablation baseline)."))
    if "factor" in fams:
        specs += [("factor", c, None, "Relative strength across a universe; needs 3 or more instruments.")
                  for c in _factor(datasets)]
    out = []
    for fam, c, ablation, why in specs:
        key = _key(fam, c["strategy"]["type"], c["dataset_ids"], s)
        if key in existing or any(o["generator_key"] == key for o in out):
            skipped.append({"family": fam, "title": c["title"], "reason": "an experiment with the same family, "
                            "strategy and universe exists: a near-identical variation adds selection risk"})
            continue
        try:
            spec = ExperimentSpec.model_validate({"family": fam, "contract": c, "ablation": ablation,
                                                  "generated_by": f"generator:{fam}"})
        except ValueError as exc:
            skipped.append({"family": fam, "title": c["title"], "reason": f"invalid: {exc}"[:300]})
            continue
        out.append({"generator_key": key, "family": fam, "title": c["title"], "why": why,
                    "variants": spec.contract.strategy.n_variants(), "baselines": spec.resolved_baselines(),
                    "is_synthetic": any(s.get(Dataset, d).is_synthetic for d in c["dataset_ids"]),
                    "spec": spec.model_dump(mode="json")})
        if len(out) >= limit:
            break
    return {"version": GENERATOR_VERSION, "proposed": out, "skipped": skipped, "n_specs": len(out),
            "n_variants": sum(o["variants"] for o in out),
            "note": "Research proposals from deterministic rules, not evidence. Every variant run counts as a trial "
                    "in the lab-wide multiple-testing correction."}


__all__ = ["GENERATOR_VERSION", "generate"]
