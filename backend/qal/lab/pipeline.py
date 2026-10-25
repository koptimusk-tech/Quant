"""Common validation pipeline of the Experiment Lab (stages A-E).

Every candidate goes through the same stages. The pipeline reuses the research engine: ``Validator`` (walk-forward
selection with embargo, holdout isolation, leakage checks, placebo, block bootstrap, DSR, PBO) and
``run_backtest`` (next-open execution, costs, capacity). What it adds is the comparison with baselines on exactly
the same out-of-sample bars, robustness re-runs (training length, execution delay, universe subsets) and a few
statistics (HAC t-statistic, sensitivity to the best bars).

* A integrity: data quality, timestamps, prices, frequency, temporal separation, leakage checks, survivorship
  and cost-data warnings. A failed *critical* check makes the experiment INVALID: no later number is used.
* B baselines: candidate minus baseline, bar by bar on the stitched walk-forward test windows.
* C temporal validation: walk-forward with re-selection per window; the isolated final period only if the spec
  asks, once.
* D robustness: parameters, training length, test periods, costs, delays, regimes, universe subsets. The output
  lists where the edge disappears; it does not demand profit everywhere.
* E statistics: gross and net, Sharpe and drawdown, confidence intervals, independent trades, extreme-value
  sensitivity, DSR with the lab-wide trial count, PBO.

Nothing here touches stored data or results: it reads frames and returns a report.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from qal.lab.spec import ExperimentSpec
from qal.research.backtest import run_backtest
from qal.research.contract import HypothesisContract
from qal.research.costs import CostModel
from qal.research.metrics import deflated_sharpe
from qal.research.strategies import STRATEGIES
from qal.research.validation import Validator

PIPELINE_VERSION = "lab-pipeline@1.0.0"
MIN_OOS_BARS = 120
MIN_INDEPENDENT_TRADES = 20


class Cancelled(Exception):
    pass


def _sharpe(r: np.ndarray, ppy: float) -> float | None:
    if len(r) < 2 or np.std(r, ddof=1) == 0:
        return None
    return float(np.mean(r) / np.std(r, ddof=1) * np.sqrt(ppy))


def _series_metrics(r: np.ndarray, ppy: float) -> dict[str, Any]:
    if len(r) == 0:
        return {"bars": 0}
    eq = np.cumprod(1 + r)
    return {"bars": int(len(r)), "mean_net": float(r.mean()), "sharpe_net": _sharpe(r, ppy),
            "total_return_net": float(eq[-1] - 1), "max_drawdown": float((eq / np.maximum.accumulate(eq) - 1).min())}


def hac_t(x: np.ndarray, lag: int) -> tuple[float | None, float | None]:
    """Newey-West t-statistic of the mean (overlapping holding periods make bars dependent) and its one-sided p."""
    x = np.asarray(x, float)
    n = len(x)
    if n < 10 or np.allclose(x, x[0]):
        return None, None
    u = x - x.mean()
    s = (u @ u) / n
    for k in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - k / (lag + 1)) * (u[k:] @ u[:-k]) / n
    if s <= 0:
        return None, None
    t = float(x.mean() / np.sqrt(s / n))
    return t, float(stats.t.sf(t, n - 1))


def bootstrap_mean_ci(x: np.ndarray, seed: int, n_boot: int = 1000) -> tuple[float, float] | None:
    """Stationary block bootstrap 95% CI of the mean."""
    T = len(x)
    if T < 20:
        return None
    rng = np.random.default_rng(seed)
    block = max(5, int(round(T ** (1 / 3))))
    t = np.arange(T)
    out = np.empty(n_boot)
    for b in range(n_boot):
        jumps = rng.random(T) < 1 / block
        jumps[0] = True
        seg = np.cumsum(jumps) - 1
        t0 = t[jumps]
        start = rng.integers(T, size=len(t0))
        out[b] = x[(start[seg] + (t - t0[seg])) % T].mean()
    return float(np.quantile(out, 0.025)), float(np.quantile(out, 0.975))


class Pipeline:
    def __init__(self, spec: ExperimentSpec, frames: list[pd.DataFrame], names: list[str], asset_class: str,
                 costs: list[CostModel], quality: dict[str, str], *, check: Callable[[], None] = lambda: None,
                 progress: Callable[[str], None] = lambda stage: None):
        self.spec, self.c = spec, spec.contract
        self.frames, self.names, self.asset_class, self.costs, self.quality = frames, names, asset_class, costs, quality
        self.check, self.progress = check, progress
        self.cls = STRATEGIES[self.c.strategy.type]
        self.v: Validator | None = None
        self.wf: dict[str, Any] | None = None

    # -- helpers -------------------------------------------------------------------------------------
    def _bt(self, W: pd.DataFrame, frames: list[pd.DataFrame] | None = None, cost_mult: float = 1.0):
        v = self.v
        return run_backtest(frames or v.research, W, v.costs if frames is None else self.costs, capital=self.c.capital,
                            asset_class=self.asset_class, names=self.names, cost_multiplier=cost_mult)

    def _stitch(self, per_window_params: list[dict[str, Any]], transform: Callable[[pd.DataFrame], pd.DataFrame]
                | None = None) -> np.ndarray:
        """Net returns on the walk-forward test windows when window k trades ``per_window_params[k]``."""
        cache: dict[str, np.ndarray] = {}
        parts = []
        for wd, params in zip(self.wf["wins"], per_window_params, strict=True):
            key = repr(sorted(params.items()))
            if key not in cache:
                W = self.cls(**params).weights(self.v.research)
                if transform is not None:
                    W = transform(W)
                cache[key] = self._bt(W).bars["net"].to_numpy()
            parts.append(cache[key][wd["test_start"]:wd["test_end"]])
        return np.concatenate(parts) if parts else np.array([])

    def _chosen(self) -> list[dict[str, Any]]:
        return [self.wf["variants"][i] for i in self.wf["chosen_idx"]]

    # -- stage A -------------------------------------------------------------------------------------
    def integrity_pre(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []

        def add(check: str, passed: bool, detail: Any, critical: bool = True) -> None:
            out.append({"check": check, "passed": bool(passed), "critical": critical, "detail": detail})

        add("data_quality", all(q != "fail" for q in self.quality.values()), self.quality)
        freqs = {str(f["frequency"].iloc[0]) for f in self.frames if len(f)}
        add("same_frequency", len(freqs) == 1, sorted(freqs))
        mono = all(f["ts"].is_monotonic_increasing and not f["ts"].duplicated().any() for f in self.frames)
        add("timestamps_ordered_unique", mono, "bar starts strictly increasing in every dataset")
        avail = all((f["available_ts"] >= f["event_ts"]).all() for f in self.frames)
        add("availability_after_bar_end", avail, "available_ts >= event_ts for every bar")
        prices = all((f[["open", "high", "low", "close"]] > 0).all().all() for f in self.frames)
        add("positive_prices", prices, "open/high/low/close > 0")
        vd = self.c.validation
        add("embargo_covers_horizon", vd.embargo_bars >= self.c.horizon_bars,
            {"embargo": vd.embargo_bars, "horizon": self.c.horizon_bars})
        try:
            self.v = Validator(self.c, self.frames, self.asset_class, self.names, self.costs)
            need = vd.train_bars + vd.embargo_bars + 2 * vd.test_bars
            add("enough_research_bars", self.v.n_research >= need,
                {"research_bars": self.v.n_research, "needed": need, "holdout_bars": len(self.v.all_frames[0])
                 - self.v.n_research})
        except Exception as exc:  # e.g. no common bars
            add("validator_setup", False, f"{type(exc).__name__}: {exc}")
            return out
        ends = [pd.Timestamp(f["ts"].iloc[-1]) for f in self.frames]
        starts = [pd.Timestamp(f["ts"].iloc[0]) for f in self.frames]
        spread = (max(ends) - min(ends)).days
        add("universe_survivorship", spread <= 10,
            {"end_spread_days": spread, "note": "The universe was chosen with today's list of instruments: "
             "survivorship bias cannot be ruled out for equities and ETFs." if self.asset_class in ("equity", "etf")
             else "Instruments end within 10 days of each other.", "starts": [str(s.date()) for s in starts]},
            critical=False)
        missing = [d for d in self.c.dataset_ids if d not in self.c.costs]
        sym = dict(zip(self.c.dataset_ids, self.names, strict=False))
        add("explicit_costs", not missing, {"datasets_on_default_costs": missing,
                                            "symbols_on_default_costs": [sym.get(d, d) for d in missing],
                                            "note": "asset-class default costs are estimates"}, critical=False)
        if self.spec.evaluate_holdout:
            add("holdout_size", len(self.v.all_frames[0]) - self.v.n_research >= vd.test_bars,
                {"holdout_bars": len(self.v.all_frames[0]) - self.v.n_research, "min": vd.test_bars}, critical=False)
        return out

    # -- stage B -------------------------------------------------------------------------------------
    def baselines(self, oos: np.ndarray, report: dict[str, Any], ppy: float, seed: int) -> list[dict[str, Any]]:
        rows = []
        n = len(oos)
        for b in self.spec.resolved_baselines():
            self.check()
            if b == "no_trade":
                r, how = np.zeros(n), "flat: zero return, zero cost"
            elif b == "buy_and_hold":
                W = pd.DataFrame(np.column_stack([np.ones(len(self.v.research[0]))] +
                                                 [np.zeros(len(self.v.research[0]))] * (len(self.names) - 1)))
                full = self._bt(W).bars["net"].to_numpy()
                r = np.concatenate([full[w["test_start"]:w["test_end"]] for w in self.wf["wins"]])
                how = f"long {self.names[0]} on the same test windows, same costs"
            elif b == "permuted_signal":
                rows.append({"baseline": b, "how": "the candidate's own out-of-sample positions circularly shifted "
                             f"against returns ({report['robustness'].get('placebo_runs')} shifts): same exposure "
                             "profile, random timing", "placebo_pvalue": report["robustness"].get("placebo_pvalue"),
                             "placebo_mean_cc": report["robustness"].get("placebo_mean"),
                             "beaten": (report["robustness"].get("placebo_pvalue") or 1) <= 0.1})
                continue
            elif b == "simple_reference":
                params = dict(self.cls.defaults)
                r = self._stitch([params] * len(self.wf["wins"]))
                how = f"{self.c.strategy.type} with its default, unoptimised parameters {params}"
            elif b == "ablation":
                r = self._stitch([{**p, **self.spec.ablation} for p in self._chosen()])
                how = f"the selected parameters with the feature switched off {self.spec.ablation}"
            else:  # pragma: no cover - validated in the spec
                continue
            d = oos - r
            t, p = hac_t(d, max(1, self.c.horizon_bars))
            ci = bootstrap_mean_ci(d, seed + len(rows))
            rows.append({"baseline": b, "how": how, "metrics": _series_metrics(r, ppy),
                         "diff_mean_net": float(d.mean()) if n else None, "diff_ci95": ci, "diff_t_hac": t,
                         "diff_pvalue_one_sided": p, "beaten": bool(ci is not None and ci[0] > 0)})
        return rows

    # -- stage D -------------------------------------------------------------------------------------
    def robustness(self, report: dict[str, Any], oos: np.ndarray, ppy: float) -> dict[str, Any]:
        rob = report["robustness"]
        out: dict[str, Any] = {"parameters": {"grid": rob["param_grid"], "positive_frac": rob["robust_param_frac"]},
                               "costs": rob["cost_sensitivity"], "regimes": rob["regimes"]}
        # test periods: each walk-forward window and each calendar year of the stitched test bars
        out["windows"] = [{"test_from": w["test_from"], "test_to": w["test_to"], "chosen_params": w["chosen_params"],
                           "mean_net": w["test_mean_net"], "sharpe_net": w["test_sharpe_net"]} for w in report["windows"]]
        yr = pd.DatetimeIndex(pd.to_datetime(self.wf["oos"]["ts"], utc=True)).year.to_numpy()
        years = []
        for y in sorted(set(yr.tolist())):
            m = yr == y
            years.append({"year": int(y), **_series_metrics(oos[m], ppy)})
        out["years"] = years
        # training length
        tl = []
        for f in self.spec.robustness.train_factors:
            self.check()
            tb = max(50, int(round(self.c.validation.train_bars * f)))
            c2 = self.c.model_copy(update={"validation": self.c.validation.model_copy(update={"train_bars": tb})})
            try:
                wf2 = Validator(c2, self.frames, self.asset_class, self.names, self.costs).walk_forward()
                tl.append({"train_bars": tb, "factor": f, **_series_metrics(wf2["oos"]["net"].to_numpy(), ppy)})
            except ValueError as exc:
                tl.append({"train_bars": tb, "factor": f, "not_applicable": str(exc)})
        out["training_length"] = tl
        # execution delay: decisions acted d bars later
        dl = []
        for d in self.spec.robustness.delays:
            self.check()
            r = self._stitch(self._chosen(), lambda W, d=d: W.shift(d).fillna(0.0))
            dl.append({"delay_bars": d, **_series_metrics(r, ppy)})
        out["delays"] = dl
        cm = []
        for m in self.spec.robustness.cost_multipliers:
            net = self.wf["oos"]["gross"].to_numpy() - m * self.wf["oos"]["cost"].to_numpy()
            cm.append({"multiplier": m, **_series_metrics(net, ppy)})
        out["costs"] = cm
        # universe subsets
        subsets = []
        if self.spec.robustness.leave_one_out and len(self.frames) >= 4 and self.cls.n_assets == 0:
            for i in range(len(self.frames)):
                self.check()
                keep = [j for j in range(len(self.frames)) if j != i]
                c2 = self.c.model_copy(update={"dataset_ids": [self.c.dataset_ids[j] for j in keep]})
                v2 = Validator(c2, [self.frames[j] for j in keep], self.asset_class, [self.names[j] for j in keep],
                               [self.costs[j] for j in keep])
                subsets.append({"without": self.names[i], **_series_metrics(v2.walk_forward()["oos"]["net"].to_numpy(),
                                                                            ppy)})
        elif self.cls.n_assets == 0:
            subsets.append({"not_applicable": "needs 4 or more instruments to drop one and keep a universe of 3"})
        else:
            subsets.append({"not_applicable": "single instrument or fixed pair"})
        out["universe_subsets"] = subsets
        breaks = []
        for x in tl + dl + cm + subsets:
            if x.get("mean_net") is not None and x["mean_net"] <= 0:
                breaks.append({k: v for k, v in x.items() if k in ("train_bars", "delay_bars", "multiplier", "without")}
                              | {"mean_net": x["mean_net"]})
        for name, reg in rob["regimes"].items():
            if reg.get("mean_net") is not None and reg["mean_net"] <= 0:
                breaks.append({"regime": name, "mean_net": reg["mean_net"]})
        for y in years:  # partial years (< 60 bars) and years with no position at all are not evidence of a break
            if y.get("mean_net") is not None and y["bars"] >= 60 and y["mean_net"] < 0:
                breaks.append({"year": y["year"], "mean_net": y["mean_net"]})
        out["breaks_under"] = breaks
        return out

    # -- stage E -------------------------------------------------------------------------------------
    def statistics(self, report: dict[str, Any], oos: np.ndarray, ppy: float, n_trials: int) -> dict[str, Any]:
        m = report["oos"]["metrics"]
        t, p = hac_t(oos, max(1, self.c.horizon_bars))
        srt = np.sort(oos)
        k1 = max(1, int(len(oos) * 0.01))
        no_top5 = np.delete(oos, np.argsort(oos)[-5:]) if len(oos) > 10 else oos
        per_period = [g["sharpe_net_in_sample"] / np.sqrt(ppy) for g in report["robustness"]["param_grid"]]
        sr_pp = float(oos.mean() / oos.std(ddof=1)) if len(oos) > 1 and oos.std(ddof=1) > 0 else 0.0
        dsr = deflated_sharpe(sr_pp, len(oos), m.get("skew", 0.0), m.get("kurtosis", 3.0), n_trials,
                              float(np.var(per_period, ddof=1)) if len(per_period) > 1 else 0.0)
        return {
            "oos": {k: m.get(k) for k in ("bars", "total_return_gross", "total_return_net", "sharpe_gross", "sharpe_net",
                                          "max_drawdown", "n_trades", "hit_rate", "profit_factor", "top5_trades_share",
                                          "turnover_ann", "total_cost", "exposure_avg", "skew", "kurtosis", "mean_net")},
            "sharpe_ci95": [report["robustness"]["bootstrap"].get("sharpe_ci_low"),
                            report["robustness"]["bootstrap"].get("sharpe_ci_high")],
            "mean_net_ci95": bootstrap_mean_ci(oos, self.c.validation.seed),
            "t_hac": t, "pvalue_one_sided": p, "hac_lag": max(1, self.c.horizon_bars),
            "independent_trades": m.get("n_trades"),
            "extremes": {"sharpe_without_best_5_bars": _sharpe(no_top5, ppy),
                         "sharpe_without_best_1pct_bars": _sharpe(srt[:-k1], ppy) if len(srt) > k1 + 1 else None},
            "dsr": dsr.get("dsr"), "dsr_detail": dsr, "n_trials": n_trials,
            "pbo": report["overfitting"].get("pbo"),
            "placebo_pvalue": report["robustness"].get("placebo_pvalue"),
            "assumptions": ["HAC (Newey-West) t-statistic with lag = horizon: bars are dependent, not independent.",
                            "Bootstrap CIs use stationary blocks of ~T^(1/3) bars.",
                            "DSR uses the in-sample Sharpe dispersion of the grid and the lab-wide trial count.",
                            "Costs are the contract's assumptions (asset-class estimates unless set explicitly)."],
        }

    # -- run -----------------------------------------------------------------------------------------
    def run(self, *, n_prior_trials: int, holdout_accesses_before: int) -> dict[str, Any]:
        out: dict[str, Any] = {"version": PIPELINE_VERSION}
        self.progress("A_integrity")
        pre = self.integrity_pre()
        out["A_integrity"] = {"checks": pre}
        if any(c["critical"] and not c["passed"] for c in pre):
            out["invalid"] = [c["check"] for c in pre if c["critical"] and not c["passed"]]
            return out
        self.check()
        self.progress("C_temporal")
        v = self.v
        report = v.run(n_prior_trials=n_prior_trials)
        self.wf = v.last_walk_forward
        self.check()
        leak = report["leakage_checks"]
        out["A_integrity"]["checks"] += [{"check": f"leakage:{c['check']}", "passed": c["passed"], "critical": True,
                                          "detail": c.get("detail") or c.get("description")} for c in leak]
        if not all(c["passed"] for c in leak):
            out["invalid"] = [f"leakage:{c['check']}" for c in leak if not c["passed"]]
            return out
        ppy = report["periods_per_year"]
        oos = self.wf["oos"]["net"].to_numpy()
        n_trials = n_prior_trials + len(self.wf["variants"])
        out["C_temporal"] = {"windows": report["windows"], "research_bars": report["research_bars"],
                             "holdout_bars": report["holdout_bars"], "holdout_start": report["holdout_start"],
                             "embargo_bars": self.c.validation.embargo_bars, "oos_bars": int(len(oos)),
                             "selected_params_last_window": report["selected_params_last_window"],
                             "in_sample_grid": report["robustness"]["param_grid"]}
        self.progress("B_baselines")
        out["B_baselines"] = self.baselines(oos, report, ppy, self.c.validation.seed)
        self.progress("D_robustness")
        out["D_robustness"] = self.robustness(report, oos, ppy)
        self.progress("E_statistics")
        out["E_statistics"] = self.statistics(report, oos, ppy, n_trials)
        out["policy_gates_A_D"] = _gates(self.c, report, self.quality)
        if self.spec.evaluate_holdout:
            self.check()
            self.progress("holdout")
            ft = v.final_test(report["selected_params_last_window"])
            access = holdout_accesses_before + 1
            out["C_temporal"]["holdout"] = {**ft, "access_number": access, "independent": access == 1,
                                            "note": "First look at the final period." if access == 1 else
                                            "This final period was examined before with the same data and split: "
                                            "it is no longer an independent test."}
            out["C_temporal"]["holdout"].pop("equity", None)
        out["capacity_breach_frac"] = report["oos"]["capacity_breaches"] / max(1, len(oos))
        out["oos_series"] = {"ts": [str(t) for t in self.wf["oos"]["available_ts"]],
                             "net": oos.round(10).tolist()}
        out["oos_curve"] = {"ts": [str(t) for t in self.wf["oos"]["ts"].iloc[:: max(1, len(oos) // 400)]],
                            "equity": np.cumprod(1 + oos)[:: max(1, len(oos) // 400)].round(6).tolist()}
        return out


def _gates(c: HypothesisContract, report: dict[str, Any], quality: dict[str, str]) -> list[dict[str, Any]]:
    from qal.research.policy import evaluate_gates

    return [{"gate": g.gate, "passed": g.passed, "reasons": g.reasons} for g in evaluate_gates(c, report, quality)]


__all__ = ["MIN_INDEPENDENT_TRADES", "MIN_OOS_BARS", "PIPELINE_VERSION", "Cancelled", "Pipeline", "bootstrap_mean_ci",
           "hac_t"]
