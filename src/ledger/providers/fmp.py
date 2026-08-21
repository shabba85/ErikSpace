"""Financial Modeling Prep adapter.

Covers .TO and .V symbols with full statements.  Known weaknesses -- restatement
drift and share-count errors on small caps -- are exactly what the
reconciliation gate is built to catch, which is why FMP is never trusted alone
on the three anchors.
"""

from __future__ import annotations

from typing import Any

from .. import config
from ..classify import classify
from ..http import get_json
from ..model import Period
from ..provenance import NOT_FETCHED, Q
from .base import Provider, ProviderUnavailable, Quote, build_period, default_unit_for, field_q

BASE = "https://financialmodelingprep.com/api/v3"

INCOME = {
    "revenue": "revenue", "cost_of_revenue": "costOfRevenue",
    "gross_profit": "grossProfit",
    "sga_expense": "sellingGeneralAndAdministrativeExpenses",
    "rnd_expense": "researchAndDevelopmentExpenses",
    "operating_income": "operatingIncome",
    "depreciation_amortization": "depreciationAndAmortization",
    "interest_expense": "interestExpense", "pretax_income": "incomeBeforeTax",
    "income_tax_expense": "incomeTaxExpense", "net_income": "netIncome",
    "eps_diluted": "epsdiluted", "diluted_shares": "weightedAverageShsOutDil",
    "basic_shares": "weightedAverageShsOut",
}
BALANCE = {
    "cash_and_equivalents": "cashAndCashEquivalents",
    "short_term_investments": "shortTermInvestments",
    "receivables": "netReceivables", "inventory": "inventory",
    "other_current_assets": "otherCurrentAssets",
    "total_current_assets": "totalCurrentAssets",
    "ppe_net": "propertyPlantEquipmentNet", "goodwill": "goodwill",
    "intangibles": "intangibleAssets",
    "total_non_current_assets": "totalNonCurrentAssets",
    "total_assets": "totalAssets", "payables": "accountPayables",
    "other_current_liabilities": "otherCurrentLiabilities",
    "total_current_liabilities": "totalCurrentLiabilities",
    "short_term_debt": "shortTermDebt", "long_term_debt": "longTermDebt",
    "total_debt": "totalDebt", "total_liabilities": "totalLiabilities",
    "retained_earnings": "retainedEarnings",
    "total_equity": "totalStockholdersEquity",
    "minority_interest": "minorityInterest",
}
CASHFLOW = {
    "cash_from_operations": "netCashProvidedByOperatingActivities",
    "capital_expenditure": "capitalExpenditure",
    "depreciation_amortization_cf": "depreciationAndAmortization",
    "stock_based_compensation": "stockBasedCompensation",
    "deferred_income_tax": "deferredIncomeTax",
    "change_in_working_capital": "changeInWorkingCapital",
    "dividends_paid": "dividendsPaid",
    "share_repurchase": "commonStockRepurchased",
    "share_issuance": "commonStockIssued",
    "cash_from_investing": "netCashUsedForInvestingActivites",
    "cash_from_financing": "netCashUsedProvidedByFinancingActivities",
    "other_non_cash_charges": "otherNonCashItems",
}
FIELD_MAP = ({k: ("income", v) for k, v in INCOME.items()}
             | {k: ("balance", v) for k, v in BALANCE.items()}
             | {k: ("cashflow", v) for k, v in CASHFLOW.items()})


class FMP:
    name = "fmp"
    confidence = "high"

    def available(self) -> bool:
        return bool(config.api_key("fmp"))

    def _key(self) -> str:
        k = config.api_key("fmp")
        if not k:
            raise ProviderUnavailable("FMP_API_KEY is not set")
        return k

    def symbol(self, ticker: str) -> str:
        """FMP uses the Yahoo-style suffixes for Canadian listings."""
        return ticker.upper()

    def _get(self, path: str, ticker: str, kind: str, **params: Any):
        return get_json(self.name, f"{BASE}/{path}", ticker=ticker, kind=kind,
                        params={"apikey": self._key(), **params})

    def profile(self, ticker: str) -> dict[str, Any]:
        f = self._get(f"profile/{self.symbol(ticker)}", ticker, "statements")
        rows = f.body if isinstance(f.body, list) else []
        if not rows:
            return {"_fetched": f, "_empty": True}
        r = rows[0]
        return {
            "_fetched": f,
            "name": r.get("companyName", ""), "sector": r.get("sector", "") or "",
            "industry": r.get("industry", "") or "", "currency": r.get("currency", "") or "",
            "exchange": r.get("exchangeShortName", "") or "",
            "cik": (str(r.get("cik")).zfill(10) if r.get("cik") else None),
            "schema": classify(r.get("sector", "") or "", r.get("industry", "") or "",
                               r.get("companyName", "") or ""),
        }

    def quote(self, ticker: str) -> Quote:
        f = self._get(f"quote/{self.symbol(ticker)}", ticker, "prices")
        rows = f.body if isinstance(f.body, list) else []
        r = rows[0] if rows else {}
        cur = "CAD" if ticker.upper().endswith((".TO", ".V")) else "USD"
        as_of = str(r.get("date", ""))[:10] or None
        return Quote(
            last_close=field_q(f, r, "price", unit=cur, as_of=as_of,
                               label="last_close", confidence=self.confidence),
            market_cap=field_q(f, r, "marketCap", unit=cur, as_of=as_of,
                               label="market_cap", confidence=self.confidence),
            shares_outstanding=field_q(f, r, "sharesOutstanding", unit="shares",
                                       as_of=as_of, label="shares_outstanding",
                                       confidence=self.confidence),
        )

    def _periods(self, ticker: str, period: str, limit: int, schema: str,
                 currency: str) -> list[Period]:
        sym = self.symbol(ticker)
        parts = {}
        for key, path in (("income", "income-statement"),
                          ("balance", "balance-sheet-statement"),
                          ("cashflow", "cash-flow-statement")):
            parts[key] = self._get(f"{path}/{sym}", ticker, "statements",
                                   period=period, limit=limit)

        by_date: dict[str, dict[str, Any]] = {}
        for key, f in parts.items():
            for row in (f.body if isinstance(f.body, list) else []):
                d = str(row.get("date", ""))[:10]
                if d:
                    by_date.setdefault(d, {})[key] = (f, row)

        out: list[Period] = []
        for d in sorted(by_date, reverse=True)[:limit]:
            cur = next((r.get("reportedCurrency") for _, r in by_date[d].values()
                        if r.get("reportedCurrency")), currency)
            filing = next((r.get("finalLink") or r.get("link")
                           for _, r in by_date[d].values()
                           if r.get("finalLink") or r.get("link")), None)
            out.append(build_period(
                by_date[d], FIELD_MAP, ticker=ticker, period_end=d,
                period_type="FY" if period == "annual" else "Q",
                currency=cur or "", schema=schema, confidence=self.confidence,
                source_name=self.name, filing_url=filing,
                unit_for=default_unit_for(cur or "")))
        return out

    def annual_periods(self, ticker: str, limit: int, *, schema: str = "industrial",
                       currency: str = "") -> list[Period]:
        return self._periods(ticker, "annual", limit, schema, currency)

    def quarterly_periods(self, ticker: str, limit: int, *, schema: str = "industrial",
                          currency: str = "") -> list[Period]:
        return self._periods(ticker, "quarter", limit, schema, currency)
