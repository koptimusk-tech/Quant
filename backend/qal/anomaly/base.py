from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np
import pandas as pd


@dataclass
class Detection:
    detector: str
    version: str
    anomaly_type: str
    event_ts: pd.Timestamp
    available_ts: pd.Timestamp
    direction: int
    statistic: float
    empirical_pvalue: float
    metrics: dict[str, Any]
    alternative_explanations: list[str]
    move_bps: float | None = None
    related: bool = False  # True for pairwise detections


@dataclass
class DetectorOutput:
    detections: list[Detection] = field(default_factory=list)
    pvalues: np.ndarray = field(default_factory=lambda: np.array([]))  # every evaluated test
    applicable: bool = True
    reason: str = ""
    # Full statistic series indexed by event_ts (for charts, reference distributions, reproduction).
    series: pd.Series | None = None

    @property
    def n_tests(self) -> int:
        return int(np.isfinite(self.pvalues).sum())


class Detector(ABC):
    name: ClassVar[str]
    version: ClassVar[str]
    anomaly_type: ClassVar[str]
    pairwise: ClassVar[bool] = False
    description: ClassVar[str] = ""
    defaults: ClassVar[dict[str, Any]] = {}
    # Score scale: |statistic| equal to this maps to severity 0.5.
    score_reference: ClassVar[str] = "threshold"

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"{self.name}: unknown parameters {sorted(unknown)}")
        self.params = {**self.defaults, **params}

    def severity(self, statistic: float) -> float:
        """Ranking score in [0, 1): |s| / (|s| + threshold). Not a probability."""
        ref = float(self.params[self.score_reference])
        s = abs(statistic)
        return s / (s + ref)

    @abstractmethod
    def run(self, df: pd.DataFrame, other: pd.DataFrame | None = None) -> DetectorOutput: ...


def gap_before(df: pd.DataFrame, i: int, bar: pd.Timedelta, factor: int = 4) -> bool:
    if i == 0:
        return False
    return (df["ts"].iloc[i] - df["ts"].iloc[i - 1]) > factor * bar


def common_explanations(df: pd.DataFrame, i: int, bar: pd.Timedelta) -> list[str]:
    out: list[str] = []
    if bool(df["is_synthetic"].iloc[i]):
        out.append("Synthetic data: the event may be an injected test anomaly, not market behaviour.")
    if gap_before(df, i, bar):
        out.append("The bar follows a data gap; the move may accumulate several missing periods.")
    if df["adj_close"].notna().iloc[i] and i > 0:
        raw = np.log(df["close"].iloc[i] / df["close"].iloc[i - 1])
        adj = np.log(df["adj_close"].iloc[i] / df["adj_close"].iloc[i - 1])
        if abs(raw - adj) > 0.02:
            out.append("Corporate action on this bar (raw and adjusted returns differ).")
    return out
