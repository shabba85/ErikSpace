"""Cyclical normalization for Energy and Materials.

Scoring a cyclical on TTM earnings is the most reliable way to buy the top and
sell the bottom.  At the peak of a commodity cycle a producer's trailing P/E
looks like a bargain precisely because the earnings are unrepeatable; at the
trough it looks ruinous for the same reason inverted.

So for Energy and Materials the system computes earnings at a user-set
mid-cycle deck and reports BOTH figures side by side, with the difference
labelled explicitly as cyclical distortion.

The bridge from reported to mid-cycle uses the company's OWN disclosed price
sensitivities from the MD&A -- not a sector beta, not a regression, not an
assumption about how levered this producer is to crude.  If a company does not
disclose its sensitivity, mid-cycle earnings return null and say so.  That is
the correct outcome: without the sensitivity we genuinely do not know.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config import Deck
from ..model import Company
from ..provenance import (
    DECK_NOT_SET, MISSING_INPUT, NOT_APPLICABLE, Q, add, combine, normalized,
    ratio, sub,
)

CYCLICAL_SECTORS = ("Energy", "Materials")

#: deck commodity -> the sensitivity key a company must disclose for it.
#: The sensitivity is "change in annual cash flow per one-unit change in price".
SENSITIVITY_KEYS = {
    "wti": "cash_flow_per_usd_wti",
    "wcs_differential": "cash_flow_per_usd_wcs_differential",
    "gold": "cash_flow_per_usd_gold_oz",
    "aeco": "cash_flow_per_usd_aeco_mmbtu",
    "henry_hub": "cash_flow_per_usd_henry_hub_mmbtu",
    "copper": "cash_flow_per_usd_copper_lb",
    "uranium_spot": "cash_flow_per_usd_uranium_spot_lb",
    "uranium_term": "cash_flow_per_usd_uranium_term_lb",
    "potash": "cash_flow_per_usd_potash_tonne",
    "met_coal": "cash_flow_per_usd_met_coal_tonne",
    "lithium": "cash_flow_per_usd_lithium_tonne",
}


def is_cyclical(co: Company) -> bool:
    return co.sector in CYCLICAL_SECTORS


@dataclass
class MidCycleBridge:
    """The full walk from reported to mid-cycle, line by line."""

    reported: Q
    mid_cycle: Q
    distortion: Q
    legs: dict[str, Q] = field(default_factory=dict)
    deck_header: dict[str, Any] = field(default_factory=dict)
    unpriced: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reported": self.reported.to_dict(),
            "mid_cycle": self.mid_cycle.to_dict(),
            "cyclical_distortion": self.distortion.to_dict(),
            "bridge_legs": {k: v.to_dict() for k, v in sorted(self.legs.items())},
            "deck": self.deck_header,
            "commodities_without_sensitivity_or_deck_price": sorted(self.unpriced),
            "note": ("Mid-cycle figures are NORMALIZED to user deck assumptions, "
                     "not observed. Reported and mid-cycle are always shown "
                     "together; the difference is cyclical distortion, not alpha."),
        }


def mid_cycle_bridge(
    co: Company, deck: Deck, *, base: Q, realized: dict[str, Q],
    commodities: Iterable[str], today=None,
) -> MidCycleBridge:
    """Walk `base` earnings to a mid-cycle deck.

        mid_cycle = base + SUM_c (deck_price_c - realized_price_c) * sensitivity_c

    ``realized`` holds the *fetched* average benchmark price the company
    actually earned against over the period (EIA for WTI, etc.).  Both terms of
    every leg are real: one fetched price, one user assumption, one disclosed
    sensitivity.
    """
    legs: dict[str, Q] = {}
    unpriced: list[str] = []

    for c in commodities:
        deck_q = deck.q(c, today)
        real_q = realized.get(c, Q.null(MISSING_INPUT,
                                        missing=(f"realized {c} price for period",),
                                        label=f"realized_{c}"))
        sens = co.sensitivities.get(
            SENSITIVITY_KEYS.get(c, c),
            Q.null(MISSING_INPUT,
                   missing=(f"{co.ticker} does not disclose a {c} price "
                            "sensitivity in its MD&A",),
                   label=f"sensitivity_{c}"))
        if not (deck_q.ok and sens.ok):
            unpriced.append(c)
        delta = sub(f"{c}_price_delta", deck_q.unit, deck_q, real_q)
        legs[f"{c}_bridge"] = combine(
            f"{c}_bridge", f"(deck_{c} - realized_{c}) * sensitivity_{c}",
            base.unit, lambda d, s: d * s, {"d": delta, "s": sens},
            note=f"deck {c} is a user assumption; realized {c} is fetched",
        )

    if not legs:
        null = Q.null(NOT_APPLICABLE,
                      missing=("no commodity exposure configured for this name",),
                      label="mid_cycle")
        return MidCycleBridge(reported=base, mid_cycle=null, distortion=null,
                              deck_header=deck.header(today), unpriced=unpriced)

    total_bridge = add("mid_cycle_bridge_total", base.unit, **legs)
    mid = add("mid_cycle", base.unit, reported=base,
              mid_cycle_bridge_total=total_bridge)
    if mid.ok:
        mid = normalized(
            "mid_cycle", mid.value, unit=mid.unit, as_of=mid.as_of,
            basis=[base, total_bridge],
            formula=f"reported + SUM (deck_price - realized_price) * disclosed sensitivity",
            note=f"deck v{deck.version}; " + ("; ".join(deck.warnings(today)) or "deck within age tolerance"),
        )
    dist = sub("cyclical_distortion", base.unit, base, mid)
    return MidCycleBridge(reported=base, mid_cycle=mid, distortion=dist, legs=legs,
                          deck_header=deck.header(today), unpriced=unpriced)


def dual_multiple(label: str, ev: Q, reported: Q, mid_cycle: Q) -> dict[str, Q]:
    """TTM and mid-cycle multiples, always side by side.

    Reporting only one of these for a cyclical is the error this function exists
    to prevent.
    """
    return {
        f"{label}_ttm": ratio(f"{label}_ttm", "x", ev, reported),
        f"{label}_mid_cycle": ratio(f"{label}_mid_cycle", "x", ev, mid_cycle),
    }


def reserve_life_index(co: Company) -> Q:
    """Proved reserves / annual production, in years.

    How long the company can produce before it must replace what it pumps.  A
    short RLI means the reported reserve base is a wasting asset and the capex
    line understates what is required to stand still.
    """
    return ratio("reserve_life_index", "years",
                 co.reserves.get("proved_reserves",
                                 Q.null(MISSING_INPUT,
                                        missing=("proved reserves (from MD&A / "
                                                 "annual information form)",),
                                        label="proved_reserves")),
                 co.reserves.get("annual_production",
                                 Q.null(MISSING_INPUT,
                                        missing=("annual production",),
                                        label="annual_production")),
                 formula="proved reserves / annual production")


def corporate_breakeven_wti(co: Company, realized_wti: Q) -> Q:
    """The WTI price at which cash flow just covers capex plus the dividend.

        breakeven = realized_wti + (capex + dividends - CFO) / sensitivity

    This is the number that tells you whether the dividend is funded by the
    business or by the commodity.  Below the breakeven the company is borrowing
    to pay you your own money back.
    """
    sens = co.sensitivities.get(
        SENSITIVITY_KEYS["wti"],
        Q.null(MISSING_INPUT,
               missing=(f"{co.ticker} WTI cash-flow sensitivity (MD&A)",),
               label="sensitivity_wti"))
    shortfall = combine(
        "funding_shortfall", "abs(capex) + abs(dividends) - cash_from_operations",
        co.fy("cash_from_operations", 0).unit,
        lambda c, d, o: abs(c) + abs(d) - o,
        {"c": co.fy("capital_expenditure", 0), "d": co.fy("dividends_paid", 0),
         "o": co.fy("cash_from_operations", 0)},
    )
    return combine(
        "corporate_breakeven_wti",
        "realized_wti + (capex + dividends - CFO) / wti_sensitivity", "USD/bbl",
        lambda w, s, g: None if s == 0 else w + g / s,
        {"w": realized_wti, "s": sens, "g": shortfall},
    )


def net_debt(co: Company, i: int = 0) -> Q:
    return combine("net_debt", "total_debt - cash_and_equivalents",
                   co.fy("total_debt", i).unit, lambda d, c: d - c,
                   {"d": co.fy("total_debt", i),
                    "c": co.fy("cash_and_equivalents", i)})


def net_debt_to_mid_cycle_ebitda(co: Company, mid_cycle_ebitda: Q) -> Q:
    """Leverage measured against normalized, not peak, cash flow.

    Leverage against TTM EBITDA at a cycle peak is how energy balance sheets get
    described as conservative right before they are not.
    """
    return ratio("net_debt_to_mid_cycle_ebitda", "x", net_debt(co, 0),
                 mid_cycle_ebitda,
                 formula="net debt / mid-cycle EBITDA (deck-normalized)")


def ebitda(co: Company, i: int = 0) -> Q:
    return add("ebitda", co.fy("operating_income", i).unit,
               operating_income=co.fy("operating_income", i),
               depreciation_amortization_cf=co.fy("depreciation_amortization_cf", i))
