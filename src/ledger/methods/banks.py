"""Banks.

A generic screen applied to a Canadian bank is not merely imprecise, it is
wrong in a specific and expensive way: it computes a P/E on an entity whose
earnings are a function of provisioning judgement, and it reports an EV that
treats deposits as debt.  In a bank-heavy market like the TSX that error is not
a corner case, it is most of the index.

So banks route here, to a vocabulary of their own.  The metrics that make no
sense for a bank are not computed and set aside -- they are never computed.

The valuation anchor is the P/TBV-on-ROTCE cross-section.  Banks trade on
returns on tangible equity; the residual from that regression is the signal a
P/E screen cannot see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..model import Company
from ..provenance import (
    INSUFFICIENT_HISTORY, MISSING_INPUT, NOT_APPLICABLE, Q, Provenance, add,
    combine, normalized, ratio, sub,
)


def _avg(label: str, a: Q, b: Q) -> Q:
    return combine(label, "(t0 + t1) / 2", a.unit, lambda x, y: (x + y) / 2.0,
                   {"x": a, "y": b})


def tangible_common_equity(co: Company, i: int = 0) -> Q:
    """Common equity less goodwill and other intangibles.

    Regulators and acquirers both look through goodwill; so does this system.
    """
    return combine("tangible_common_equity",
                   "common_equity - goodwill - intangibles",
                   co.fy("common_equity", i).unit,
                   lambda e, g, n: e - g - n,
                   {"e": co.fy("common_equity", i), "g": co.fy("goodwill", i),
                    "n": co.fy("intangibles", i)})


def rotce(co: Company, i: int = 0) -> Q:
    """Return on tangible common equity, on *average* TCE.

    Preferred dividends are subtracted because they are not available to the
    common.  Where the preferred line is absent we do not assume zero -- the
    ratio returns null and names the missing field.
    """
    ni_common = sub("net_income_to_common", co.fy("net_income", i).unit,
                    co.fy("net_income", i), co.fy("preferred_dividends", i))
    avg_tce = _avg("average_tangible_common_equity",
                   tangible_common_equity(co, i), tangible_common_equity(co, i + 1))
    return ratio("rotce", "ratio", ni_common, avg_tce,
                 formula="(net_income - preferred_dividends) / average TCE")


def tangible_book_value_per_share(co: Company, i: int = 0) -> Q:
    return ratio("tangible_book_value_per_share", co.fy("common_equity", i).unit,
                 tangible_common_equity(co, i), co.fy("diluted_shares", i),
                 formula="tangible_common_equity / diluted_shares")


def price_to_tangible_book(co: Company) -> Q:
    return ratio("price_to_tangible_book", "x", co.market_q("last_close"),
                 tangible_book_value_per_share(co, 0),
                 formula="last_close / tangible book value per share")


def cet1_ratio(co: Company, i: int = 0) -> Q:
    """Reported CET1 if disclosed, else CET1 capital / RWA.

    Prefer the reported figure: the regulatory calculation includes deductions
    and transitional adjustments we cannot reconstruct from public statements,
    so a computed CET1 is an approximation and is labelled as derived.
    """
    reported = co.fy("cet1_ratio", i)
    if reported.ok:
        return reported.named("cet1_ratio")
    return ratio("cet1_ratio", "ratio", co.fy("cet1_capital", i),
                 co.fy("risk_weighted_assets", i),
                 formula="cet1_capital / risk_weighted_assets (approximation: "
                         "excludes regulatory deductions)")


def nim(co: Company, i: int = 0) -> Q:
    """Net interest margin on average earning assets."""
    return ratio("net_interest_margin", "ratio", co.fy("net_interest_income", i),
                 co.fy("average_earning_assets", i),
                 formula="net_interest_income / average_earning_assets")


def efficiency_ratio(co: Company, i: int = 0) -> Q:
    """Non-interest expense / total revenue.  Lower is better."""
    rev = co.fy("total_revenue", i)
    if not rev.ok:
        rev = add("total_revenue", co.fy("net_interest_income", i).unit,
                  net_interest_income=co.fy("net_interest_income", i),
                  non_interest_income=co.fy("non_interest_income", i))
    return ratio("efficiency_ratio", "ratio", co.fy("non_interest_expense", i), rev,
                 formula="non_interest_expense / total revenue")


def pcl_ratio(co: Company, i: int = 0) -> Q:
    """Provision for credit losses as a percentage of average gross loans."""
    avg_loans = co.fy("average_gross_loans", i)
    if not avg_loans.ok:
        avg_loans = _avg("average_gross_loans", co.fy("gross_loans", i),
                         co.fy("gross_loans", i + 1))
    return ratio("pcl_to_average_loans", "ratio",
                 co.fy("provision_for_credit_losses", i), avg_loans,
                 formula="provision_for_credit_losses / average gross loans")


def pcl_split(co: Company, i: int = 0) -> dict[str, Q]:
    """Performing vs impaired provisions.

    The split is the tell.  Provisions on *performing* loans are a forward-looking
    management judgement under IFRS 9 and swing with the macro overlay; provisions
    on *impaired* loans are realised credit deterioration.  A bank whose headline
    PCL is rising entirely on performing loans is making a forecast; one whose
    impaired provisions are rising is reporting an outcome.  Conflating them
    misreads the cycle.
    """
    avg_loans = co.fy("average_gross_loans", i)
    if not avg_loans.ok:
        avg_loans = _avg("average_gross_loans", co.fy("gross_loans", i),
                         co.fy("gross_loans", i + 1))
    return {
        "pcl_performing": co.fy("pcl_on_performing_loans", i),
        "pcl_impaired": co.fy("pcl_on_impaired_loans", i),
        "pcl_performing_to_loans": ratio("pcl_performing_to_loans", "ratio",
                                         co.fy("pcl_on_performing_loans", i), avg_loans),
        "pcl_impaired_to_loans": ratio("pcl_impaired_to_loans", "ratio",
                                       co.fy("pcl_on_impaired_loans", i), avg_loans),
    }


def deposit_growth(co: Company, years: int = 1) -> Q:
    return combine("deposit_growth", f"(deposits_t0 / deposits_t-{years}) - 1", "ratio",
                   lambda a, b: None if b == 0 else a / b - 1.0,
                   {"a": co.fy("total_deposits", 0), "b": co.fy("total_deposits", years)})


def deposit_mix(co: Company, i: int = 0) -> dict[str, Q]:
    """Demand / notice / term as shares of total deposits.

    Funding mix is a rate-sensitivity statement: term deposits reprice, demand
    deposits are sticky and cheap.
    """
    total = co.fy("total_deposits", i)
    return {
        f"{k}_share": ratio(f"{k}_share", "ratio", co.fy(f"{k}_deposits", i), total)
        for k in ("demand", "notice", "term")
    }


def loans_to_deposits(co: Company, i: int = 0) -> Q:
    return ratio("loans_to_deposits", "ratio", co.fy("gross_loans", i),
                 co.fy("total_deposits", i))


# ---------------------------------------------------------------------------
# the cross-sectional anchor
# ---------------------------------------------------------------------------


@dataclass
class Regression:
    """OLS of P/TBV on ROTCE across a peer set, with per-name residuals."""

    slope: Q
    intercept: Q
    r_squared: Q
    n: int
    peers: list[str]
    fitted: dict[str, Q] = field(default_factory=dict)
    residual: dict[str, Q] = field(default_factory=dict)
    observed: dict[str, Q] = field(default_factory=dict)
    excluded: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slope": self.slope.to_dict(), "intercept": self.intercept.to_dict(),
            "r_squared": self.r_squared.to_dict(), "n": self.n, "peers": self.peers,
            "observed": {k: v.to_dict() for k, v in sorted(self.observed.items())},
            "fitted": {k: v.to_dict() for k, v in sorted(self.fitted.items())},
            "residual": {k: v.to_dict() for k, v in sorted(self.residual.items())},
            "excluded": self.excluded,
            "interpretation": (
                "Residual = observed P/TBV - P/TBV predicted by this bank's ROTCE. "
                "Positive means the market pays more per unit of tangible return "
                "than the peer line implies; negative means less. The residual is "
                "the signal, not the level."
            ),
        }


def ptbv_rotce_regression(banks: Sequence[Company], *, min_n: int = 4) -> Regression:
    """Regress P/TBV on ROTCE across the peer set and report residuals.

    A bank with a high P/TBV is not expensive if its ROTCE justifies it; a bank
    at 1.2x tangible book is not cheap if it earns 9% on tangible equity.  The
    residual answers the question the multiple alone cannot.

    Names missing either coordinate are excluded and named -- never imputed to
    the peer mean, which would drag every residual toward zero and destroy the
    signal.
    """
    xs: list[float] = []
    ys: list[float] = []
    used: list[str] = []
    obs: dict[str, Q] = {}
    excluded: dict[str, list[str]] = {}
    inputs: list[Q] = []

    for co in banks:
        r, p = rotce(co, 0), price_to_tangible_book(co)
        obs[co.ticker] = p
        if r.ok and p.ok:
            xs.append(r.value)
            ys.append(p.value)
            used.append(co.ticker)
            inputs.extend([r, p])
        else:
            excluded[co.ticker] = sorted(set(list(r.missing) + list(p.missing)))

    n = len(used)
    if n < min_n:
        null = Q.null(INSUFFICIENT_HISTORY,
                      missing=(f"P/TBV~ROTCE regression needs {min_n} peers, "
                               f"have {n}",), unit="ratio")
        return Regression(slope=null, intercept=null, r_squared=null, n=n,
                          peers=used, observed=obs, excluded=excluded)

    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        null = Q.null("undefined_math", missing=("all peers share one ROTCE; "
                                                 "regression is undefined",),
                      unit="ratio")
        return Regression(slope=null, intercept=null, r_squared=null, n=n,
                          peers=used, observed=obs, excluded=excluded)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot else None

    def _mk(label: str, v: float | None) -> Q:
        if v is None:
            return Q.null("undefined_math", missing=(label,), unit="ratio", label=label)
        return normalized(label, v, unit="ratio",
                          as_of=min((q.as_of for q in inputs if q.as_of), default=None),
                          basis=inputs,
                          formula=f"OLS P/TBV ~ ROTCE across {n} peers: {used}")

    reg = Regression(slope=_mk("ptbv_rotce_slope", b),
                     intercept=_mk("ptbv_rotce_intercept", a),
                     r_squared=_mk("ptbv_rotce_r_squared", r2),
                     n=n, peers=used, observed=obs, excluded=excluded)

    for co in banks:
        if co.ticker not in used:
            reg.fitted[co.ticker] = Q.null(
                MISSING_INPUT, missing=excluded.get(co.ticker, ["excluded from fit"]),
                unit="x", label="ptbv_fitted")
            reg.residual[co.ticker] = Q.null(
                MISSING_INPUT, missing=excluded.get(co.ticker, ["excluded from fit"]),
                unit="x", label="ptbv_residual")
            continue
        r = rotce(co, 0)
        fit = combine("ptbv_fitted", "intercept + slope * rotce", "x",
                      lambda i, s, x: i + s * x,
                      {"i": reg.intercept, "s": reg.slope, "x": r})
        reg.fitted[co.ticker] = fit
        reg.residual[co.ticker] = sub("ptbv_residual", "x", obs[co.ticker], fit)
    return reg


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------


def bank_metrics(co: Company) -> dict[str, Any]:
    """The complete bank metric set.

    Note what is absent: no ROIC, no EV, no gross margin, no Altman Z, no
    current ratio, no owner earnings.  Those keys do not appear at all.
    """
    if co.schema != "bank":
        raise ValueError(f"{co.ticker} is schema {co.schema!r}, not a bank")
    out: dict[str, Any] = {
        "rotce": rotce(co, 0),
        "tangible_common_equity": tangible_common_equity(co, 0),
        "tangible_book_value_per_share": tangible_book_value_per_share(co, 0),
        "price_to_tangible_book": price_to_tangible_book(co),
        "cet1_ratio": cet1_ratio(co, 0),
        "net_interest_margin": nim(co, 0),
        "efficiency_ratio": efficiency_ratio(co, 0),
        "pcl_to_average_loans": pcl_ratio(co, 0),
        "deposit_growth": deposit_growth(co, 1),
        "loans_to_deposits": loans_to_deposits(co, 0),
    }
    out.update(pcl_split(co, 0))
    out.update(deposit_mix(co, 0))
    return out
