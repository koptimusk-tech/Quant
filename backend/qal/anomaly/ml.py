"""Multivariate unsupervised detectors (Isolation Forest, Local Outlier Factor), fitted causally.

A model fitted on the whole history and used to score past bars would use the future. These detectors are
refitted walk-forward instead: at each refit point t0 the model, the feature scaling and the reference
distribution of scores are estimated on the previous ``train_window`` bars only, and the model scores the
next ``refit_every`` bars. So the score of bar t depends on bars <= t only (checked by the no-lookahead
tests).

The anomaly score is not a probability. Each bar gets an empirical p-value: the share of training-window
scores at least as extreme, ``(1 + #{ref > s} + #{ref == s} / 2) / (n_ref + 1)``, where the reference scores come from the
last third of the training window, held out of the fit (scores of points the model was fitted on are biased,
strongly so for LOF). A bar is flagged when that p-value is at or below ``contamination``, so
``contamination`` is the expected share of flagged bars in a stable market. The p-values go into the scan's
Benjamini-Hochberg correction like every other detector's.

Rolling features (kurtosis, autocorrelation, relative volatility) keep the trace of a shock for up to their
window length, so one event can produce a cluster of flagged bars; the model comparator counts episodes.

The ``direction`` of a detection is the sign of the bar's return: these models say a bar is unusual, not
which way prices will move next.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from qal.anomaly.base import Detection, Detector, DetectorOutput, common_explanations
from qal.data.schema import bar_delta
from qal.features.catalog import DEFAULT_FEATURES, build, usable


def _robust_scale(train: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    med = np.median(train, axis=0)
    mad = np.median(np.abs(train - med), axis=0) * 1.4826
    sd = train.std(axis=0)
    scale = np.where(mad > 1e-12, mad, np.where(sd > 1e-12, sd, 1.0))
    return med, scale


class WalkForwardModelDetector(Detector):
    """Common machinery: causal feature matrix, walk-forward refits, training-window p-values."""

    anomaly_type = "multivariate_outlier"
    stochastic = False  # True when the seed changes the result
    common_defaults: dict[str, Any] = {
        "features": list(DEFAULT_FEATURES), "train_window": 1500, "refit_every": 0, "min_train": 300,
        "contamination": 0.01, "scaling": "robust", "seed": 0,
    }

    def __init__(self, **params: Any) -> None:
        if isinstance(params.get("features"), str):  # "ret_1,rel_vol" from a form field
            params["features"] = [f.strip() for f in params["features"].split(",") if f.strip()]
        super().__init__(**params)
        p = self.params
        if not 0 < float(p["contamination"]) <= 0.1:
            raise ValueError(f"{self.name}: contamination must be in (0, 0.1]")
        if int(p["min_train"]) < 50 or int(p["train_window"]) < int(p["min_train"]):
            raise ValueError(f"{self.name}: need 50 <= min_train <= train_window")
        if p["scaling"] not in ("robust", "none"):
            raise ValueError(f"{self.name}: scaling must be robust or none")
        if int(p["refit_every"]) < 0:
            raise ValueError(f"{self.name}: refit_every must be >= 0 (0 = automatic)")
        if not p["features"]:
            raise ValueError(f"{self.name}: select at least one feature")
        usable(pd.DataFrame({"frequency": ["1d"], "volume": [1.0]}), list(p["features"]))  # unknown names raise

    def severity(self, statistic: float) -> float:
        s = max(float(statistic), 0.0)
        return s / (s + 3.0)

    def _fit_score(self, fit: np.ndarray, ref: np.ndarray, test: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Fit on ``fit``; return anomaly scores (higher = more unusual) of the held-out ``ref`` rows, which
        are the reference distribution for p-values, and of the rows to score."""
        raise NotImplementedError

    def run(self, df: pd.DataFrame, other: pd.DataFrame | None = None) -> DetectorOutput:
        p = self.params
        names, dropped = usable(df, list(p["features"]))
        if len(names) < 2:
            return DetectorOutput(applicable=False, reason=f"fewer than 2 usable features (dropped: {dropped})")
        X = build(df, names)
        valid = X.notna().all(axis=1).to_numpy()
        pos = np.flatnonzero(valid)
        Xv = X.to_numpy()[valid]
        n, w, m = len(Xv), int(p["train_window"]), int(p["min_train"])
        refit = int(p["refit_every"]) or max(25, w // 20)  # never depends on the series length (causality)
        if n < m + refit:
            return DetectorOutput(applicable=False, reason=f"need at least {m + refit} complete feature rows, have {n}")
        score = np.full(n, np.nan)
        pval = np.full(n, np.nan)
        zscore = np.full(n, np.nan)
        contrib: list[tuple[np.ndarray, np.ndarray] | None] = [None] * n
        for t0 in range(m, n, refit):
            tr = Xv[max(0, t0 - w):t0]
            te = Xv[t0:t0 + refit]
            if p["scaling"] == "robust":
                med, sc = _robust_scale(tr)
            else:
                med, sc = np.zeros(tr.shape[1]), np.ones(tr.shape[1])
            z_tr = (tr - med) / sc
            k_ref = max(50, len(tr) // 3)  # held-out tail of the training window calibrates the p-values
            s_tr, s_te = self._fit_score(z_tr[:-k_ref], z_tr[-k_ref:], (te - med) / sc)
            ref_med = np.median(s_tr)
            ref_mad = np.median(np.abs(s_tr - ref_med)) * 1.4826 or (s_tr.std() or 1.0)
            srt = np.sort(s_tr)
            above = len(srt) - np.searchsorted(srt, s_te, side="right")
            ties = np.searchsorted(srt, s_te, side="right") - np.searchsorted(srt, s_te, side="left")
            exceed = above + 0.5 * ties  # mid-rank: saturated scores (ties with the reference) are not penalised
            score[t0:t0 + len(te)] = s_te
            pval[t0:t0 + len(te)] = (1 + exceed) / (len(srt) + 1)
            zscore[t0:t0 + len(te)] = (s_te - ref_med) / ref_mad
            for k in range(len(te)):
                contrib[t0 + k] = (med, sc)
        out = DetectorOutput(pvalues=pval, series=pd.Series(zscore, index=df["event_ts"].to_numpy()[pos]))
        r = np.log(df["close"].astype(float)).diff().to_numpy()
        bar = pd.Timedelta(bar_delta(str(df["frequency"].iloc[0])))
        alpha = float(p["contamination"])
        for k in np.flatnonzero(np.nan_to_num(pval, nan=1.0) <= alpha):
            i = int(pos[k])
            med, sc = contrib[k]
            fz = (Xv[k] - med) / sc
            top = np.argsort(-np.abs(fz))[:3]
            expl = common_explanations(df, i, bar) + [
                "The combination of features is unusual relative to the training window, which can come from a "
                "single extreme feature (compare with the univariate detectors) or from an unusual mix.",
                "A change of regime makes many recent bars look unusual against an older training window.",
                "Data errors (bad ticks, stale prices) produce outliers in several features at once.",
            ]
            out.detections.append(Detection(
                detector=self.name, version=self.version, anomaly_type=self.anomaly_type,
                event_ts=df["event_ts"].iloc[i], available_ts=df["available_ts"].iloc[i],
                direction=int(np.sign(r[i])) if np.isfinite(r[i]) else 0, statistic=float(zscore[k]),
                empirical_pvalue=float(pval[k]),
                metrics={"anomaly_score": float(score[k]), "score_robust_z": float(zscore[k]),
                         "contamination": alpha, "features": names, "features_dropped": dropped,
                         "top_features": [{"feature": names[j], "robust_z": float(fz[j])} for j in top],
                         "train_window": w, "refit_every": refit, "log_return": float(r[i]) if np.isfinite(r[i])
                         else None},
                alternative_explanations=expl,
                move_bps=abs(float(r[i])) * 1e4 if np.isfinite(r[i]) else None,
            ))
        return out


class IsolationForestDetector(WalkForwardModelDetector):
    name = "isolation_forest"
    version = "1.0.0"
    description = ("Isolation Forest on a causal feature vector (returns, relative volatility, robust z, range, gap, "
                   "kurtosis, autocorrelation), refitted walk-forward on the previous train_window bars; flags "
                   "bars whose isolation score is in the top `contamination` share of the training scores.")
    stochastic = True
    defaults = {**WalkForwardModelDetector.common_defaults, "n_estimators": 100, "max_samples": 256}

    def _fit_score(self, fit: np.ndarray, ref: np.ndarray, test: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        from sklearn.ensemble import IsolationForest

        model = IsolationForest(n_estimators=int(self.params["n_estimators"]),
                                max_samples=min(int(self.params["max_samples"]), len(fit)),
                                random_state=int(self.params["seed"])).fit(fit)
        return -model.score_samples(ref), -model.score_samples(test)


class LocalOutlierFactorDetector(WalkForwardModelDetector):
    name = "local_outlier_factor"
    version = "1.0.0"
    description = ("Local Outlier Factor (novelty mode) on the same causal features, refitted walk-forward: flags "
                   "bars whose local density is much lower than that of their nearest training neighbours. "
                   "Distances lose contrast as the number of features grows; keep the feature set small.")
    defaults = {**WalkForwardModelDetector.common_defaults, "n_neighbors": 35}

    def _fit_score(self, fit: np.ndarray, ref: np.ndarray, test: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        from sklearn.neighbors import LocalOutlierFactor

        k = min(int(self.params["n_neighbors"]), len(fit) - 1)
        model = LocalOutlierFactor(n_neighbors=k, novelty=True).fit(fit)
        return -model.score_samples(ref), -model.score_samples(test)
