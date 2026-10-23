"""Provider adapter interface.

An adapter only fetches and returns a provider frame indexed by bar start (UTC) with
open/high/low/close[/adj_close]/volume. Normalisation, quality checks and storage are done
by the ingestion service so every provider goes through the same controls.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar

import pandas as pd

from qal.data.schema import InstrumentSpec


class ProviderError(RuntimeError):
    """The provider answered with an error or unusable data."""


class ProviderUnavailable(ProviderError):
    """The provider cannot be used in this environment (missing library, terminal, key, network)."""


@dataclass
class FetchRequest:
    instrument: InstrumentSpec
    frequency: str
    start: datetime | None = None
    end: datetime | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class CatalogEntry:
    kind: str
    name: str
    asset_classes: list[str]
    frequencies: list[str]
    requires_credentials: bool
    cost: str
    license_notes: str
    history_notes: str
    delay_notes: str
    limits_notes: str
    install: str


class DataAdapter(ABC):
    kind: ClassVar[str]
    is_synthetic: ClassVar[bool] = False
    supported_frequencies: ClassVar[tuple[str, ...]] = ()
    # Conservative publication delay added to the bar end to obtain available_ts.
    default_availability_delay: ClassVar[timedelta] = timedelta(0)
    adjustment: ClassVar[str] = "raw"
    max_attempts: ClassVar[int] = 3

    def __init__(self, provider_id: str | None = None, **config: Any) -> None:
        self.provider_id = provider_id or self.kind
        self.config = config

    @classmethod
    @abstractmethod
    def catalog(cls) -> CatalogEntry: ...

    @abstractmethod
    def _fetch(self, req: FetchRequest) -> pd.DataFrame: ...

    def source_ref(self, req: FetchRequest) -> str:
        return f"{self.provider_id}:{req.instrument.symbol}:{req.frequency}"

    def availability_delay(self, req: FetchRequest) -> timedelta:
        return self.default_availability_delay

    def fetch(self, req: FetchRequest) -> pd.DataFrame:
        if self.supported_frequencies and req.frequency not in self.supported_frequencies:
            raise ProviderError(
                f"{self.kind} does not support frequency {req.frequency!r} "
                f"(supported: {', '.join(self.supported_frequencies)})"
            )
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._fetch(req)
            except ProviderUnavailable:
                raise
            except Exception as exc:  # network hiccups, rate limits
                last = exc
                if attempt < self.max_attempts:
                    time.sleep(min(2**attempt, 10) * self.config.get("retry_base_seconds", 1.0))
        raise ProviderError(f"{self.kind} failed after {self.max_attempts} attempts: {last}") from last
