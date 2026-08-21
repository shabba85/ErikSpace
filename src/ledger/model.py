"""Canonical statement schema.

Every vendor payload is normalized into these containers.  The key property:
a field that was never populated returns ``Q.null(NOT_FETCHED, missing=(name,))``
rather than raising or defaulting.  Deleting an input therefore propagates a
named null all the way to the UI with no special-casing anywhere -- which is
exactly what acceptance test 2 demands.

Financials are deliberately a *separate* schema from industrials.  A bank has
no gross margin, no enterprise value and no invested capital in any meaningful
sense, and the way to guarantee we never print one is to give banks a schema
that has no such field to print.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Literal

from .provenance import NOT_FETCHED, NOT_APPLICABLE, Q

PeriodType = Literal["FY", "Q", "TTM"]

# --- non-financial line items ------------------------------------------------

INCOME_FIELDS = (
    "revenue", "cost_of_revenue", "gross_profit", "sga_expense", "rnd_expense",
    "operating_income", "depreciation_amortization", "interest_expense",
    "pretax_income", "income_tax_expense", "net_income",
    "eps_diluted", "diluted_shares", "basic_shares",
)
BALANCE_FIELDS = (
    "cash_and_equivalents", "short_term_investments", "receivables", "inventory",
    "other_current_assets", "total_current_assets", "ppe_gross", "ppe_net",
    "goodwill", "intangibles", "total_non_current_assets", "total_assets",
    "payables", "other_current_liabilities", "total_current_liabilities",
    "short_term_debt", "long_term_debt", "total_debt", "total_liabilities",
    "retained_earnings", "total_equity", "minority_interest",
    "non_operating_assets",
)
CASHFLOW_FIELDS = (
    "cash_from_operations", "capital_expenditure", "depreciation_amortization_cf",
    "stock_based_compensation", "deferred_income_tax", "change_in_working_capital",
    "cash_taxes_paid", "dividends_paid", "share_repurchase", "share_issuance",
    "cash_from_investing", "cash_from_financing", "other_non_cash_charges",
)
MARKET_FIELDS = ("last_close", "market_cap", "shares_outstanding")

CORE_FIELDS = INCOME_FIELDS + BALANCE_FIELDS + CASHFLOW_FIELDS + MARKET_FIELDS

# --- bank line items (a distinct vocabulary, by design) ----------------------

BANK_FIELDS = (
    "net_interest_income", "non_interest_income", "total_revenue",
    "non_interest_expense", "provision_for_credit_losses",
    "pcl_on_performing_loans", "pcl_on_impaired_loans",
    "average_earning_assets", "average_gross_loans", "gross_loans",
    "total_deposits", "demand_deposits", "notice_deposits", "term_deposits",
    "net_income", "preferred_dividends", "common_equity", "goodwill",
    "intangibles", "total_assets", "cet1_capital", "risk_weighted_assets",
    "cet1_ratio", "diluted_shares", "last_close", "income_tax_expense",
    "pretax_income", "dividends_paid", "share_repurchase",
)

INSURER_FIELDS = (
    "book_value", "contractual_service_margin", "licat_ratio", "core_earnings",
    "net_income", "common_equity", "diluted_shares", "last_close",
    "goodwill", "intangibles",
)

# fields a bank must NEVER produce.  Enforced in code, asserted in tests.
FORBIDDEN_FOR_FINANCIALS = (
    "altman_z", "altman_z_double_prime", "roic", "incremental_roic",
    "enterprise_value", "ev_ebit", "gross_margin", "current_ratio",
    "acquirers_multiple", "ebit_ev_yield", "owner_earnings", "epv",
    "reproduction_value", "franchise_value", "invested_capital", "nopat",
)


@dataclass
class Period:
    """One fiscal period of one company, as normalized line items."""

    ticker: str
    period_end: str
    period_type: PeriodType
    currency: str
    schema: str = "industrial"          # industrial | bank | insurer
    fields: dict[str, Q] = field(default_factory=dict)
    source_name: str = ""
    filing_url: str | None = None

    def allowed(self) -> tuple[str, ...]:
        return {"industrial": CORE_FIELDS, "bank": BANK_FIELDS,
                "insurer": INSURER_FIELDS}[self.schema]

    def set(self, name: str, q: Q) -> None:
        if name not in self.allowed():
            raise KeyError(
                f"{name!r} is not part of the {self.schema!r} schema "
                f"(this is the guard that stops bank/industrial metric bleed)"
            )
        self.fields[name] = q.named(name)

    def get(self, name: str) -> Q:
        """A field that was never populated is a *named* null, not a zero."""
        if name not in self.allowed():
            return Q.not_applicable(
                f"{name} is not applicable to a {self.schema} business model",
                label=name,
            )
        q = self.fields.get(name)
        if q is None:
            return Q.null(NOT_FETCHED, missing=(f"{name} ({self.period_end})",),
                          label=name)
        return q

    __getitem__ = get

    def drop(self, name: str) -> None:
        """Remove a field.  Used by acceptance test 2 to prove null propagation."""
        self.fields.pop(name, None)

    @property
    def populated(self) -> int:
        return sum(1 for q in self.fields.values() if q.ok)

    @property
    def null_count(self) -> int:
        return len(self.allowed()) - self.populated

    def coverage(self) -> dict[str, Any]:
        return {"populated": self.populated, "total": len(self.allowed()),
                "null": self.null_count}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker, "period_end": self.period_end,
            "period_type": self.period_type, "currency": self.currency,
            "schema": self.schema, "source_name": self.source_name,
            "filing_url": self.filing_url,
            "fields": {k: v.to_dict() for k, v in sorted(self.fields.items())},
            "coverage": self.coverage(),
        }


@dataclass
class Company:
    """A company and its period history, newest first."""

    ticker: str
    name: str = ""
    exchange: str = ""
    sector: str = ""
    industry: str = ""
    currency: str = ""
    schema: str = "industrial"
    cik: str | None = None
    annual: list[Period] = field(default_factory=list)
    quarterly: list[Period] = field(default_factory=list)
    market: dict[str, Q] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    sensitivities: dict[str, Q] = field(default_factory=dict)   # from MD&A
    reserves: dict[str, Q] = field(default_factory=dict)
    debt_maturities: list[dict[str, Q]] = field(default_factory=list)
    #: disclosed segment earnings, keyed by the issuer's own segment name.
    #: Used for look-through factor exposure. Never inferred.
    segments: dict[str, Q] = field(default_factory=dict)

    def latest_annual(self) -> Period | None:
        return self.annual[0] if self.annual else None

    def annuals(self, n: int) -> list[Period]:
        return self.annual[:n]

    def market_q(self, name: str) -> Q:
        q = self.market.get(name)
        if q is None:
            return Q.null(NOT_FETCHED, missing=(name,), label=name)
        return q

    def fy(self, name: str, i: int = 0) -> Q:
        """Field ``name`` from the i-th most recent annual period."""
        if i >= len(self.annual):
            from .provenance import INSUFFICIENT_HISTORY
            return Q.null(INSUFFICIENT_HISTORY,
                          missing=(f"{name}: fiscal year -{i} not available",),
                          label=name)
        return self.annual[i].get(name)

    def series(self, name: str, n: int) -> list[Q]:
        return [self.fy(name, i) for i in range(n)]

    def is_financial(self) -> bool:
        return self.schema in ("bank", "insurer")

    def coverage(self) -> dict[str, Any]:
        p = self.latest_annual()
        return p.coverage() if p else {"populated": 0, "total": 0, "null": 0}


def ttm(periods: Iterable[Period], name: str) -> Q:
    """Trailing-twelve-month sum of a flow item over four quarters.

    Returns a null naming the shortfall if fewer than four quarters are present
    -- never a partial sum annualized by a guessed factor.
    """
    from .provenance import INSUFFICIENT_HISTORY, combine

    qs = list(periods)[:4]
    if len(qs) < 4:
        return Q.null(INSUFFICIENT_HISTORY,
                      missing=(f"{name}: TTM needs 4 quarters, have {len(qs)}",),
                      label=f"{name}_ttm")
    vals = {f"q{i}": p.get(name) for i, p in enumerate(qs)}
    return combine(f"{name}_ttm", "q0 + q1 + q2 + q3",
                   next((v.unit for v in vals.values() if v.ok), ""),
                   lambda **kw: sum(kw.values()), vals)


def _company_segments_default() -> dict[str, Q]:
    return {}
