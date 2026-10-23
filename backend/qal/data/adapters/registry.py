from __future__ import annotations

from dataclasses import asdict
from typing import Any

from qal.config import get_settings
from qal.data.adapters.base import DataAdapter
from qal.data.adapters.ccxt_adapter import CCXTAdapter
from qal.data.adapters.local_files import LocalFileAdapter
from qal.data.adapters.metatrader import MetaTrader5Adapter
from qal.data.adapters.stooq import StooqAdapter
from qal.data.adapters.synthetic import SyntheticAdapter
from qal.data.adapters.yahoo import YahooAdapter

ADAPTERS: dict[str, type[DataAdapter]] = {
    a.kind: a for a in (LocalFileAdapter, YahooAdapter, CCXTAdapter, MetaTrader5Adapter, StooqAdapter, SyntheticAdapter)
}


def list_adapter_kinds() -> list[str]:
    return list(ADAPTERS)


def provider_catalog() -> list[dict[str, Any]]:
    return [asdict(a.catalog()) for a in ADAPTERS.values()]


def build_adapter(kind: str, **overrides: Any) -> DataAdapter:
    """Instantiate an adapter. Credentials come from settings, never from request payloads."""
    if kind not in ADAPTERS:
        raise ValueError(f"Unknown adapter {kind!r}; available: {list(ADAPTERS)}")
    s = get_settings()
    if kind == "ccxt":
        return CCXTAdapter(exchange=overrides.pop("exchange", s.ccxt_exchange), **overrides)
    if kind == "metatrader5":
        return MetaTrader5Adapter(
            terminal_path=s.mt5_terminal_path,
            login=s.mt5_login,
            password=s.mt5_password,
            server=s.mt5_server,
            **overrides,
        )
    if kind == "stooq":
        return StooqAdapter(api_key=s.stooq_api_key, **overrides)
    return ADAPTERS[kind](**overrides)
