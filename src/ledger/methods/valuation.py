"""Valuation, expectations first.

The headline output of this module is NOT a fair value.  It is a falsifiable
claim about what the current price already assumes.  A fair-value point
estimate invites the user to argue with the model; an implied growth rate
invites them to judge the market's claim, which is the actual job.

Forward DCF exists here only as a scenario tool with user-supplied assumptions.
It never produces a headline number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..model import Company
from ..provenance import (
    MISSING_INPUT, UNDEFINED_MATH, Q, add, combine, mean, normalized, ratio, sub,
)
from .cashflow import cash_tax_rate, free_cash_flow
from .returns import nopat


# ---------------------------------------------------------------------------
# enterprise value
# ---------------------------------------------------------------------------


def market_cap(co: Company) -> Q:
    mc = co.market_q("market_cap")
    if mc.ok:
        return mc
    return combine("market_cap", "last_close * diluted_shares",
                   co.market_q("last_close").unit,
                   lambda p, s: p * s,
                   {"p": co.market_q("last_close"), "s": co.fy("diluted_shares", 0)})


def enterprise_value(co: Company) -> Q:
    """market cap + total debt + minority interest - cash.

    Minority interest is included when disclosed; when it is not, we do not
    assume zero, we compute without it and the formula records the omission.
    """
    debt = co.fy("total_debt", 0)
    cash = co.fy("cash_and_equivalents", 0)
    core = combine("enterprise_value_core",
                   "market_cap + total_debt - cash_and_equivalents",
                   market_cap(co).unit,
                   lambda m, d, c: m + d - c,
                   {"m": market_cap(co), "d": debt, "c": cash})
    mi = co.fy("minority_interest", 0)
    if not mi.ok:
        return core.named("enterprise_value")
    return add("enterprise_value", core.unit, enterprise_value_core=core,
               minority_interest=mi)


def acquirers_multiple(co: Company) -> Q:
    """EV / operating earnings.  Greenblatt's cross-sectional ranking lens."""
    return ratio("acquirers_multiple", "x", enterprise_value(co),
                 co.fy("operating_income", 0),
                 formula="enterprise_value / operating_income")


def ebit_ev_yield(co: Company) -> Q:
    return ratio("ebit_ev_yield", "ratio", co.fy("operating_income", 0),
                 enterprise_value(co), formula="operating_income / enterprise_value")


# ---------------------------------------------------------------------------
# reverse DCF -- the primary lens
# ---------------------------------------------------------------------------


def _pv_of_growth(fcf0: float, g: float, years: int, wacc: float, tg: float) -> float:
    """PV of `years` of growth at g, then a Gordon terminal at tg."""
    pv = 0.0
    cf = fcf0
    for t in range(1, years + 1):
        cf = cf * (1.0 + g)
        pv += cf / (1.0 + wacc) ** t
    terminal = cf * (1.0 + tg) / (wacc - tg)
    pv += terminal / (1.0 + wacc) ** years
    return pv


def _bisect(f: Callable[[float], float], lo: float, hi: float, tol: float,
            max_iter: int = 200) -> float | None:
    flo, fhi = f(lo), f(hi)
    if flo * fhi > 0:
        return None
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        fm = f(mid)
        if abs(fm) < tol or (hi - lo) / 2.0 < tol:
            return mid
        if flo * fm <= 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2.0


@dataclass(frozen=True)
class ImpliedExpectation:
    """What the market is currently pricing in, as a falsifiable claim."""

    fade_years: int
    implied_growth: Q
    wacc: Q
    terminal_growth: float
    base_fcf: Q
    claim: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fade_years": self.fade_years,
            "implied_growth": self.implied_growth.to_dict(),
            "wacc": self.wacc.to_dict(),
            "terminal_growth": self.terminal_growth,
            "base_fcf": self.base_fcf.to_dict(),
            "claim": self.claim,
        }


def reverse_dcf(co: Company, wacc: Q, *, fade_years: int, terminal_growth: float,
                bounds: tuple[float, float] = (-0.50, 1.00),
                tol: float = 1e-6, base_fcf: Q | None = None) -> ImpliedExpectation:
    """Solve for the FCF growth rate the current enterprise value implies.

    Output shape: "the market expects 7.4% FCF growth for 10 years".  The user's
    job is to judge that claim.
    """
    fcf = base_fcf if base_fcf is not None else free_cash_flow(co, 0)
    ev = enterprise_value(co)

    def _null(reason: str, missing: tuple[str, ...]) -> ImpliedExpectation:
        return ImpliedExpectation(
            fade_years=fade_years,
            implied_growth=Q.null(reason, missing=missing, unit="ratio",
                                  label="reverse_dcf_implied_growth"),
            wacc=wacc, terminal_growth=terminal_growth, base_fcf=fcf,
            claim="insufficient data to state what the market is pricing in",
        )

    if not (fcf.ok and ev.ok and wacc.ok):
        miss = tuple(fcf.missing) + tuple(ev.missing) + tuple(wacc.missing)
        return _null(MISSING_INPUT, miss or ("reverse DCF inputs",))
    if wacc.value <= terminal_growth:
        return _null(UNDEFINED_MATH,
                     (f"wacc {wacc.value:.2%} <= terminal growth "
                      f"{terminal_growth:.2%}; Gordon terminal diverges",))
    if fcf.value <= 0:
        return _null(UNDEFINED_MATH,
                     (f"base FCF is {fcf.value:,.0f}; a reverse DCF on negative "
                      "free cash flow has no economic meaning -- normalize first",))

    g = _bisect(lambda x: _pv_of_growth(fcf.value, x, fade_years, wacc.value,
                                        terminal_growth) - ev.value,
                bounds[0], bounds[1], tol)
    if g is None:
        return _null(UNDEFINED_MATH,
                     (f"no growth rate in [{bounds[0]:.0%}, {bounds[1]:.0%}] "
                      "reproduces the current EV",))

    implied = normalized(
        "reverse_dcf_implied_growth", g, unit="ratio", as_of=ev.as_of,
        basis=[fcf, ev, wacc],
        formula=(f"solve g: PV(FCF0*(1+g)^t, t=1..{fade_years}) + "
                 f"PV(terminal @ {terminal_growth:.1%}) = EV"),
        note=f"terminal growth {terminal_growth:.1%} is an explicit assumption",
    )
    claim = (
        f"At {ev.value:,.0f} {ev.unit} enterprise value, the market is pricing "
        f"{g:.1%} annual free-cash-flow growth for {fade_years} years, then "
        f"{terminal_growth:.1%} in perpetuity, discounted at {wacc.value:.1%}."
    )
    return ImpliedExpectation(fade_years=fade_years, implied_growth=implied,
                              wacc=wacc, terminal_growth=terminal_growth,
                              base_fcf=fcf, claim=claim)


def expectations_gap(implied: Q, compounding: Q) -> Q:
    """Market-implied growth minus the rate the business can self-fund.

    Positive means the price requires more than the business has demonstrated
    it can generate from reinvestment.  This is the number to argue about.
    """
    return sub("expectations_gap", "ratio", implied, compounding)


# ---------------------------------------------------------------------------
# Greenwald EPV
# ---------------------------------------------------------------------------


def normalized_ebit(co: Company, years: int) -> Q:
    """Mean EBIT margin over a full cycle, applied to current revenue.

    Greenwald's point: current-year EBIT is a cyclical accident; the margin
    through a cycle applied to current scale is the durable earnings power.
    """
    margins = [ratio(f"ebit_margin_{i}", "ratio", co.fy("operating_income", i),
                     co.fy("revenue", i)) for i in range(years)]
    m = mean("ebit_margin_normalized", "ratio", margins, min_n=max(3, years // 2))
    return combine("normalized_ebit", "mean_ebit_margin * revenue_t0",
                   co.fy("revenue", 0).unit, lambda mm, r: mm * r,
                   {"mm": m, "r": co.fy("revenue", 0)})


def epv(co: Company, wacc: Q, *, years: int = 7) -> Q:
    """Earnings power value: normalized EBIT x (1 - cash tax rate) / WACC."""
    after_tax = combine("normalized_nopat",
                        "normalized_ebit * (1 - cash_tax_rate)",
                        co.fy("revenue", 0).unit,
                        lambda e, t: e * (1.0 - t),
                        {"e": normalized_ebit(co, years), "t": cash_tax_rate(co, 0)})
    return combine("epv", "normalized_nopat / wacc", after_tax.unit,
                   lambda n, w: None if w == 0 else n / w,
                   {"n": after_tax, "w": wacc})


def reproduction_value(co: Company) -> Q:
    """Asset reproduction value: what a competitor would spend to rebuild.

    Approximated by tangible assets: total assets less goodwill and other
    intangibles.  Goodwill is excluded because an acquirer reproducing the
    business does not reproduce someone else's overpayment.
    """
    return combine("reproduction_value",
                   "total_assets - goodwill - intangibles",
                   co.fy("total_assets", 0).unit,
                   lambda a, g, i: a - g - i,
                   {"a": co.fy("total_assets", 0), "g": co.fy("goodwill", 0),
                    "i": co.fy("intangibles", 0)})


def franchise_value(co: Company, wacc: Q, *, years: int = 7) -> Q:
    """EPV - reproduction value.

    Positive franchise value is the market's estimate of a durable competitive
    advantage: earnings power in excess of what the assets alone could earn.
    """
    return sub("franchise_value", co.fy("total_assets", 0).unit,
               epv(co, wacc, years=years), reproduction_value(co))


def greenwald_triplet(co: Company, wacc: Q, *, years: int = 7) -> dict[str, Q]:
    """All three, always reported together -- EPV alone is misleading."""
    return {
        "epv": epv(co, wacc, years=years),
        "reproduction_value": reproduction_value(co),
        "franchise_value": franchise_value(co, wacc, years=years),
    }


# ---------------------------------------------------------------------------
# forward DCF -- scenario tool only
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """A user-supplied forward case.  Every field is an explicit assumption."""

    name: str
    growth: float
    years: int
    terminal_growth: float
    wacc: float
    probability: float | None = None


def forward_dcf(co: Company, scenario: Scenario, *, base_fcf: Q | None = None) -> Q:
    """A scenario value.  NEVER a headline.

    Output is tagged method='normalized' with the full assumption set in the
    formula string, so it can never be mistaken for a computed fact.
    """
    fcf = base_fcf if base_fcf is not None else free_cash_flow(co, 0)
    if not fcf.ok:
        return Q.null(fcf.reason or MISSING_INPUT, missing=fcf.missing,
                      unit=fcf.unit, label=f"forward_dcf_{scenario.name}")
    if scenario.wacc <= scenario.terminal_growth:
        return Q.null(UNDEFINED_MATH,
                      missing=(f"scenario {scenario.name}: wacc <= terminal growth",),
                      label=f"forward_dcf_{scenario.name}")
    ev = _pv_of_growth(fcf.value, scenario.growth, scenario.years,
                       scenario.wacc, scenario.terminal_growth)
    return normalized(
        f"forward_dcf_{scenario.name}", ev, unit=fcf.unit, as_of=fcf.as_of,
        basis=fcf,
        formula=(f"scenario '{scenario.name}': g={scenario.growth:.2%} for "
                 f"{scenario.years}y, tg={scenario.terminal_growth:.2%}, "
                 f"wacc={scenario.wacc:.2%}"),
        note="SCENARIO OUTPUT -- user assumptions, not a computed fair value",
    )
