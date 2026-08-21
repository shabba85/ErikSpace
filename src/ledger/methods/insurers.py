"""Insurers.

Under IFRS 17 an insurer's reported book value understates the economics: the
contractual service margin is unearned profit already written and sitting on
the balance sheet as a liability.  Book value plus CSM is the closer analogue to
what a bank's tangible book means.

Like banks, insurers get their own schema and never touch industrial metrics.
"""

from __future__ import annotations

from typing import Any

from ..model import Company
from ..provenance import MISSING_INPUT, Q, add, combine, ratio, sub


def book_value_plus_csm(co: Company, i: int = 0) -> Q:
    """Book value + contractual service margin.

    Reported together and never netted into a single 'adjusted book value' that
    obscures which part is earned and which is contracted-but-unearned.
    """
    return add("book_value_plus_csm", co.fy("book_value", i).unit,
               book_value=co.fy("book_value", i),
               contractual_service_margin=co.fy("contractual_service_margin", i))


def csm_growth(co: Company, years: int = 1) -> Q:
    """Growth in the CSM: new profitable business written, before it is earned.

    This is the closest thing an insurer has to a forward order book.
    """
    return combine("csm_growth", f"(csm_t0 / csm_t-{years}) - 1", "ratio",
                   lambda a, b: None if b == 0 else a / b - 1.0,
                   {"a": co.fy("contractual_service_margin", 0),
                    "b": co.fy("contractual_service_margin", years)})


def licat_ratio(co: Company, i: int = 0) -> Q:
    """OSFI's Life Insurance Capital Adequacy Test ratio, as reported.

    Never derived: LICAT is a regulatory calculation we cannot reconstruct from
    public statements.  If it is not disclosed, it is null.
    """
    return co.fy("licat_ratio", i).named("licat_ratio")


def core_roe(co: Company, i: int = 0) -> Q:
    """Core earnings on average common equity.

    Core, not reported: an insurer's reported net income swings on mark-to-market
    of the investment portfolio and actuarial assumption changes, neither of
    which is the underwriting business.
    """
    avg_eq = combine("average_common_equity", "(equity_t0 + equity_t1) / 2",
                     co.fy("common_equity", i).unit,
                     lambda a, b: (a + b) / 2.0,
                     {"a": co.fy("common_equity", i), "b": co.fy("common_equity", i + 1)})
    return ratio("core_roe", "ratio", co.fy("core_earnings", i), avg_eq,
                 formula="core_earnings / average common equity")


def insurer_metrics(co: Company) -> dict[str, Any]:
    if co.schema != "insurer":
        raise ValueError(f"{co.ticker} is schema {co.schema!r}, not an insurer")
    return {
        "book_value": co.fy("book_value", 0),
        "contractual_service_margin": co.fy("contractual_service_margin", 0),
        "book_value_plus_csm": book_value_plus_csm(co, 0),
        "csm_growth": csm_growth(co, 1),
        "licat_ratio": licat_ratio(co, 0),
        "core_roe": core_roe(co, 0),
    }
