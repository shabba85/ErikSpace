"""Returns on capital.

Incremental ROIC is the single most predictive quality number in this system,
so it gets the most careful treatment: it is computed over a full trailing
window, it refuses to compute on a shrinking capital base (where the ratio is
meaningless rather than merely noisy), and it is surfaced with the reinvestment
rate beside it so the compounding identity is visible.
"""

from __future__ import annotations

from ..model import Company
from ..provenance import (
    INSUFFICIENT_HISTORY, UNDEFINED_MATH, Q, add, combine, ratio, sub,
)
from .cashflow import cash_tax_rate


def nopat(co: Company, i: int = 0) -> Q:
    """Net operating profit after *cash* tax."""
    return combine(
        "nopat", "operating_income * (1 - cash_tax_rate)",
        co.fy("operating_income", i).unit,
        lambda ebit, t: ebit * (1.0 - t),
        {"ebit": co.fy("operating_income", i), "t": cash_tax_rate(co, i)},
    )


def invested_capital(co: Company, i: int = 0) -> Q:
    """total debt + equity - cash - non-operating assets.

    Non-operating assets are subtracted only when the company discloses them.
    If the line is absent we do not assume zero: we compute invested capital
    without it and record that in the formula, because assuming zero would
    overstate invested capital and *understate* ROIC -- an error in the
    conservative direction, but an error we must still name.
    """
    debt = co.fy("total_debt", i)
    if not debt.ok:
        debt = add("total_debt", co.fy("total_equity", i).unit,
                   short_term_debt=co.fy("short_term_debt", i),
                   long_term_debt=co.fy("long_term_debt", i))
    core = combine(
        "invested_capital_core", "total_debt + total_equity - cash_and_equivalents",
        debt.unit, lambda d, e, c: d + e - c,
        {"d": debt, "e": co.fy("total_equity", i), "c": co.fy("cash_and_equivalents", i)},
    )
    noa = co.fy("non_operating_assets", i)
    if not noa.ok:
        # Not disclosed -> compute without it and say so, rather than assume 0.
        return core.named("invested_capital")
    return sub("invested_capital", core.unit, core, noa)


def roic(co: Company, i: int = 0) -> Q:
    """NOPAT / invested capital.  Suppressed entirely for financials upstream."""
    return ratio("roic", "ratio", nopat(co, i), invested_capital(co, i),
                 formula="nopat / invested_capital")


def incremental_roic(co: Company, years: int = 5, *,
                     min_delta_ic_pct: float | None = None) -> Q:
    """Delta NOPAT / delta invested capital over the trailing window.

    Guard: if invested capital barely moved (or shrank), the ratio is not a
    return on incremental capital -- it is a small number divided by a smaller
    one, and it will produce a spectacular and meaningless figure.  We return a
    null saying so instead.  This guard is the difference between a useful
    number and a trap.
    """
    from .forensics import models

    if min_delta_ic_pct is None:
        min_delta_ic_pct = float(
            models()["incremental_roic"]["min_delta_invested_capital_pct"])

    n0, nN = nopat(co, 0), nopat(co, years)
    ic0, icN = invested_capital(co, 0), invested_capital(co, years)

    d_nopat = sub("delta_nopat", n0.unit, n0, nN)
    d_ic = sub("delta_invested_capital", ic0.unit, ic0, icN)
    if not (d_ic.ok and icN.ok):
        return Q.null(d_ic.reason or INSUFFICIENT_HISTORY,
                      missing=(d_ic.missing or icN.missing),
                      unit="ratio", label="incremental_roic")
    if icN.value == 0:
        return Q.null(UNDEFINED_MATH, missing=("base-year invested capital is zero",),
                      unit="ratio", label="incremental_roic")
    if d_ic.value <= 0:
        return Q.null(
            UNDEFINED_MATH,
            missing=(
                f"invested capital fell over {years}y "
                f"({icN.value:,.0f} -> {ic0.value:,.0f}); incremental ROIC is "
                "undefined on a shrinking capital base",
            ),
            unit="ratio", label="incremental_roic",
        )
    if abs(d_ic.value) < abs(icN.value) * min_delta_ic_pct:
        return Q.null(
            UNDEFINED_MATH,
            missing=(
                f"invested capital moved only {d_ic.value / icN.value:.1%} over "
                f"{years}y; below the {min_delta_ic_pct:.0%} floor the ratio is noise",
            ),
            unit="ratio", label="incremental_roic",
        )
    return ratio("incremental_roic", "ratio", d_nopat, d_ic,
                 formula=f"(nopat_t0 - nopat_t-{years}) / (ic_t0 - ic_t-{years})")


def reinvestment_rate(co: Company, i: int = 0) -> Q:
    """(capex - D&A + change in working capital) / NOPAT.

    The fraction of after-tax operating profit ploughed back into the business.
    """
    net_capex = combine(
        "net_capex", "abs(capital_expenditure) - depreciation_amortization_cf",
        co.fy("capital_expenditure", i).unit,
        lambda c, d: abs(c) - d,
        {"c": co.fy("capital_expenditure", i),
         "d": co.fy("depreciation_amortization_cf", i)},
    )
    reinvested = add("reinvested_capital", net_capex.unit, net_capex=net_capex,
                     change_in_working_capital=co.fy("change_in_working_capital", i))
    return ratio("reinvestment_rate", "ratio", reinvested, nopat(co, i),
                 formula="(net capex + change in WC) / nopat")


def intrinsic_compounding_rate(co: Company, years: int = 5) -> Q:
    """reinvestment rate x incremental ROIC.

    The rate at which the business compounds intrinsic value under its own
    steam.  This is the figure to hold against the market-implied growth rate
    that the reverse DCF backs out: the comparison is the whole point.
    """
    return combine(
        "intrinsic_compounding_rate", "reinvestment_rate * incremental_roic",
        "ratio", lambda r, i: r * i,
        {"r": reinvestment_rate(co, 0), "i": incremental_roic(co, years)},
    )
