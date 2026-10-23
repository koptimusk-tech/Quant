"""Walk-forward validation, robustness analysis, overfitting indicators and leakage checks.

All computations are deterministic given the contract (incl. seeds) and the dataset snapshots.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from qal.research.backtest import BacktestResult, align, extract_trades, run_backtest
from qal.research.contract import HypothesisContract
from qal.research.costs import CostModel
from qal.research.metrics import (
    block_bootstrap_sharpe,
    deflated_sharpe,
    probability_of_backtest_overfitting,
    summary,
)
from qal.research.strategies import STRATEGIES

COST_MULTIPLIERS = (0.0, 0.5, 1.0, 2.0, 3.0)


def _metric(bars: pd.DataFrame, name: str, ppy: float) -> float:
    r = bars["net"].to_numpy()
    if len(r) < 2:
        return float("-inf")
    if name == "mean_net":
        return float(r.mean())
    sd = r.std(ddof=1)
    return float(r.mean() / sd * np.sqrt(ppy)) if sd > 0 else 0.0


def windows(n: int, train: int, test: int, embargo: int) -> list[dict[str, int]]:
    out, i = [], 0
    while True:
        tr0, tr1 = i, i + train
        te0 = tr1 + embargo
        te1 = te0 + test
        if te1 > n:
            break
        out.append({"train_start": tr0, "train_end": tr1, "test_start": te0, "test_end": te1})
        i += test
    return out


def purged_kfold(starts: np.ndarray, ends: np.ndarray, n_splits: int, embargo: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """Purged K-fold with embargo (Lopez de Prado). Events i span bars [starts[i], ends[i]].

    Training events whose span overlaps a test fold's span are purged; events starting within
    ``embargo`` bars after the test fold are also removed.
    """
    n = len(starts)
    folds = np.array_split(np.arange(n), n_splits)
    out = []
    for f in folds:
        t0, t1 = starts[f].min(), ends[f].max()
        overlap = (starts <= t1 + embargo) & (ends >= t0)
        train = np.setdiff1d(np.flatnonzero(~overlap), f)
        out.append((train, f))
    return out


class Validator:
    def __init__(self, contract: HypothesisContract, frames: list[pd.DataFrame], asset_class: str,
                 names: list[str], costs: list[CostModel]):
        self.c = contract
        self.names = names
        self.asset_class = asset_class
        self.costs = costs
        self.all_frames = align(frames)
        hs = pd.Timestamp(contract.validation.holdout_start)
        hs = hs.tz_localize("UTC") if hs.tzinfo is None else hs.tz_convert("UTC")
        self.holdout_start = hs
        mask = (self.all_frames[0]["available_ts"] < hs).to_numpy()
        self.n_research = int(mask.sum())
        self.research = [f.iloc[: self.n_research].reset_index(drop=True) for f in self.all_frames]
        self.strategy_cls = STRATEGIES[contract.strategy.type]

    # -- core -----------------------------------------------------------------------------------
    def backtest(self, frames: list[pd.DataFrame], params: dict[str, Any], cost_multiplier: float = 1.0) -> BacktestResult:
        w = self.strategy_cls(**params).weights(frames)
        return run_backtest(frames, w, self.costs, capital=self.c.capital, asset_class=self.asset_class,
                            names=self.names, cost_multiplier=cost_multiplier)

    def run(self, n_prior_trials: int = 0) -> dict[str, Any]:
        v = self.c.validation
        variants = self.c.strategy.variants()
        ppy_holder: dict[str, float] = {}
        full = []
        for p in variants:
            res = self.backtest(self.research, p)
            ppy_holder["ppy"] = res.periods_per_year
            full.append(res)
        ppy = ppy_holder["ppy"]
        wins = windows(self.n_research, v.train_bars, v.test_bars, v.embargo_bars)
        if not wins:
            raise ValueError(f"Not enough research bars ({self.n_research}) for train={v.train_bars}, "
                             f"embargo={v.embargo_bars}, test={v.test_bars} before the holdout")

        oos_parts, pos_parts, wrows = [], [], []
        chosen_idx: list[int] = []
        for wd in wins:
            scores = [_metric(r.bars.iloc[wd["train_start"]:wd["train_end"]], v.selection_metric, ppy) for r in full]
            best = int(np.argmax(scores))
            chosen_idx.append(best)
            test_bars = full[best].bars.iloc[wd["test_start"]:wd["test_end"]]
            oos_parts.append(test_bars)
            pos_parts.append(full[best].positions.iloc[wd["test_start"]:wd["test_end"]])
            tr = extract_trades(full[best].positions.iloc[wd["test_start"]:wd["test_end"]].reset_index(drop=True),
                                test_bars.reset_index(drop=True), self.names)
            m = summary(test_bars, tr, ppy)
            wrows.append({**wd, "train_from": str(self.research[0]["ts"].iloc[wd["train_start"]]),
                          "test_from": str(self.research[0]["ts"].iloc[wd["test_start"]]),
                          "test_to": str(self.research[0]["ts"].iloc[wd["test_end"] - 1]),
                          "chosen_params": variants[best], "train_score": scores[best],
                          "test_sharpe_net": m.get("sharpe_net"), "test_mean_net": m.get("mean_net"),
                          "test_trades": m.get("n_trades")})
        oos = pd.concat(oos_parts).reset_index(drop=True)
        oos_pos = pd.concat(pos_parts).reset_index(drop=True)
        oos_trades = extract_trades(oos_pos, oos, self.names)
        breaches = sum(full[i].capacity_breaches for i in set(chosen_idx))  # upper bound across chosen variants
        oos_metrics = summary(oos, oos_trades, ppy, capacity_breaches=0)

        # Benchmark over the same OOS bars.
        bench = None
        if self.c.benchmark == "buy_and_hold":
            bh = run_backtest(self.research[:1], pd.DataFrame({0: np.ones(self.n_research)}), self.costs[:1],
                              capital=self.c.capital, asset_class=self.asset_class, names=self.names[:1])
            bb = pd.concat([bh.bars.iloc[w["test_start"]:w["test_end"]] for w in wins]).reset_index(drop=True)
            bench = summary(bb, [], ppy)

        # Robustness ---------------------------------------------------------------------------
        cost_sens = []
        for m_ in COST_MULTIPLIERS:
            net = oos["gross"] - m_ * oos["cost"]
            sd = net.std(ddof=1)
            cost_sens.append({"multiplier": m_, "mean_net": float(net.mean()),
                              "sharpe_net": float(net.mean() / sd * np.sqrt(ppy)) if sd > 0 else 0.0,
                              "total_net": float(np.prod(1 + net) - 1)})
        grid = [{"params": variants[i], "sharpe_net_in_sample": _metric(full[i].bars, "sharpe_net", ppy),
                 "mean_net_in_sample": float(full[i].bars["net"].mean()), "n_trades": len(full[i].trades)}
                for i in range(len(variants))]
        robust_param_frac = float(np.mean([g["mean_net_in_sample"] > 0 for g in grid]))
        win_pos = [w["test_mean_net"] is not None and w["test_mean_net"] > 0 for w in wrows]
        regime = self._regime_split(oos, wins, ppy)
        placebo = self._placebo(oos_pos, oos, ppy, v.placebo_runs, v.seed)
        boot = block_bootstrap_sharpe(oos["net"].to_numpy(), ppy, n_boot=v.bootstrap_runs, seed=v.seed)

        # Overfitting --------------------------------------------------------------------------
        per_period = [g["sharpe_net_in_sample"] / np.sqrt(ppy) for g in grid]
        r = oos["net"].to_numpy()
        sr_pp = float(r.mean() / r.std(ddof=1)) if r.std(ddof=1) > 0 else 0.0
        n_trials = n_prior_trials + len(variants)
        dsr = deflated_sharpe(sr_pp, len(r), oos_metrics.get("skew", 0.0), oos_metrics.get("kurtosis", 3.0),
                              n_trials, float(np.var(per_period, ddof=1)) if len(per_period) > 1 else 0.0)
        pbo = probability_of_backtest_overfitting(np.column_stack([f.bars["net"].to_numpy() for f in full])) \
            if len(variants) > 1 else None

        leakage = self._leakage_checks(variants, chosen_idx, wins)
        return {
            "research_bars": self.n_research, "holdout_bars": len(self.all_frames[0]) - self.n_research,
            "holdout_start": str(self.holdout_start), "periods_per_year": ppy,
            "variants": len(variants), "windows": wrows,
            "oos": {"metrics": oos_metrics, "capacity_breaches": breaches,
                    "equity": self._curve(oos), "returns_hist": self._hist(oos["net"].to_numpy()),
                    "trades_sample": oos_trades[:200]},
            "benchmark": bench,
            "robustness": {"cost_sensitivity": cost_sens, "param_grid": grid, "robust_param_frac": robust_param_frac,
                           "positive_window_frac": float(np.mean(win_pos)) if win_pos else 0.0,
                           "regimes": regime, "bootstrap": boot, **placebo,
                           "per_asset_pnl_in_sample": full[max(set(chosen_idx), key=chosen_idx.count)].asset_pnl},
            "overfitting": {**dsr, "pbo": None if pbo is None else pbo["pbo"], "pbo_detail": pbo,
                            "prior_trials": n_prior_trials,
                            "note": "Parameter grid metrics are in-sample (research period); OOS metrics come from "
                                    "walk-forward test windows only. The holdout was not used."},
            "leakage_checks": leakage,
            "selected_params_last_window": variants[chosen_idx[-1]],
        }

    # -- helpers --------------------------------------------------------------------------------
    @staticmethod
    def _curve(bars: pd.DataFrame, max_points: int = 600) -> list[dict[str, Any]]:
        eq_g = np.cumprod(1 + bars["gross"].to_numpy())
        eq_n = np.cumprod(1 + bars["net"].to_numpy())
        peak = np.maximum.accumulate(eq_n)
        dd = eq_n / peak - 1
        step = max(1, len(bars) // max_points)
        return [{"ts": str(bars["ts"].iloc[i]), "equity_gross": float(eq_g[i]), "equity_net": float(eq_n[i]),
                 "drawdown": float(dd[i])} for i in range(0, len(bars), step)]

    @staticmethod
    def _hist(r: np.ndarray) -> dict[str, list[float]]:
        if len(r) == 0:
            return {"bins": [], "counts": []}
        counts, edges = np.histogram(r, bins=40)
        return {"bins": edges.tolist(), "counts": counts.tolist()}

    def _bar_returns(self) -> np.ndarray:
        """Close-to-close returns per asset (used by the placebo, consistently for actual and placebo)."""
        out = []
        for f in self.research:
            px = f["adj_close"] if f["adj_close"].notna().mean() > 0.95 else f["close"]
            out.append(px.pct_change().fillna(0.0).to_numpy())
        return np.column_stack(out)

    def _placebo(self, oos_pos: pd.DataFrame, oos: pd.DataFrame, ppy: float, runs: int, seed: int) -> dict[str, Any]:
        """Circularly shift the OOS position path against returns: same exposure profile, random timing."""
        R_all = self._bar_returns()
        idx = pd.Index(self.research[0]["ts"]).get_indexer(oos_pos["ts"])
        R = R_all[idx]
        E = oos_pos[self.names].to_numpy()
        cost = oos["cost"].to_numpy()
        actual = float(((E * R).sum(axis=1) - cost).mean())
        rng = np.random.default_rng(seed)
        T = len(E)
        if T < 30:
            return {"placebo_pvalue": None}
        sims = np.empty(runs)
        for k in range(runs):
            s = int(rng.integers(T // 10, T - T // 10))
            sims[k] = ((np.roll(E, s, axis=0) * R).sum(axis=1) - cost).mean()
        return {"placebo_pvalue": float((1 + (sims >= actual).sum()) / (runs + 1)), "placebo_runs": runs,
                "placebo_actual_mean_cc": actual, "placebo_mean": float(sims.mean())}

    def _regime_split(self, oos: pd.DataFrame, wins: list[dict[str, int]], ppy: float) -> dict[str, Any]:
        f = self.research[0]
        px = f["adj_close"] if f["adj_close"].notna().mean() > 0.95 else f["close"]
        vol = np.log(px).diff().rolling(60).std().shift(1)  # known before the bar
        idx = np.concatenate([np.arange(w["test_start"], w["test_end"]) for w in wins])
        v = vol.to_numpy()[idx]
        med = np.nanmedian(v)
        out = {}
        for name, m in (("high_vol", v > med), ("low_vol", v <= med)):
            part = oos[m]
            out[name] = {"bars": int(m.sum()), "sharpe_net": _metric(part, "sharpe_net", ppy) if len(part) > 2 else None,
                         "mean_net": float(part["net"].mean()) if len(part) else None}
        return out

    def _leakage_checks(self, variants: list[dict[str, Any]], chosen_idx: list[int], wins: list[dict[str, int]]) -> list[dict[str, Any]]:
        checks = []
        # 1. Causality: weights computed on truncated data equal the full-sample weights on the overlap.
        ok, detail = True, []
        for i in sorted(set(chosen_idx)):
            full_w = self.strategy_cls(**variants[i]).weights(self.research).to_numpy()
            for frac in (0.6, 0.85):
                cut = int(self.n_research * frac)
                part = [f.iloc[:cut].reset_index(drop=True) for f in self.research]
                w = self.strategy_cls(**variants[i]).weights(part).to_numpy()
                same = np.allclose(np.nan_to_num(w), np.nan_to_num(full_w[:cut]), atol=1e-12)
                ok &= bool(same)
                detail.append({"variant": i, "cut": cut, "identical": bool(same)})
        checks.append({"check": "strategy_causality", "passed": ok, "detail": detail,
                       "description": "Future bars do not change past target weights"})
        # 2. Execution lag: a weight decided at bar k is first held at bar k+1.
        n = min(50, self.n_research)
        probe = np.zeros((n, len(self.research)))
        probe[n // 2, 0] = 0.5
        big = [c.model_copy(update={"max_participation": 1.0}) for c in self.costs]
        res = run_backtest([f.iloc[:n].reset_index(drop=True) for f in self.research], pd.DataFrame(probe), big,
                           capital=1.0, asset_class=self.asset_class, names=self.names)
        first = int(np.flatnonzero(res.positions[self.names[0]].to_numpy())[0]) if res.positions[self.names[0]].any() else -1
        checks.append({"check": "execution_lag", "passed": first == n // 2 + 1,
                       "description": "Orders execute at the next bar's open, never on the decision bar",
                       "detail": {"decision_bar": n // 2, "first_held_bar": first}})
        # 3. Holdout isolation.
        last_avail = self.research[0]["available_ts"].iloc[-1]
        checks.append({"check": "holdout_isolated", "passed": bool(last_avail < self.holdout_start),
                       "description": "No research or selection bar is available after holdout_start",
                       "detail": {"last_research_available_ts": str(last_avail), "holdout_start": str(self.holdout_start)}})
        # 4. Temporal separation of each walk-forward window.
        sep = all(w["test_start"] >= w["train_end"] + self.c.validation.embargo_bars for w in wins)
        checks.append({"check": "walk_forward_separation", "passed": sep,
                       "description": f"Test windows start at least embargo={self.c.validation.embargo_bars} bars after training"})
        return checks

    def final_test(self, params: dict[str, Any]) -> dict[str, Any]:
        res = self.backtest(self.all_frames, params)
        mask = (res.bars["ts"] >= self.holdout_start).to_numpy()
        # Holdout bars are those whose availability is after holdout_start: same split as research.
        mask = np.zeros(len(res.bars), dtype=bool)
        mask[self.n_research:] = True
        bars = res.bars[mask].reset_index(drop=True)
        pos = res.positions[mask].reset_index(drop=True)
        trades = extract_trades(pos, bars, self.names)
        return {"params": params, "holdout_bars": int(mask.sum()),
                "metrics": summary(bars, trades, res.periods_per_year),
                "equity": self._curve(bars) if len(bars) else []}
