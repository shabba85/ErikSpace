"""Cash-flow truth: owner earnings, maintenance capex, FCF conversion."""

from __future__ import annotations

from ..model import Company
from ..provenance import (
    INSUFFICIENT_HISTORY, MISSING_INPUT, Q, Range, add, combine, mean, ratio,
    stdev, sub,
)


def free_cash_flow(co: Company, i: int = 0) -> Q:
    """CFO less all capex.  The unadjusted figure -- no add-backs, no 'adjusted
    FCF' the company would prefer we use."""
    return combine(
        "free_cash_flow", "cash_from_operations - abs(capital_expenditure)",
        co.fy("revenue", i).unit or "",
        lambda cfo, capex: cfo - abs(capex),
        {"cfo": co.fy("cash_from_operations", i), "capex": co.fy("capital_expenditure", i)},
    )


def maintenance_capex_da_proxy(co: Company, i: int = 0) -> Q:
    """Estimate 1: maintenance capex approximated by depreciation & amortization.

    Crude but unbiased in a steady state, and it is what Buffett's original
    formulation leans on.  It understates in inflationary periods, which is why
    it is reported as one end of a band and never alone.
    """
    da = co.fy("depreciation_amortization_cf", i)
    if not da.ok:
        da = co.fy("depreciation_amortization", i)
    return da.named("maintenance_capex_da_proxy")


def maintenance_capex_ppe_trend(co: Company, i: int = 0, lookback: int = 5) -> Q:
    """Estimate 2: Greenwald's PP&E-to-sales method.

    Growth capex is the PP&E required to support the *increment* in sales, at
    the company's own historical PP&E intensity.  What is left of total capex is
    maintenance.

        ppe_intensity = mean(ppe_net / revenue) over `lookback` prior years
        growth_capex  = (revenue_t - revenue_t-1) * ppe_intensity
        maint_capex   = capex - growth_capex

    Where sales shrank, growth capex is floored at zero: a company cannot
    'recover' capex by shrinking, and letting it go negative would inflate
    maintenance capex and understate owner earnings.
    """
    intens = []
    for k in range(i + 1, i + 1 + lookback):
        intens.append(ratio(f"ppe_intensity_{k}", "ratio",
                            co.fy("ppe_net", k), co.fy("revenue", k)))
    avg_int = mean("ppe_intensity_mean", "ratio", intens, min_n=3)
    if not avg_int.ok:
        return Q.null(avg_int.reason or INSUFFICIENT_HISTORY, missing=avg_int.missing,
                      label="maintenance_capex_ppe_trend")

    d_rev = sub("revenue_change", co.fy("revenue", i).unit, co.fy("revenue", i),
                co.fy("revenue", i + 1))
    growth_capex = combine(
        "growth_capex", "max(0, revenue_change) * ppe_intensity_mean",
        d_rev.unit, lambda d, k: max(0.0, d) * k,
        {"d": d_rev, "k": avg_int},
    )
    return combine(
        "maintenance_capex_ppe_trend", "abs(capital_expenditure) - growth_capex",
        d_rev.unit,
        lambda capex, growth: abs(capex) - growth,
        {"capex": co.fy("capital_expenditure", i), "growth": growth_capex},
    )


def maintenance_capex(co: Company, i: int = 0) -> Range:
    """Both estimates as a band.  A point estimate here would be false
    precision: maintenance capex is not a disclosed number."""
    a = maintenance_capex_da_proxy(co, i)
    b = maintenance_capex_ppe_trend(co, i)
    if a.ok and b.ok:
        low, high = (a, b) if a.value <= b.value else (b, a)
        lm = "D&A proxy" if low is a else "PP&E-to-sales trend"
        hm = "PP&E-to-sales trend" if low is a else "D&A proxy"
    else:
        low, high, lm, hm = a, b, "D&A proxy", "PP&E-to-sales trend"
    return Range(low=low, high=high, label="maintenance_capex",
                 method_low=lm, method_high=hm)


def owner_earnings(co: Company, i: int = 0) -> Range:
    """Buffett's owner earnings, reported as a band.

        net income + D&A + other non-cash charges - maintenance capex

    Because maintenance capex is a range, owner earnings is a range.  The low
    end uses the *higher* maintenance capex estimate.
    """
    da = maintenance_capex_da_proxy(co, i)          # the D&A add-back itself
    non_cash = co.fy("other_non_cash_charges", i)
    if not non_cash.ok:
        # A company with no disclosed 'other non-cash charges' line has none we
        # can verify.  We add zero *as a fetched absence*, not as a guess: the
        # add-back is simply omitted and the formula records that.
        base = add("owner_earnings_base", co.fy("net_income", i).unit,
                   net_income=co.fy("net_income", i), depreciation_amortization=da)
        formula_note = "other_non_cash_charges not disclosed; omitted from add-back"
    else:
        base = add("owner_earnings_base", co.fy("net_income", i).unit,
                   net_income=co.fy("net_income", i), depreciation_amortization=da,
                   other_non_cash_charges=non_cash)
        formula_note = None

    mc = maintenance_capex(co, i)
    lo = combine("owner_earnings_low", "owner_earnings_base - maintenance_capex_high",
                 base.unit, lambda b, m: b - m, {"b": base, "m": mc.high},
                 note=formula_note)
    hi = combine("owner_earnings_high", "owner_earnings_base - maintenance_capex_low",
                 base.unit, lambda b, m: b - m, {"b": base, "m": mc.low},
                 note=formula_note)
    return Range(low=lo, high=hi, label="owner_earnings",
                 method_low=f"maint capex via {mc.method_high}",
                 method_high=f"maint capex via {mc.method_low}")


def fcf_conversion(co: Company, i: int = 0) -> Q:
    """FCF / net income for one year."""
    return ratio("fcf_conversion", "ratio", free_cash_flow(co, i),
                 co.fy("net_income", i), formula="free_cash_flow / net_income")


def fcf_conversion_stats(co: Company, years: int = 5) -> dict[str, Q]:
    """5-year mean and dispersion of FCF conversion.

    Persistent conversion below ~0.8 is an earnings-quality flag: reported
    profit that does not become cash.  The threshold lives in config, not here;
    this function reports the statistics and lets the caller judge.
    """
    series = [fcf_conversion(co, i) for i in range(years)]
    m = mean("fcf_conversion_mean", "ratio", series, min_n=3)
    s = stdev("fcf_conversion_stdev", "ratio", series, min_n=3)
    n_ok = sum(1 for q in series if q.ok)
    return {
        "fcf_conversion_mean": m,
        "fcf_conversion_stdev": s,
        "fcf_conversion_years_used": Q(
            value=n_ok, unit="count", as_of=m.as_of, prov=m.prov,
            label="fcf_conversion_years_used",
        ) if m.ok else Q.null(INSUFFICIENT_HISTORY, missing=m.missing,
                              label="fcf_conversion_years_used"),
        "fcf_conversion_series": series,
    }


def cash_tax_rate(co: Company, i: int = 0) -> Q:
    """Cash taxes paid / pretax income.

    Deliberately NOT the statutory rate and NOT the book effective rate.  A
    company with large deferred tax balances shows a book rate that flatters
    NOPAT; the cash rate is what actually leaves the business.
    """
    return ratio("cash_tax_rate", "ratio", co.fy("cash_taxes_paid", i),
                 co.fy("pretax_income", i),
                 formula="cash_taxes_paid / pretax_income")
