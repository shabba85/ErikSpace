"""EODHD adapter.

Better TSXV / small-cap coverage and cleaner corporate-actions handling than the
alternatives, which matters on the Venture where share consolidations are
routine and a mishandled one silently corrupts every per-share figure.

EODHD returns one large nested fundamentals document rather than per-statement
endpoints, so the whole document is archived once and every period is read from
that single immutable payload.
"""

from __future__ import annotations

from typing import Any

from .. import config
from ..classify import classify
from ..http import get_json
from ..model import Period
from ..provenance import NOT_FETCHED, Q
from .base import ProviderUnavailable, Quote, build_period, default_unit_for, field_q

BASE = "https://eodhd.com/api"

INCOME = {
    "revenue": "totalRevenue", "cost_of_revenue": "costOfRevenue",
    "gross_profit": "grossProfit",
    "sga_expense": "sellingGeneralAdministrative",
    "rnd_expense": "researchDevelopment",
    "operating_income": "operatingIncome",
    "depreciation_amortization": "depreciationAndAmortization",
    "interest_expense": "interestExpense",
    "pretax_income": "incomeBeforeTax", "income_tax_expense": "incomeTaxExpense",
    "net_income": "netIncome",
}
BALANCE = {
    "cash_and_equivalents": "cash", "short_term_investments": "shortTermInvestments",
    "receivables": "netReceivables", "inventory": "inventory",
    "other_current_assets": "otherCurrentAssets",
    "total_current_assets": "totalCurrentAssets",
    "ppe_net": "propertyPlantEquipment", "ppe_gross": "propertyPlantAndEquipmentGross",
    "goodwill": "goodWill", "intangibles": "intangibleAssets",
    "total_assets": "totalAssets", "payables": "accountsPayable",
    "total_current_liabilities": "totalCurrentLiabilities",
    "short_term_debt": "shortTermDebt", "long_term_debt": "longTermDebt",
    "total_liabilities": "totalLiab", "retained_earnings": "retainedEarnings",
    "total_equity": "totalStockholderEquity",
    "minority_interest": "noncontrollingInterestInConsolidatedEntity",
    "diluted_shares": "commonStockSharesOutstanding",
}
CASHFLOW = {
    "cash_from_operations": "totalCashFromOperatingActivities",
    "capital_expenditure": "capitalExpenditures",
    "depreciation_amortization_cf": "depreciation",
    "stock_based_compensation": "stockBasedCompensation",
    "change_in_working_capital": "changeInWorkingCapital",
    "dividends_paid": "dividendsPaid", "share_repurchase": "salePurchaseOfStock",
    "cash_from_investing": "totalCashflowsFromInvestingActivities",
    "cash_from_financing": "totalCashFromFinancingActivities",
}
FIELD_MAP = ({k: ("income", v) for k, v in INCOME.items()}
             | {k: ("balance", v) for k, v in BALANCE.items()}
             | {k: ("cashflow", v) for k, v in CASHFLOW.items()})


class EODHD:
    name = "eodhd"
    confidence = "high"

    def available(self) -> bool:
        return bool(config.api_key("eodhd"))

    def _key(self) -> str:
        k = config.api_key("eodhd")
        if not k:
            raise ProviderUnavailable("EODHD_API_KEY is not set")
        return k

    def symbol(self, ticker: str) -> str:
        """EODHD uses EXCHANGE codes: RY.TO -> RY.TO, ABC.V -> ABC.V."""
        return ticker.upper()

    def _fundamentals(self, ticker: str):
        return get_json(self.name, f"{BASE}/fundamentals/{self.symbol(ticker)}",
                        ticker=ticker, kind="statements",
                        params={"api_token": self._key(), "fmt": "json"})

    def profile(self, ticker: str) -> dict[str, Any]:
        f = self._fundamentals(ticker)
        g = (f.body or {}).get("General", {}) if isinstance(f.body, dict) else {}
        return {
            "_fetched": f, "name": g.get("Name", ""),
            "sector": g.get("Sector", "") or "", "industry": g.get("Industry", "") or "",
            "currency": g.get("CurrencyCode", "") or "",
            "exchange": g.get("Exchange", "") or "", "cik": g.get("CIK"),
            "schema": classify(g.get("Sector", "") or "", g.get("Industry", "") or "",
                               g.get("Name", "") or ""),
        }

    def quote(self, ticker: str) -> Quote:
        f = get_json(self.name, f"{BASE}/real-time/{self.symbol(ticker)}",
                     ticker=ticker, kind="prices",
                     params={"api_token": self._key(), "fmt": "json"})
        r = f.body if isinstance(f.body, dict) else {}
        cur = "CAD" if ticker.upper().endswith((".TO", ".V")) else "USD"
        fu = self._fundamentals(ticker)
        hl = (fu.body or {}).get("SharesStats", {}) if isinstance(fu.body, dict) else {}
        return Quote(
            last_close=field_q(f, r, "close", unit=cur, as_of=None,
                               label="last_close", confidence=self.confidence),
            market_cap=field_q(
                fu, ((fu.body or {}).get("Highlights", {})
                     if isinstance(fu.body, dict) else {}),
                "MarketCapitalization", unit=cur, as_of=None, label="market_cap",
                confidence=self.confidence),
            shares_outstanding=field_q(fu, hl, "SharesOutstanding", unit="shares",
                                       as_of=None, label="shares_outstanding",
                                       confidence=self.confidence),
        )

    def _periods(self, ticker: str, freq: str, limit: int, schema: str,
                 currency: str) -> list[Period]:
        f = self._fundamentals(ticker)
        fin = (f.body or {}).get("Financials", {}) if isinstance(f.body, dict) else {}
        blocks = {
            "income": (fin.get("Income_Statement", {}) or {}).get(freq, {}) or {},
            "balance": (fin.get("Balance_Sheet", {}) or {}).get(freq, {}) or {},
            "cashflow": (fin.get("Cash_Flow", {}) or {}).get(freq, {}) or {},
        }
        dates = sorted(set().union(*(set(b) for b in blocks.values())), reverse=True)
        out: list[Period] = []
        for d in dates[:limit]:
            parts = {k: (f, b[d]) for k, b in blocks.items() if d in b}
            if not parts:
                continue
            cur = next((r.get("currency_symbol") for _, r in parts.values()
                        if r.get("currency_symbol")), currency)
            out.append(build_period(
                parts, FIELD_MAP, ticker=ticker, period_end=str(d)[:10],
                period_type="FY" if freq == "yearly" else "Q",
                currency=cur or "", schema=schema, confidence=self.confidence,
                source_name=self.name, unit_for=default_unit_for(cur or "")))
        return out

    def annual_periods(self, ticker: str, limit: int, *, schema: str = "industrial",
                       currency: str = "") -> list[Period]:
        return self._periods(ticker, "yearly", limit, schema, currency)

    def quarterly_periods(self, ticker: str, limit: int, *, schema: str = "industrial",
                          currency: str = "") -> list[Period]:
        return self._periods(ticker, "quarterly", limit, schema, currency)
