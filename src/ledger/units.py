"""Unit vocabulary.

Units are compared, never converted silently.  A currency mismatch is a bug we
want to surface, not paper over: a Canadian issuer reporting in USD (many
energy and materials names do) must be handled explicitly, with an FX rate that
is itself a fetched, provenance-carrying quantity.
"""

from __future__ import annotations

from .provenance import Q, MISSING_INPUT, combine

CAD = "CAD"
USD = "USD"
SHARES = "shares"
RATIO = "ratio"
PCT = "pct"
YEARS = "years"
BBL = "bbl"
BOE = "boe"
USD_PER_BBL = "USD/bbl"
USD_PER_OZ = "USD/oz"
USD_PER_MMBTU = "USD/MMBtu"
USD_PER_LB = "USD/lb"
USD_PER_TONNE = "USD/tonne"

CURRENCIES = {CAD, USD}


class UnitMismatch(ValueError):
    pass


def same_currency(*qs: Q) -> str | None:
    """Return the shared currency of populated inputs, or raise on a mismatch."""
    units = {q.unit for q in qs if q.ok and q.unit in CURRENCIES}
    if len(units) > 1:
        raise UnitMismatch(f"mixed currencies: {sorted(units)}")
    return next(iter(units), None)


def convert(q: Q, to: str, fx: Q) -> Q:
    """Convert a currency quantity using a *fetched* FX rate.

    ``fx`` must be a Q whose unit reads like 'USD/CAD'.  There is no default
    rate and no hardcoded parity: without a fetched fx, the result is null.
    """
    if not q.ok:
        return q
    if q.unit == to:
        return q
    if q.unit not in CURRENCIES or to not in CURRENCIES:
        raise UnitMismatch(f"cannot convert {q.unit!r} -> {to!r}")
    pair = f"{q.unit}/{to}"
    inv = f"{to}/{q.unit}"
    if fx.unit == pair:
        return combine(f"{q.label or 'value'} in {to}", f"value * {pair}", to,
                       lambda value, rate: value * rate, {"value": q, "rate": fx})
    if fx.unit == inv:
        return combine(f"{q.label or 'value'} in {to}", f"value / {inv}", to,
                       lambda value, rate: None if rate == 0 else value / rate,
                       {"value": q, "rate": fx})
    return Q.null(MISSING_INPUT, missing=(f"fx rate {pair}",), unit=to, label=q.label)
