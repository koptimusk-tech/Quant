"""Portfolio risk engine: covariance, correlations, risk contributions, exposures, concentration and
diversification. Deterministic; every estimate uses only the returns passed in (callers pass data up to the
decision date).

Estimates from history describe the past. Correlations measured in calm periods are not assumed to hold in
stress: the engine reports stress-conditional correlations separately and the stress module tests the case
where diversification disappears.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

PPY = 252.0
COV_METHODS = ("sample", "ledoit_wolf", "ewma")


def _clean(R: pd.DataFrame, min_obs: int = 20) -> pd.DataFrame:
    """Rows where every column has a value (a common sample keeps the covariance positive semi-definite)."""
    X = R.dropna(how="any")
    if len(X) < min_obs:
        raise ValueError(f"only {len(X)} common observations (need {min_obs}) to estimate the covariance")
    return X


def covariance(R: pd.DataFrame, method: str = "ledoit_wolf", halflife: float = 60.0, min_obs: int = 20) -> pd.DataFrame:
    """Annualised covariance of daily returns.

    ``ledoit_wolf`` shrinks the sample covariance towards a scaled identity (robust when observations are
    few relative to strategies or the sample covariance is unstable); ``ewma`` weights recent days more.
    """
    X = _clean(R, min_obs)
    A = X.to_numpy()
    if method == "sample":
        C = np.cov(A, rowvar=False, ddof=1)
    elif method == "ledoit_wolf":
        from sklearn.covariance import LedoitWolf

        C = LedoitWolf().fit(A).covariance_ * len(A) / max(len(A) - 1, 1)
    elif method == "ewma":
        lam = 0.5 ** (1.0 / halflife)
        w = lam ** np.arange(len(A))[::-1]
        w /= w.sum()
        mu = (w[:, None] * A).sum(axis=0)
        D = A - mu
        C = (w[:, None] * D).T @ D
    else:
        raise ValueError(f"unknown covariance method {method!r}; use one of {COV_METHODS}")
    C = np.atleast_2d(C) * PPY
    return pd.DataFrame(C, index=R.columns, columns=R.columns)


def corr_from_cov(C: pd.DataFrame) -> pd.DataFrame:
    d = np.sqrt(np.clip(np.diag(C.to_numpy()), 1e-18, None))
    return pd.DataFrame(C.to_numpy() / np.outer(d, d), index=C.index, columns=C.columns)


def _pairwise_mean(corr: np.ndarray) -> float | None:
    n = corr.shape[0]
    if n < 2:
        return None
    return float(corr[~np.eye(n, dtype=bool)].mean())


def correlations(R: pd.DataFrame, *, window: int = 63, stress_q: float = 0.1,
                 vol_window: int = 60) -> dict[str, Any]:
    """Static, rolling, regime and stress-conditional correlations of strategy returns.

    * static: whole common sample;
    * rolling: mean pairwise correlation over a trailing window (shows instability);
    * regime: split by an EW portfolio volatility measured over the *previous* ``vol_window`` days against
      its expanding median (causal: known before the day);
    * stress: the most turbulent days, by EW portfolio volatility over the previous 20 days (top ``stress_q``).
      Conditioning on the worst *returns* instead would bias correlations downwards (selection on the sum).
    """
    X = R.dropna(how="any")
    cols = list(R.columns)
    out: dict[str, Any] = {"strategies": cols, "observations": len(X)}
    if len(X) < 20 or len(cols) < 1:
        out["note"] = "not enough common observations"
        return out
    static = X.corr().to_numpy()
    out["static"] = static.tolist()
    out["static_mean_pairwise"] = _pairwise_mean(static)
    roll = []
    for end in range(window, len(X) + 1, max(1, window // 3)):
        c = X.iloc[end - window:end].corr().to_numpy()
        roll.append({"ts": str(X.index[end - 1].date()), "mean_pairwise": _pairwise_mean(c)})
    out["rolling"] = {"window": window, "points": roll,
                      "min": min((p["mean_pairwise"] for p in roll if p["mean_pairwise"] is not None), default=None),
                      "max": max((p["mean_pairwise"] for p in roll if p["mean_pairwise"] is not None), default=None)}
    ew = X.mean(axis=1)
    vol = ew.rolling(vol_window).std().shift(1)
    thresh = vol.expanding(min_periods=vol_window).median().shift(1)
    hi = (vol > thresh).to_numpy()
    lo = (vol <= thresh).to_numpy()
    reg = {}
    for name, m in (("high_vol", hi), ("low_vol", lo)):
        part = X[m]
        reg[name] = {"days": int(m.sum()),
                     "matrix": part.corr().to_numpy().tolist() if len(part) > 20 else None,
                     "mean_pairwise": _pairwise_mean(part.corr().to_numpy()) if len(part) > 20 else None}
    out["regime"] = {**reg, "rule": f"EW portfolio vol over the previous {vol_window} days vs its expanding median"}
    turb = ew.rolling(20).std().shift(1)
    sm = (turb >= turb.quantile(1 - stress_q)).to_numpy()
    sc = X[sm].corr().to_numpy() if sm.sum() > 10 else None
    out["stress"] = {"days": int(sm.sum()), "quantile": stress_q,
                     "matrix": None if sc is None else sc.tolist(),
                     "mean_pairwise": None if sc is None else _pairwise_mean(sc),
                     "rule": f"days whose previous-20-day EW portfolio volatility is in its top {stress_q:.0%}",
                     "note": "Correlation in the most turbulent periods observed. Future stress can be worse; "
                             "the stress module also tests correlations near 1."}
    return out


def risk_contributions(w: np.ndarray, C: pd.DataFrame | np.ndarray) -> dict[str, Any]:
    """Euler decomposition: RC_i = w_i (C w)_i / sigma_p, summing to the portfolio volatility."""
    Cm = C.to_numpy() if isinstance(C, pd.DataFrame) else C
    w = np.asarray(w, float)
    var = float(w @ Cm @ w)
    sig = np.sqrt(max(var, 0.0))
    if sig <= 0:
        z = np.zeros_like(w)
        return {"vol": 0.0, "marginal": z.tolist(), "contrib": z.tolist(), "pct": z.tolist()}
    mrc = Cm @ w / sig
    rc = w * mrc
    return {"vol": sig, "marginal": mrc.tolist(), "contrib": rc.tolist(), "pct": (rc / sig).tolist()}


def diversification(w: np.ndarray, C: pd.DataFrame) -> dict[str, Any]:
    """Weight and risk concentration, effective number of strategies and of independent bets."""
    Cm = C.to_numpy()
    w = np.asarray(w, float)
    aw = np.abs(w)
    hhi_w = float((aw**2).sum() / max(aw.sum() ** 2, 1e-18))
    rc = risk_contributions(w, Cm)
    pct = np.clip(np.asarray(rc["pct"]), 0, None)
    hhi_r = float((pct**2).sum() / max(pct.sum() ** 2, 1e-18)) if pct.sum() > 0 else None
    vols = np.sqrt(np.clip(np.diag(Cm), 0, None))
    dr = float(aw @ vols / rc["vol"]) if rc["vol"] > 0 else None
    # Effective number of bets: entropy of the portfolio variance spread over principal components.
    vals, vecs = np.linalg.eigh(Cm)
    vals = np.clip(vals, 0, None)
    loads = vecs.T @ w
    var_pc = vals * loads**2
    p = var_pc / var_pc.sum() if var_pc.sum() > 0 else var_pc
    enb = float(np.exp(-(p[p > 0] * np.log(p[p > 0])).sum())) if p.sum() > 0 else None
    return {"hhi_weights": hhi_w, "effective_n_weights": 1 / hhi_w if hhi_w > 0 else None,
            "hhi_risk": hhi_r, "effective_n_risk": 1 / hhi_r if hhi_r else None,
            "diversification_ratio": dr, "effective_number_of_bets": enb,
            "first_pc_variance_share": float(vals[-1] / vals.sum()) if vals.sum() > 0 else None,
            "max_risk_share": float(pct.max()) if len(pct) else None}


def fx_legs(symbol: str) -> tuple[str, str] | None:
    s = symbol.upper().replace("SYN-", "").replace("/", "").replace("_", "")
    s = "".join(ch for ch in s if ch.isalpha())
    return (s[:3], s[3:6]) if len(s) == 6 else None


def exposures(weights: dict[str, float], strat_exposure: dict[str, dict[str, float]],
              asset_class: dict[str, str], groups: dict[str, str] | None = None) -> dict[str, Any]:
    """Look-through exposure: instrument exposure = sum over strategies of weight x strategy position.

    FX instruments are also split into currency legs (long base / short quote), so two strategies on
    different pairs that are both short USD show up as one concentrated USD exposure.
    """
    inst: dict[str, float] = {}
    for sid, w in weights.items():
        for sym, e in (strat_exposure.get(sid) or {}).items():
            if e is None or not np.isfinite(e):
                continue
            inst[sym] = inst.get(sym, 0.0) + w * float(e)
    market: dict[str, float] = {}
    ccy: dict[str, float] = {}
    for sym, e in inst.items():
        ac = asset_class.get(sym, "other")
        market[ac] = market.get(ac, 0.0) + abs(e)
        legs = fx_legs(sym) if ac == "fx" else None
        if legs:
            ccy[legs[0]] = ccy.get(legs[0], 0.0) + e
            ccy[legs[1]] = ccy.get(legs[1], 0.0) - e
    by_group: dict[str, float] = {}
    if groups:
        for sid, w in weights.items():
            g = groups.get(sid, "")
            by_group[g] = by_group.get(g, 0.0) + abs(w)
    return {"instrument": inst, "market_gross": market, "currency_net": ccy, "strategy_group": by_group,
            "gross": float(sum(abs(v) for v in inst.values())), "net": float(sum(inst.values()))}


def drawdown_stats(r: pd.Series) -> dict[str, Any]:
    if len(r) < 2:
        return {}
    eq = (1 + r.fillna(0)).cumprod()
    dd = eq / eq.cummax() - 1
    trough = dd.idxmin()
    peak = eq.loc[:trough].idxmax()
    rec = eq.loc[trough:]
    recovered = rec[rec >= eq.loc[peak]]
    return {"max_drawdown": float(dd.min()), "peak": str(peak.date()), "trough": str(trough.date()),
            "recovered": str(recovered.index[0].date()) if len(recovered) else None,
            "current_drawdown": float(dd.iloc[-1])}


def marginal_analysis(R: pd.DataFrame, C: pd.DataFrame, w: np.ndarray, corr_threshold: float = 0.7) -> list[dict[str, Any]]:
    """For each strategy: what removing it changes, and whether it adds risk without diversifying.

    Flags a strategy as redundant when its correlation with the rest of the portfolio is above
    ``corr_threshold``, or when dropping it (and renormalising) lowers volatility without lowering the
    effective number of bets.
    """
    cols = list(C.columns)
    X = R[cols].dropna(how="any")
    out = []
    base = diversification(w, C)
    base_vol = risk_contributions(w, C)["vol"]
    for i, sid in enumerate(cols):
        rest = [j for j in range(len(cols)) if j != i]
        if not rest:
            out.append({"strategy": sid, "corr_with_rest": None, "redundant": False})
            continue
        wr = np.asarray(w, float)[rest]
        wr = wr / wr.sum() * np.asarray(w).sum() if wr.sum() > 0 else wr
        Cr = C.iloc[rest, rest]
        d_rest = diversification(wr, Cr)
        rest_ret = X.iloc[:, rest].to_numpy() @ (wr / max(wr.sum(), 1e-12))
        rho = float(np.corrcoef(X.iloc[:, i].to_numpy(), rest_ret)[0, 1]) if len(X) > 2 else None
        vol_wo = risk_contributions(wr, Cr)["vol"]
        enb_b, enb_r = base["effective_number_of_bets"] or 0, d_rest["effective_number_of_bets"] or 0
        redundant = bool((rho is not None and rho > corr_threshold) or (vol_wo < base_vol and enb_r >= enb_b - 1e-9))
        out.append({"strategy": sid, "corr_with_rest": rho, "vol_with": base_vol, "vol_without": vol_wo,
                    "enb_with": base["effective_number_of_bets"], "enb_without": d_rest["effective_number_of_bets"],
                    "dr_with": base["diversification_ratio"], "dr_without": d_rest["diversification_ratio"],
                    "redundant": redundant,
                    "note": ("adds risk without adding diversification" if redundant else "diversifies")})
    return out


def factor_exposure(R: pd.DataFrame, n_factors: int = 3) -> dict[str, Any]:
    """Statistical common factors (PCA of the correlation matrix) and each strategy's loading."""
    X = R.dropna(how="any")
    if len(X) < 20 or X.shape[1] < 2:
        return {"note": "not enough data"}
    Z = (X - X.mean()) / X.std(ddof=1).replace(0, np.nan)
    Z = Z.fillna(0.0)
    vals, vecs = np.linalg.eigh(np.corrcoef(Z.to_numpy(), rowvar=False))
    order = np.argsort(vals)[::-1][:n_factors]
    return {"explained": (vals[order] / vals.sum()).tolist(),
            "loadings": {sid: vecs[i, order].tolist() for i, sid in enumerate(X.columns)},
            "note": "Statistical factors from historical returns; they need not persist."}


def exposure_overlap(strat_exposure: dict[str, pd.DataFrame], asset_class: dict[str, str]) -> dict[str, Any]:
    """Signal redundancy: cosine similarity of the strategies' average signed currency/instrument exposure."""
    ids = list(strat_exposure)
    vecs = []
    keys: set[str] = set()
    per: dict[str, dict[str, float]] = {}
    for sid in ids:
        df = strat_exposure[sid]
        avg = {}
        for sym in df.columns:
            m = float(df[sym].mean())
            legs = fx_legs(sym) if asset_class.get(sym) == "fx" else None
            if legs:
                avg[legs[0]] = avg.get(legs[0], 0) + m
                avg[legs[1]] = avg.get(legs[1], 0) - m
            else:
                avg[sym] = avg.get(sym, 0) + m
        per[sid] = avg
        keys |= set(avg)
    ks = sorted(keys)
    for sid in ids:
        vecs.append([per[sid].get(k, 0.0) for k in ks])
    V = np.array(vecs, float)
    norms = np.linalg.norm(V, axis=1)
    S = np.where(np.outer(norms, norms) > 0, V @ V.T / np.maximum(np.outer(norms, norms), 1e-18), 0.0)
    return {"strategies": ids, "keys": ks, "similarity": S.tolist()}
