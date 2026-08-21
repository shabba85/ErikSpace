"""Provider abstraction.

Adapters do exactly two things: fetch bytes, and map vendor field names onto
the canonical schema.  They perform NO arithmetic.  If a vendor reports a ratio
we need, we ignore it and compute it ourselves from the underlying line items,
because a vendor's ratio carries the vendor's undisclosed conventions and we
cannot audit it.

Every value an adapter emits is built by ``field_q`` below, which cannot
construct a populated quantity without a source name, a source URL and a
retrieval timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from ..http import Fetched, FetchError
from ..model import Company, Period
from ..provenance import NOT_FETCHED, UPSTREAM_ERROR, Q


class ProviderUnavailable(RuntimeError):
    """Provider has no key, or is not configured.  Never a reason to substitute
    another source silently -- the caller records which providers answered."""


@dataclass(frozen=True)
class Quote:
    last_close: Q
    market_cap: Q
    shares_outstanding: Q


class Provider(Protocol):
    name: str
    confidence: str

    def available(self) -> bool: ...
    def symbol(self, ticker: str) -> str: ...
    def profile(self, ticker: str) -> dict[str, Any]: ...
    def quote(self, ticker: str) -> Quote: ...
    def annual_periods(self, ticker: str, limit: int) -> list[Period]: ...
    def quarterly_periods(self, ticker: str, limit: int) -> list[Period]: ...


def field_q(fetched: Fetched, raw: Mapping[str, Any], vendor_key: str, *,
            unit: str, as_of: str | None, label: str, confidence: str = "high",
            scale: float = 1.0, note: str | None = None) -> Q:
    """Lift one vendor field into a provenance-carrying quantity.

    A key that is absent, null, or non-numeric yields a null naming the vendor
    key -- so an audit shows precisely which vendor field was missing, not just
    that something was.
    """
    if vendor_key not in raw:
        return Q.null(NOT_FETCHED,
                      missing=(f"{label}: {fetched.source} did not return "
                               f"'{vendor_key}'",), unit=unit, label=label)
    v = raw[vendor_key]
    if v is None or isinstance(v, bool):
        return Q.null(NOT_FETCHED,
                      missing=(f"{label}: {fetched.source} returned null for "
                               f"'{vendor_key}'",), unit=unit, label=label)
    try:
        val = float(v) * scale
    except (TypeError, ValueError):
        return Q.null(NOT_FETCHED,
                      missing=(f"{label}: {fetched.source} returned non-numeric "
                               f"{v!r} for '{vendor_key}'",), unit=unit, label=label)
    return Q.reported(val, unit=unit, as_of=as_of, source_name=fetched.source,
                      source_url=fetched.url, retrieved_at=fetched.retrieved_at,
                      confidence=confidence, payload_path=fetched.payload_path,
                      label=label, note=note)


def build_period(fetched_by_stmt: Mapping[str, tuple[Fetched, Mapping[str, Any]]],
                 field_map: Mapping[str, tuple[str, str]], *, ticker: str,
                 period_end: str, period_type: str, currency: str, schema: str,
                 confidence: str, source_name: str,
                 filing_url: str | None = None,
                 unit_for: Callable[[str], str] | None = None) -> Period:
    """Assemble one Period from several statement payloads.

    ``field_map`` maps canonical field -> (statement key, vendor key).
    """
    p = Period(ticker=ticker, period_end=period_end, period_type=period_type,
               currency=currency, schema=schema, source_name=source_name,
               filing_url=filing_url)
    for canonical, (stmt, vendor_key) in field_map.items():
        if canonical not in p.allowed():
            continue
        unit = unit_for(canonical) if unit_for else currency
        if stmt not in fetched_by_stmt:
            p.set(canonical, Q.null(NOT_FETCHED,
                                    missing=(f"{canonical}: {stmt} not fetched",),
                                    unit=unit, label=canonical))
            continue
        fetched, raw = fetched_by_stmt[stmt]
        p.set(canonical, field_q(fetched, raw, vendor_key, unit=unit,
                                 as_of=period_end, label=canonical,
                                 confidence=confidence))
    return p


def default_unit_for(currency: str) -> Callable[[str], str]:
    share_fields = {"diluted_shares", "basic_shares", "shares_outstanding"}
    ratio_fields = {"cet1_ratio", "licat_ratio"}
    per_share = {"eps_diluted"}

    def _u(name: str) -> str:
        if name in share_fields:
            return "shares"
        if name in ratio_fields:
            return "ratio"
        if name in per_share:
            return f"{currency}/share"
        return currency

    return _u


def safe_call(fn: Callable[[], Any], *, label: str, unit: str = "") -> Any:
    """Turn an upstream failure into a null that names it, never into a default."""
    try:
        return fn()
    except FetchError as exc:
        return Q.null(UPSTREAM_ERROR,
                      missing=(f"{label}: {exc.url} -> {exc.status} {exc.detail[:120]}",),
                      unit=unit, label=label)
    except ProviderUnavailable as exc:
        return Q.null(NOT_FETCHED, missing=(f"{label}: {exc}",), unit=unit, label=label)
