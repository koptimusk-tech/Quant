"""Phase 1 detectors: return shocks, volume, volatility regime, pair divergence.

All detectors are causal: the statistic at bar t uses bars <= t and baselines that end at t-1.
``tests/test_no_lookahead.py`` checks that appending future bars never changes past detections.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from qal.anomaly.base import Detection, Detector, DetectorOutput, common_explanations
from qal.data.schema import bar_delta, total_return_index
from qal.features.basic import ewma_vol, log_returns, trailing_rank_pvalue, trailing_robust


def _bar(df: pd.DataFrame) -> pd.Timedelta:
    return pd.Timedelta(bar_delta(str(df["frequency"].iloc[0])))


class ReturnShockDetector(Detector):
    name = "return_shock"
    version = "1.0.0"
    anomaly_type = "extreme_return"
    description = (
        "Robust z-score of the bar's log return against the median and MAD of the trailing window "
        "(current bar excluded). Also reports the return scaled by trailing EWMA volatility."
    )
    defaults = {"window": 250, "threshold": 5.0}

    def run(self, df: pd.DataFrame, other: pd.DataFrame | None = None) -> DetectorOutput:
        w, thr = int(self.params["window"]), float(self.params["threshold"])
        r = log_returns(df)
        valid = r.notna().to_numpy()
        pos = np.flatnonzero(valid)
        x = r.to_numpy()[valid]
        if len(x) <= w:
            return DetectorOutput(applicable=False, reason=f"Need more than {w} returns, have {len(x)}")
        med, scale, pval = trailing_robust(x, w)
        z = (x - med) / scale
        ez = (r / ewma_vol(r)).to_numpy()[valid]
        out = DetectorOutput(pvalues=pval, series=pd.Series(z, index=df["event_ts"].to_numpy()[pos]))
        bar = _bar(df)
        for k in np.flatnonzero(np.abs(np.nan_to_num(z)) >= thr):
            i = int(pos[k])
            expl = common_explanations(df, i, bar) + [
                "Scheduled information (earnings, macro releases, central bank decisions) or unscheduled news.",
                "Low liquidity or a thin session amplifying the price move.",
                "Bad tick / provider error (check against a second source).",
                "Volatility clustering: a calmer baseline makes ordinary turbulence look extreme.",
            ]
            out.detections.append(Detection(
                detector=self.name, version=self.version, anomaly_type=self.anomaly_type,
                event_ts=df["event_ts"].iloc[i], available_ts=df["available_ts"].iloc[i],
                direction=int(np.sign(x[k])), statistic=float(z[k]), empirical_pvalue=float(pval[k]),
                metrics={"log_return": float(x[k]), "robust_z": float(z[k]),
                         "ewma_vol_z": None if not np.isfinite(ez[k]) else float(ez[k]),
                         "baseline_median": float(med[k]), "baseline_mad_scale": float(scale[k]),
                         "window": w, "threshold": thr},
                alternative_explanations=expl, move_bps=abs(float(x[k])) * 1e4,
            ))
        return out


class VolumeAnomalyDetector(Detector):
    name = "volume_anomaly"
    version = "1.0.0"
    anomaly_type = "volume_liquidity"
    description = (
        "Robust z-score of log volume against the trailing window (current bar excluded). Positive = "
        "volume spike, negative = liquidity drought. Not applicable when volume is missing or mostly zero."
    )
    defaults = {"window": 120, "threshold": 4.0, "min_coverage": 0.8}

    def run(self, df: pd.DataFrame, other: pd.DataFrame | None = None) -> DetectorOutput:
        w, thr = int(self.params["window"]), float(self.params["threshold"])
        vol = df["volume"]
        good = vol.notna() & (vol > 0)
        if good.mean() < float(self.params["min_coverage"]):
            return DetectorOutput(applicable=False, reason=f"Volume coverage {good.mean():.0%} below minimum")
        pos = np.flatnonzero(good.to_numpy())
        x = np.log(vol.to_numpy()[pos])
        if len(x) <= w:
            return DetectorOutput(applicable=False, reason=f"Need more than {w} volume bars")
        med, scale, pval = trailing_robust(x, w)
        z = (x - med) / scale
        r = log_returns(df).to_numpy()
        out = DetectorOutput(pvalues=pval, series=pd.Series(z, index=df["event_ts"].to_numpy()[pos]))
        bar = _bar(df)
        for k in np.flatnonzero(np.abs(np.nan_to_num(z)) >= thr):
            i = int(pos[k])
            spike = z[k] > 0
            expl = common_explanations(df, i, bar) + (
                [
                    "Index rebalancing, options/futures expiry or end-of-period flows.",
                    "Scheduled news (earnings, macro) or unscheduled news.",
                    "Block trade or a single large participant.",
                    "Provider change in volume definition/units, or consolidated vs single-venue volume.",
                ]
                if spike
                else [
                    "Holiday or shortened session.",
                    "Partial bar or missing trades at the provider.",
                    "Trading halt or venue outage.",
                ]
            )
            ret = r[i] if np.isfinite(r[i]) else None
            out.detections.append(Detection(
                detector=self.name, version=self.version, anomaly_type=self.anomaly_type,
                event_ts=df["event_ts"].iloc[i], available_ts=df["available_ts"].iloc[i],
                direction=1 if spike else -1, statistic=float(z[k]), empirical_pvalue=float(pval[k]),
                metrics={"volume": float(vol.iloc[i]), "baseline_median_volume": float(np.exp(med[k])),
                         "volume_ratio": float(np.exp(x[k] - med[k])), "robust_z": float(z[k]),
                         "bar_log_return": ret, "dollar_volume": float(vol.iloc[i] * df["close"].iloc[i]),
                         "window": w, "threshold": thr},
                alternative_explanations=expl, move_bps=None if ret is None else abs(ret) * 1e4,
            ))
        return out


class VolatilityRegimeDetector(Detector):
    name = "volatility_regime"
    version = "1.0.0"
    anomaly_type = "volatility_shift"
    description = (
        "Ratio of short-window realised volatility (ending at t) to a long baseline that ends before the "
        "short window starts. Flags the onset bar when log2(ratio) crosses +/-threshold "
        "(default: volatility doubles or halves), with a cooldown of one short window."
    )
    defaults = {"short_window": 20, "long_window": 250, "threshold": 1.0, "rank_window": 500}
    score_reference = "threshold"

    def run(self, df: pd.DataFrame, other: pd.DataFrame | None = None) -> DetectorOutput:
        ws, wl, thr = int(self.params["short_window"]), int(self.params["long_window"]), float(self.params["threshold"])
        r = log_returns(df)
        valid = r.notna().to_numpy()
        pos = np.flatnonzero(valid)
        s = pd.Series(r.to_numpy()[valid])
        if len(s) < ws + wl + 1:
            return DetectorOutput(applicable=False, reason=f"Need at least {ws + wl + 1} returns")
        short = s.rolling(ws).std()
        long = s.rolling(wl).std().shift(ws)  # baseline ends just before the short window starts
        lr = np.log2((short / long).to_numpy())
        rank_p = trailing_rank_pvalue(np.nan_to_num(lr, nan=0.0), int(self.params["rank_window"]))
        rank_p[~np.isfinite(lr)] = np.nan
        # Variance-ratio F test p-value: valid only for iid normal returns, reported for reference.
        f = (short / long).to_numpy() ** 2
        f_p = 2 * np.minimum(stats.f.sf(f, ws - 1, wl - 1), stats.f.cdf(f, ws - 1, wl - 1))
        flag = np.abs(np.nan_to_num(lr)) >= thr
        out = DetectorOutput(pvalues=rank_p, series=pd.Series(lr, index=df["event_ts"].to_numpy()[pos]))
        bar = _bar(df)
        last = -10**9
        for k in np.flatnonzero(flag):
            if k > 0 and flag[k - 1] and np.sign(lr[k - 1]) == np.sign(lr[k]):
                continue  # not an onset
            if k - last < ws:
                continue
            last = k
            i = int(pos[k])
            up = lr[k] > 0
            expl = common_explanations(df, i, bar) + (
                ["Market-wide stress episode (check other instruments for the same onset).",
                 "Scheduled event cluster (earnings season, central bank meeting).",
                 "Change of data source, session hours or contract roll."]
                if up else
                ["Holiday period or reduced session hours.", "Post-event calm after a high-volatility episode.",
                 "Stale or interpolated prices at the provider."]
            )
            out.detections.append(Detection(
                detector=self.name, version=self.version, anomaly_type=self.anomaly_type,
                event_ts=df["event_ts"].iloc[i], available_ts=df["available_ts"].iloc[i],
                direction=1 if up else -1, statistic=float(lr[k]),
                empirical_pvalue=float(rank_p[k]) if np.isfinite(rank_p[k]) else 1.0,
                metrics={"short_vol": float(short.iloc[k]), "long_vol": float(long.iloc[k]),
                         "vol_ratio": float(2 ** lr[k]), "log2_ratio": float(lr[k]),
                         "f_test_pvalue_iid_normal": float(f_p[k]), "short_window": ws, "long_window": wl,
                         "threshold": thr},
                alternative_explanations=expl, move_bps=float(short.iloc[k]) * 1e4,
            ))
        return out


class PairDivergenceDetector(Detector):
    name = "pair_divergence"
    version = "1.0.0"
    anomaly_type = "cross_asset_divergence"
    pairwise = True
    description = (
        "Rolling OLS of log total-return index Y on X over a trailing window that ends at t-1; flags "
        "onsets where the out-of-window residual at t exceeds threshold residual standard deviations. "
        "Reports an Engle-Granger cointegration p-value on the window at each event. High correlation "
        "does not imply a stationary spread."
    )
    defaults = {"window": 120, "threshold": 4.0, "rank_window": 250, "corr_short_window": 20}

    def run(self, df: pd.DataFrame, other: pd.DataFrame | None = None) -> DetectorOutput:
        if other is None:
            raise ValueError("pair_divergence needs a second series")
        from statsmodels.tsa.stattools import coint

        w, thr = int(self.params["window"]), float(self.params["threshold"])
        a = df.assign(y=np.log(total_return_index(df)))[["ts", "available_ts", "event_ts", "y", "is_synthetic"]]
        b = other.assign(x=np.log(total_return_index(other)))[["ts", "available_ts", "x"]]
        m = a.merge(b, on="ts", suffixes=("", "_b"))
        if len(m) <= w + 1:
            return DetectorOutput(applicable=False, reason=f"Only {len(m)} common bars")
        # A pair observation is only available when both legs are.
        m["available_ts"] = m[["available_ts", "available_ts_b"]].max(axis=1)
        y, x = m["y"], m["x"]
        mx, my = x.rolling(w).mean().shift(1), y.rolling(w).mean().shift(1)
        cxy = (x * y).rolling(w).mean().shift(1) - mx * my
        vx = (x * x).rolling(w).mean().shift(1) - mx**2
        vy = (y * y).rolling(w).mean().shift(1) - my**2
        beta = cxy / vx
        alpha = my - beta * mx
        resid_sd = np.sqrt((vy - beta**2 * vx).clip(lower=0))
        spread = y - alpha - beta * x
        z = (spread / resid_sd.replace(0, np.nan)).to_numpy()
        rank_p = trailing_rank_pvalue(np.nan_to_num(z), int(self.params["rank_window"]))
        rank_p[~np.isfinite(z)] = np.nan
        ry, rx = y.diff(), x.diff()
        corr_long = ry.rolling(w).corr(rx).shift(1)
        cs = int(self.params["corr_short_window"])
        corr_short = ry.rolling(cs).corr(rx)
        flag = np.abs(np.nan_to_num(z)) >= thr
        out = DetectorOutput(pvalues=rank_p, series=pd.Series(z, index=m["event_ts"].to_numpy()))
        last = -10**9
        for k in np.flatnonzero(flag):
            if k > 0 and flag[k - 1]:
                continue
            if k - last < cs:
                continue
            last = k
            try:
                coint_p = float(coint(y.iloc[k - w:k], x.iloc[k - w:k])[1])
            except Exception:  # degenerate windows
                coint_p = None
            expl = [
                "Relationship break: the hedge ratio estimated on the window no longer holds.",
                "News or a corporate action affecting only one leg.",
                "Different session close times or a stale price on one leg (asynchronous data).",
                "Spread may be non-stationary: high correlation does not imply mean reversion.",
            ]
            if bool(m["is_synthetic"].iloc[k]):
                expl.insert(0, "Synthetic data: the event may be an injected test anomaly, not market behaviour.")
            if abs(ry.iloc[k]) < 1e-12 or abs(rx.iloc[k]) < 1e-12:
                expl.insert(0, "One leg did not move on this bar (possible stale price).")
            if coint_p is not None and coint_p > 0.1:
                expl.insert(0, f"No in-window evidence of cointegration (Engle-Granger p={coint_p:.2f}).")
            out.detections.append(Detection(
                detector=self.name, version=self.version, anomaly_type=self.anomaly_type,
                event_ts=m["event_ts"].iloc[k], available_ts=m["available_ts"].iloc[k],
                direction=int(np.sign(z[k])), statistic=float(z[k]),
                empirical_pvalue=float(rank_p[k]) if np.isfinite(rank_p[k]) else 1.0,
                metrics={"spread_z": float(z[k]), "spread": float(spread.iloc[k]), "hedge_ratio": float(beta.iloc[k]),
                         "intercept": float(alpha.iloc[k]), "residual_sd": float(resid_sd.iloc[k]),
                         "return_corr_window": float(corr_long.iloc[k]),
                         "return_corr_recent": None if not np.isfinite(corr_short.iloc[k]) else float(corr_short.iloc[k]),
                         "engle_granger_pvalue_window": coint_p, "window": w, "threshold": thr},
                alternative_explanations=expl, move_bps=abs(float(spread.iloc[k])) * 1e4, related=True,
            ))
        return out


DETECTORS: dict[str, type[Detector]] = {
    d.name: d for d in (ReturnShockDetector, VolumeAnomalyDetector, VolatilityRegimeDetector, PairDivergenceDetector)
}
