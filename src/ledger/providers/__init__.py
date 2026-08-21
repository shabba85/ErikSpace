"""Provider registry."""

from __future__ import annotations

from typing import Any

from .. import config
from .eodhd import EODHD
from .fmp import FMP
from .tiingo import Tiingo
from .yahoo import Yahoo

REGISTRY = {p.name: p for p in (FMP(), EODHD(), Tiingo(), Yahoo())}


def get(name: str):
    if name not in REGISTRY:
        raise KeyError(f"unknown provider {name!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[name]


def configured(kind: str = "fundamentals") -> list[str]:
    return list(config.settings()["providers"][kind])


def available(kind: str = "fundamentals") -> list[str]:
    """Providers that are configured AND have credentials.

    Yahoo is always available (keyless) and always last.  A run with only Yahoo
    available is reported as such rather than presented as a normal result.
    """
    return [n for n in configured(kind) if get(n).available()]


def paid_available(kind: str = "fundamentals") -> list[str]:
    return [n for n in available(kind) if n != "yahoo"]
