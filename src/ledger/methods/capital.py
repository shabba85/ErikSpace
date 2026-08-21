"""Capital allocation scorecard.

Buyback yield alone is a vanity metric.  What matters is the price paid: a
company retiring stock above intrinsic value is transferring wealth from
continuing holders to sellers, and reporting that as "capital returned" inverts
the sign of what actually happened.  So every buyback figure here is paired
with the multiple paid at the time of repurchase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..model import Company
from ..provenance import (
    INSUFFICIENT_HISTORY, MISSING_INPUT, Q, add, combine, mean, normalized, ratio,
    sub,
)
from .cashflow import free_cash_flow, owner_earnings


def buyback_yield(co: Company, i: int = 0) -> Q:
    """Net repurchases (of issuance) as a share of market cap."""
    from .valuation import market_cap

    net = combine("net_buyback", "abs(share_repurchase) - abs(share_issuance)",
                  co.fy("share_repurchase", i).unit,
                  lambda r, s: abs(r) - abs(s),
                  {"r": co.fy("share_repurchase", i), "s": co.fy("share_issuance", i)})
    return ratio("buyback_yield", "ratio", net, market_cap(co),
                 formula="(repurchases - issuance) / market cap")


def average_repurchase_price(co: Company, i: int = 0) -> Q:
    """Cash spent on repurchases divided by shares actually retired.

    Derived from the share count movement rather than from a disclosed average
    price, because the disclosed figure (where it exists) often excludes
    issuance under option plans and so flatters the number.
    """
    retired = sub("shares_retired", "shares", co.fy("diluted_shares", i + 1),
                  co.fy("diluted_shares", i))
    return combine(
        "average_repurchase_price", "abs(share_repurchase) / shares_retired",
        co.fy("share_repurchase", i).unit,
        lambda cash, n: None if n <= 0 else abs(cash) / n,
        {"cash": co.fy("share_repurchase", i), "n": retired},
        note=("null when the share count did not fall: the company issued more "
              "than it retired, so there is no meaningful repurchase price"),
    )


def repurchase_multiple_paid(co: Company, i: int = 0, *, years: int = 7) -> dict[str, Q]:
    """The multiple the company paid for its own stock, against two yardsticks.

    Against owner earnings per share, and against EPV per share.  A repurchase
    multiple above the EPV multiple is value destruction dressed as a return of
    capital, and this is the pair of numbers that shows it.
    """
    px = average_repurchase_price(co, i)
    shares = co.fy("diluted_shares", i)

    oe = owner_earnings(co, i)
    oe_ps_low = ratio("owner_earnings_per_share_low", "ratio", oe.low, shares)
    oe_ps_high = ratio("owner_earnings_per_share_high", "ratio", oe.high, shares)

    return {
        "average_repurchase_price": px,
        "repurchase_multiple_of_owner_earnings_low": ratio(
            "repurchase_multiple_of_owner_earnings_low", "x", px, oe_ps_high,
            formula="avg repurchase price / owner earnings per share (high estimate)"),
        "repurchase_multiple_of_owner_earnings_high": ratio(
            "repurchase_multiple_of_owner_earnings_high", "x", px, oe_ps_low,
            formula="avg repurchase price / owner earnings per share (low estimate)"),
    }


def buyback_value_verdict(repurchase_price: Q, epv_per_share: Q) -> Q:
    """Signed value added or destroyed per share repurchased.

    (EPV per share - price paid).  Negative means each share retired destroyed
    that much value for the holders who stayed.
    """
    return sub("buyback_value_per_share_retired", epv_per_share.unit or "",
               epv_per_share, repurchase_price)


def dividend_coverage_by_fcf(co: Company, i: int = 0) -> Q:
    """FCF / dividends paid.

    By free cash flow, never by EPS.  A payout ratio computed on earnings tells
    you whether the accountants think the dividend is covered; this tells you
    whether the bank account does.
    """
    return combine("dividend_coverage_by_fcf", "free_cash_flow / abs(dividends_paid)",
                   "x",
                   lambda f, d: None if d == 0 else f / abs(d),
                   {"f": free_cash_flow(co, i), "d": co.fy("dividends_paid", i)})


def dividend_payout_trend(co: Company, years: int = 5) -> list[Q]:
    return [ratio(f"payout_by_fcf_{i}", "ratio",
                  combine(f"abs_div_{i}", "abs(dividends_paid)",
                          co.fy("dividends_paid", i).unit, lambda d: abs(d),
                          {"d": co.fy("dividends_paid", i)}),
                  free_cash_flow(co, i)) for i in range(years)]


def dividend_yield(co: Company, i: int = 0) -> Q:
    from .valuation import market_cap

    return combine("dividend_yield", "abs(dividends_paid) / market_cap", "ratio",
                   lambda d, m: None if m == 0 else abs(d) / m,
                   {"d": co.fy("dividends_paid", i), "m": market_cap(co)})


def yield_quality(co: Company, i: int = 0) -> dict[str, Q]:
    """Sustainable yield versus distressed yield.

    A high yield is either a cheap stock or a dividend about to be cut, and the
    distinguishing evidence is FCF coverage and the leverage behind it -- not
    the yield itself.  We report the evidence; we do not label the stock.
    """
    return {
        "dividend_yield": dividend_yield(co, i),
        "dividend_coverage_by_fcf": dividend_coverage_by_fcf(co, i),
        "net_debt_to_ebitda": _net_debt_to_ebitda(co, i),
    }


def _net_debt_to_ebitda(co: Company, i: int = 0) -> Q:
    from .cyclical import ebitda, net_debt

    return ratio("net_debt_to_ebitda", "x", net_debt(co, i), ebitda(co, i))


# ---------------------------------------------------------------------------
# debt
# ---------------------------------------------------------------------------


@dataclass
class MaturityLadder:
    """Debt maturities by year with the coupon on each tranche."""

    rungs: list[dict[str, Q]] = field(default_factory=list)
    weighted_average_coupon: Q = field(
        default_factory=lambda: Q.null(MISSING_INPUT,
                                       missing=("debt maturity schedule",),
                                       label="weighted_average_coupon"))
    refinancing_gap: Q = field(
        default_factory=lambda: Q.null(MISSING_INPUT,
                                       missing=("current refinancing rate",),
                                       label="refinancing_gap"))
    total: Q = field(default_factory=lambda: Q.null(MISSING_INPUT,
                                                    missing=("debt maturity schedule",),
                                                    label="total_scheduled_debt"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "rungs": [{k: v.to_dict() for k, v in sorted(r.items())} for r in self.rungs],
            "weighted_average_coupon": self.weighted_average_coupon.to_dict(),
            "refinancing_gap": self.refinancing_gap.to_dict(),
            "total_scheduled_debt": self.total.to_dict(),
            "note": ("refinancing_gap = current market refinancing rate - weighted "
                     "average coupon. Positive means every maturity that rolls "
                     "raises interest expense."),
        }


def debt_maturity_ladder(co: Company, current_refi_rate: Q) -> MaturityLadder:
    """Weighted-average coupon against the rate the company would pay today.

    ``current_refi_rate`` must be a fetched quantity -- a GoC yield from the Bank
    of Canada plus a disclosed spread, not a guess at where the company's paper
    trades.
    """
    rungs = co.debt_maturities
    if not rungs:
        return MaturityLadder()

    amounts = {f"y{i}": r.get("amount", Q.null(MISSING_INPUT, missing=("amount",)))
               for i, r in enumerate(rungs)}
    total = add("total_scheduled_debt",
                next((q.unit for q in amounts.values() if q.ok), ""), **amounts)

    weighted: dict[str, Q] = {}
    for i, r in enumerate(rungs):
        amt = r.get("amount", Q.null(MISSING_INPUT, missing=(f"rung {i} amount",)))
        cpn = r.get("coupon", Q.null(MISSING_INPUT, missing=(f"rung {i} coupon",)))
        weighted[f"w{i}"] = combine(f"weighted_coupon_{i}", "amount * coupon", "",
                                    lambda a, c: a * c, {"a": amt, "c": cpn})
    num = add("weighted_coupon_sum", "", **weighted)
    wac = ratio("weighted_average_coupon", "ratio", num, total,
                formula="sum(amount * coupon) / sum(amount)")
    gap = sub("refinancing_gap", "ratio", current_refi_rate, wac)
    return MaturityLadder(rungs=rungs, weighted_average_coupon=wac,
                          refinancing_gap=gap, total=total)


def capital_allocation_scorecard(co: Company, *, epv_per_share: Q | None = None,
                                 current_refi_rate: Q | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "buyback_yield": buyback_yield(co, 0),
        "share_count_cagr_5y": _share_cagr(co),
    }
    out.update(repurchase_multiple_paid(co, 0))
    out.update(yield_quality(co, 0))
    if epv_per_share is not None:
        out["buyback_value_per_share_retired"] = buyback_value_verdict(
            out["average_repurchase_price"], epv_per_share)
    if current_refi_rate is not None:
        out["debt_maturity_ladder"] = debt_maturity_ladder(co, current_refi_rate)
    return out


def _share_cagr(co: Company) -> Q:
    from .forensics import share_count_trend

    return share_count_trend(co, 5)
