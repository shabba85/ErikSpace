"""Configuration loading.

Two hard rules enforced here:

1. Secrets come from the environment, never from a config file in the repo.
2. The commodity deck is loaded as *assumptions*, tagged with the date each
   price was set, so that anything computed from it is visibly an assumption
   and goes stale loudly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .provenance import DECK_NOT_SET, Q

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


def _load_yaml(p: Path) -> dict[str, Any]:
    with p.open() as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    return _load_yaml(CONFIG_DIR / "settings.yaml")


def path(key: str) -> Path:
    return ROOT / settings()["paths"][key]


def api_key(provider: str) -> str | None:
    """Read a provider key from the environment.  Absent key -> provider is
    simply unavailable; we never substitute a different source silently."""
    return os.environ.get(f"{provider.upper()}_API_KEY") or None


def contact() -> str:
    return os.environ.get("LEDGER_CONTACT", "").strip()


def user_agent() -> str:
    ua = settings()["http"]["user_agent"]
    c = contact()
    return f"{ua.replace('.env LEDGER_CONTACT', c)}" if c else ua


# ---------------------------------------------------------------------------
# commodity deck
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeckPrice:
    name: str
    value: float | None
    unit: str
    set_on: str | None
    basis: str
    stale_after_days: int
    deck_version: str

    def age_days(self, today: date | None = None) -> int | None:
        if not self.set_on:
            return None
        t = today or date.today()
        return (t - date.fromisoformat(self.set_on)).days

    def is_stale(self, today: date | None = None) -> bool:
        age = self.age_days(today)
        return age is not None and age > self.stale_after_days

    def warning(self, today: date | None = None) -> str | None:
        if self.value is None:
            return f"deck price '{self.name}' is not set"
        age = self.age_days(today)
        if age is None:
            return f"deck price '{self.name}' has no set_on date"
        if self.is_stale(today):
            return (
                f"deck price '{self.name}' = {self.value} {self.unit} was set "
                f"{age} days ago (stale after {self.stale_after_days})"
            )
        return None

    def as_q(self, today: date | None = None) -> Q:
        """The deck price as a quantity.

        Critically this is NOT method='reported' -- nothing fetched it.  It is a
        user assumption, and it says so in source_name, in the note, and in the
        staleness warning that travels with it.
        """
        if self.value is None:
            return Q.null(
                DECK_NOT_SET,
                missing=(f"deck price '{self.name}' (set it in config/deck.yaml)",),
                unit=self.unit,
                label=self.name,
            )
        from .provenance import Provenance

        note = f"USER ASSUMPTION, not observed. basis: {self.basis}"
        w = self.warning(today)
        if w:
            note += f" | WARNING: {w}"
        return Q(
            value=self.value,
            unit=self.unit,
            as_of=self.set_on,
            prov=Provenance(
                source_name="config/deck.yaml (user assumption)",
                source_url=f"file://config/deck.yaml#prices.{self.name}",
                retrieved_at=f"{self.set_on}T00:00:00+00:00",
                method="normalized",
                formula=f"deck v{self.deck_version}",
                confidence="low",
                note=note,
            ),
            label=self.name,
        )


@dataclass(frozen=True)
class Deck:
    version: str
    stale_after_days: int
    prices: dict[str, DeckPrice]

    def __getitem__(self, name: str) -> DeckPrice:
        if name not in self.prices:
            raise KeyError(
                f"unknown deck commodity {name!r}; add it to config/deck.yaml"
            )
        return self.prices[name]

    def q(self, name: str, today: date | None = None) -> Q:
        return self[name].as_q(today)

    def warnings(self, today: date | None = None) -> list[str]:
        return [w for p in self.prices.values() if (w := p.warning(today))]

    def header(self, today: date | None = None) -> dict[str, Any]:
        """What must be printed alongside every deck-normalized figure."""
        return {
            "deck_version": self.version,
            "prices": {
                k: {
                    "value": p.value,
                    "unit": p.unit,
                    "set_on": p.set_on,
                    "age_days": p.age_days(today),
                    "stale": p.is_stale(today),
                }
                for k, p in self.prices.items()
            },
            "warnings": self.warnings(today),
            "disclaimer": "Deck prices are user assumptions, not fetched data.",
        }


def load_deck(p: Path | None = None) -> Deck:
    raw = _load_yaml(p or CONFIG_DIR / "deck.yaml")
    stale = int(raw.get("stale_after_days", 90))
    ver = str(raw.get("version", "unversioned"))
    prices = {
        name: DeckPrice(
            name=name,
            value=None if d.get("value") is None else float(d["value"]),
            unit=str(d.get("unit", "")),
            set_on=d.get("set_on"),
            basis=str(d.get("basis", "")),
            stale_after_days=stale,
            deck_version=ver,
        )
        for name, d in (raw.get("prices") or {}).items()
    }
    return Deck(version=ver, stale_after_days=stale, prices=prices)
