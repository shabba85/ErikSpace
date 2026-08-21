"""Test fixtures.

Every value here is SYNTHETIC and says so in its provenance (source_name
"TEST FIXTURE").  These are inputs to unit tests, not data in the system: no
fixture value can reach the database or a screen, because refresh only ever
writes what a provider adapter returned.

The industrial fixture is built to be internally consistent and to produce
hand-checkable results: revenue falls by a constant 800/year and net PP&E is
exactly 60% of revenue in every year, so the PP&E-to-sales maintenance capex
estimate has a known answer.
"""

from __future__ import annotations

import pytest

from ledger.model import Company, Period
from ledger.provenance import Provenance, Q

RETRIEVED = "2026-01-15T12:00:00+00:00"


def fq(value, unit="CAD", as_of="2025-12-31", label="x", confidence="high") -> Q:
    return Q.reported(value, unit=unit, as_of=as_of, source_name="TEST FIXTURE",
                      source_url="test://fixture", retrieved_at=RETRIEVED,
                      confidence=confidence, label=label)


def make_period(ticker, end, fields, schema="industrial", currency="CAD") -> Period:
    p = Period(ticker=ticker, period_end=end, period_type="FY", currency=currency,
               schema=schema, source_name="TEST FIXTURE")
    for k, v in fields.items():
        unit = ("shares" if k in ("diluted_shares", "basic_shares")
                else "ratio" if k in ("cet1_ratio", "licat_ratio")
                else currency)
        p.set(k, fq(v, unit=unit, as_of=end, label=k))
    return p


def _industrial_year(i: int) -> dict:
    """Year i back from the present.  Revenue falls 800/yr; PP&E is 60% of it."""
    rev = 10000 - 800 * i
    return {
        "revenue": rev,
        "cost_of_revenue": rev * 0.60,
        "gross_profit": rev * 0.40,
        "sga_expense": rev * 0.15,
        "operating_income": rev * 0.20,
        "depreciation_amortization": 800 - 40 * i,
        "interest_expense": 200 - 10 * i,
        "pretax_income": rev * 0.20 - (200 - 10 * i),
        "income_tax_expense": (rev * 0.20 - (200 - 10 * i)) * 0.25,
        "net_income": (rev * 0.20 - (200 - 10 * i)) * 0.75,
        "diluted_shares": 1000 + 20 * i,
        "basic_shares": 1000 + 20 * i,
        "cash_and_equivalents": 500 - 40 * i,
        "receivables": 1200 - 60 * i,
        "inventory": 900 - 40 * i,
        "other_current_assets": 100 - 4 * i,
        "total_current_assets": 2700 - 144 * i,
        "ppe_gross": 9000 - 600 * i,
        "ppe_net": rev * 0.60,
        "goodwill": 800,
        "intangibles": 200,
        "total_assets": 9700 - 700 * i,
        "payables": 800 - 40 * i,
        "total_current_liabilities": 1500 - 80 * i,
        "short_term_debt": 300 - 20 * i,
        "long_term_debt": 3000 - 240 * i,
        "total_debt": 3300 - 260 * i,
        "total_liabilities": 5000 - 400 * i,
        "retained_earnings": 2500 - 200 * i,
        "total_equity": 4700 - 340 * i,
        "cash_from_operations": 2200 - 160 * i,
        "capital_expenditure": -(1000 - 60 * i),
        "depreciation_amortization_cf": 800 - 40 * i,
        "stock_based_compensation": 100,
        "change_in_working_capital": -150,
        "cash_taxes_paid": 400 - 32 * i,
        "dividends_paid": -(400 - 20 * i),
        "share_repurchase": -(200 - 10 * i),
        "share_issuance": 50,
        "cash_from_investing": -(1100 - 60 * i),
        "cash_from_financing": -(600 - 30 * i),
        "other_non_cash_charges": 50,
    }


@pytest.fixture
def industrial() -> Company:
    """A large TSX-style non-financial with 7 years of history."""
    years = ["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31",
             "2021-12-31", "2020-12-31", "2019-12-31"]
    co = Company(ticker="TESTCO.TO", name="Test Industrial Corp", exchange="TSX",
                 sector="Industrials", industry="Industrial Machinery",
                 currency="CAD", schema="industrial")
    co.annual = [make_period("TESTCO.TO", d, _industrial_year(i))
                 for i, d in enumerate(years)]
    co.market = {
        "last_close": fq(24.00, unit="CAD", label="last_close"),
        "market_cap": fq(24000.0, unit="CAD", label="market_cap"),
        "shares_outstanding": fq(1000.0, unit="shares", label="shares_outstanding"),
    }
    return co


@pytest.fixture
def bank() -> Company:
    """A Big-Six-shaped Canadian bank."""
    co = Company(ticker="TESTBANK.TO", name="Test Bank of Canada", exchange="TSX",
                 sector="Financials", industry="Diversified Banks",
                 currency="CAD", schema="bank")
    for i, d in enumerate(["2025-10-31", "2024-10-31", "2023-10-31"]):
        co.annual.append(make_period("TESTBANK.TO", d, {
            "net_interest_income": 16000 - 800 * i,
            "non_interest_income": 12000 - 500 * i,
            "total_revenue": 28000 - 1300 * i,
            "non_interest_expense": 15400 - 700 * i,
            "provision_for_credit_losses": 2000 - 300 * i,
            "pcl_on_performing_loans": 600 - 150 * i,
            "pcl_on_impaired_loans": 1400 - 150 * i,
            "average_earning_assets": 1_500_000 - 60_000 * i,
            "average_gross_loans": 800_000 - 30_000 * i,
            "gross_loans": 810_000 - 30_000 * i,
            "total_deposits": 950_000 - 40_000 * i,
            "demand_deposits": 400_000 - 20_000 * i,
            "notice_deposits": 250_000 - 10_000 * i,
            "term_deposits": 300_000 - 10_000 * i,
            "net_income": 16000 - 900 * i,
            "preferred_dividends": 300,
            "common_equity": 95_000 - 5_000 * i,
            "goodwill": 11_000,
            "intangibles": 3_000,
            "total_assets": 2_000_000 - 80_000 * i,
            "cet1_ratio": 0.132 - 0.002 * i,
            "diluted_shares": 1400 - 10 * i,
            "pretax_income": 20000 - 1100 * i,
            "income_tax_expense": 4000 - 200 * i,
            "dividends_paid": -(7000 - 300 * i),
            "share_repurchase": -(1000 - 100 * i),
        }, schema="bank"))
    co.market = {"last_close": fq(140.0, unit="CAD", as_of="2025-10-31",
                                  label="last_close")}
    return co


def make_bank(ticker: str, rotce_target: float, ptbv_target: float) -> Company:
    """A bank constructed to hit a chosen ROTCE and P/TBV, for the regression."""
    tce = 80_000.0
    ni_common = rotce_target * tce          # average TCE == TCE (flat equity)
    shares = 1000.0
    tbvps = tce / shares
    co = Company(ticker=ticker, name=ticker, sector="Financials",
                 industry="Diversified Banks", currency="CAD", schema="bank")
    for d in ("2025-10-31", "2024-10-31"):
        co.annual.append(make_period(ticker, d, {
            "net_income": ni_common + 300, "preferred_dividends": 300,
            "common_equity": tce + 14_000, "goodwill": 11_000,
            "intangibles": 3_000, "diluted_shares": shares,
        }, schema="bank"))
    co.market = {"last_close": fq(ptbv_target * tbvps, unit="CAD",
                                  as_of="2025-10-31", label="last_close")}
    return co


@pytest.fixture
def oil_producer() -> Company:
    """An oil producer that discloses a WTI cash-flow sensitivity in its MD&A."""
    co = Company(ticker="TESTOIL.TO", name="Test Energy Ltd", exchange="TSX",
                 sector="Energy", industry="Oil & Gas Exploration & Production",
                 currency="CAD", schema="industrial")
    years = ["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31",
             "2021-12-31", "2020-12-31", "2019-12-31"]
    for i, d in enumerate(years):
        f = _industrial_year(i)
        f["operating_income"] = 3000 - 200 * i      # peak-cycle earnings
        co.annual.append(make_period("TESTOIL.TO", d, f))
    co.market = {"last_close": fq(30.0, unit="CAD", label="last_close"),
                 "market_cap": fq(30000.0, unit="CAD", label="market_cap")}
    # Disclosed sensitivity: cash flow moves 120 CAD per US$1/bbl move in WTI.
    co.sensitivities = {
        "cash_flow_per_usd_wti": fq(120.0, unit="CAD per USD/bbl",
                                    label="cash_flow_per_usd_wti"),
    }
    co.reserves = {
        "proved_reserves": fq(1_800.0, unit="mmboe", label="proved_reserves"),
        "annual_production": fq(150.0, unit="mmboe", label="annual_production"),
    }
    return co


@pytest.fixture
def wacc() -> Q:
    return Q(value=0.09, unit="ratio", as_of="2026-01-15",
             prov=Provenance(source_name="TEST FIXTURE", source_url="test://wacc",
                             retrieved_at=RETRIEVED, method="normalized",
                             confidence="low"), label="wacc")


@pytest.fixture
def realized_wti() -> Q:
    return fq(78.0, unit="USD/bbl", as_of="2025-12-31", label="realized_wti")
