"""Yahoo Finance fallback -- FREE, UNOFFICIAL, AND FLAGGED LOW-CONFIDENCE.

Every quantity this adapter emits carries confidence='low' in its provenance,
and that flag propagates through every derived figure: a ROIC computed from
Yahoo inputs is itself marked low-confidence, all the way to the UI.

Why the flag matters rather than being a formality: Yahoo's Canadian statement
data is scraped, inconsistently restated, and silently drops line items.  It is
here so that a name with no vendor coverage produces *something* auditable
instead of nothing -- never as a peer of the paid sources.

It is deliberately never used as the reconciliation cross-check when a paid
source is available, because agreement with Yahoo is not evidence.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..classify import classify
from ..http import get_json
from ..model import Period
from ..provenance import NOT_FETCHED, Q
from .base import Quote, build_period, default_unit_for, field_q

CHART = "https://query2.finance.yahoo.com/v8/finance/chart"
SUMMARY = "https://query2.finance.yahoo.com/v10/finance/quoteSummary"

FIELD_MAP = {
    "revenue": ("income", "totalRevenue"),
    "cost_of_revenue": ("income", "costOfRevenue"),
    "gross_profit": ("income", "grossProfit"),
    "sga_expense": ("income", "sellingGeneralAdministrative"),
    "rnd_expense": ("income", "researchDevelopment"),
    "operating_income": ("income", "operatingIncome"),
    "interest_expense": ("income", "interestExpense"),
    "pretax_income": ("income", "incomeBeforeTax"),
    "income_tax_expense": ("income", "incomeTaxExpense"),
    "net_income": ("income", "netIncome"),
    "cash_and_equivalents": ("balance", "cash"),
    "short_term_investments": ("balance", "shortTermInvestments"),
    "receivables": ("balance", "netReceivables"),
    "inventory": ("balance", "inventory"),
    "total_current_assets": ("balance", "totalCurrentAssets"),
    "ppe_net": ("balance", "propertyPlantEquipment"),
    "goodwill": ("balance", "goodWill"),
    "intangibles": ("balance", "intangibleAssets"),
    "total_assets": ("balance", "totalAssets"),
    "payables": ("balance", "accountsPayable"),
    "total_current_liabilities": ("balance", "totalCurrentLiabilities"),
    "short_term_debt": ("balance", "shortLongTermDebt"),
    "long_term_debt": ("balance", "longTermDebt"),
    "total_liabilities": ("balance", "totalLiab"),
    "retained_earnings": ("balance", "retainedEarnings"),
    "total_equity": ("balance", "totalStockholderEquity"),
    "cash_from_operations": ("cashflow", "totalCashFromOperatingActivities"),
    "capital_expenditure": ("cashflow", "capitalExpenditures"),
    "depreciation_amortization_cf": ("cashflow", "depreciation"),
    "change_in_working_capital": ("cashflow", "changeToNetincome"),
    "dividends_paid": ("cashflow", "dividendsPaid"),
    "share_repurchase": ("cashflow", "repurchaseOfStock"),
    "share_issuance": ("cashflow", "issuanceOfStock"),
    "cash_from_investing": ("cashflow", "totalCashflowsFromInvestingActivities"),
    "cash_from_financing": ("cashflow", "totalCashFromFinancingActivities"),
}

MODULES = ("incomeStatementHistory", "balanceSheetHistory",
           "cashflowStatementHistory", "assetProfile", "price",
           "defaultKeyStatistics")


class Yahoo:
    name = "yahoo"
    confidence = "low"      # never anything else

    def available(self) -> bool:
        return True         # keyless

    def symbol(self, ticker: str) -> str:
        return ticker.upper()

    # -- Yahoo's cookie/crumb handshake ----------------------------------
    _crumb: str | None = None
    _client: httpx.Client | None = None

    def _session(self) -> tuple[httpx.Client, str]:
        if self._client is not None and self._crumb:
            return self._client, self._crumb
        from .. import config

        cl = httpx.Client(timeout=float(config.settings()["http"]["timeout_seconds"]),
                          follow_redirects=True,
                          headers={"User-Agent": config.user_agent()})
        cl.get("https://fc.yahoo.com")
        r = cl.get("https://query1.finance.yahoo.com/v1/test/getcrumb")
        crumb = r.text.strip()
        if not crumb or len(crumb) > 32:
            raise RuntimeError(f"yahoo crumb handshake failed: {r.status_code}")
        self._client, self._crumb = cl, crumb
        return cl, crumb

    def _summary(self, ticker: str):
        cl, crumb = self._session()
        return get_json(self.name, f"{SUMMARY}/{self.symbol(ticker)}",
                        ticker=ticker, kind="statements", client=cl,
                        params={"modules": ",".join(MODULES), "crumb": crumb},
                        redact=("crumb",))

    def profile(self, ticker: str) -> dict[str, Any]:
        f = self._summary(ticker)
        res = _result(f.body)
        ap = res.get("assetProfile", {}) or {}
        pr = res.get("price", {}) or {}
        name = pr.get("longName") or pr.get("shortName") or ""
        return {
            "_fetched": f, "name": name, "sector": ap.get("sector", "") or "",
            "industry": ap.get("industry", "") or "",
            "currency": pr.get("currency", "") or "",
            "exchange": pr.get("exchangeName", "") or "", "cik": None,
            "schema": classify(ap.get("sector", "") or "", ap.get("industry", "") or "",
                               name),
            "_confidence": "low",
        }

    def quote(self, ticker: str) -> Quote:
        f = get_json(self.name, f"{CHART}/{self.symbol(ticker)}", ticker=ticker,
                     kind="prices", params={"range": "5d", "interval": "1d"})
        meta = {}
        try:
            meta = f.body["chart"]["result"][0]["meta"]
        except (KeyError, IndexError, TypeError):
            pass
        cur = meta.get("currency", "") or ""
        return Quote(
            last_close=field_q(f, meta, "regularMarketPrice", unit=cur, as_of=None,
                               label="last_close", confidence="low",
                               note="Yahoo unofficial endpoint; low confidence"),
            market_cap=Q.null(NOT_FETCHED,
                              missing=("market_cap: not in yahoo chart meta",),
                              unit=cur, label="market_cap"),
            shares_outstanding=Q.null(NOT_FETCHED,
                                      missing=("shares_outstanding: not in yahoo "
                                               "chart meta",), unit="shares",
                                      label="shares_outstanding"),
        )

    def _periods(self, ticker: str, want: str, limit: int, schema: str,
                 currency: str) -> list[Period]:
        if want != "FY":
            return []   # Yahoo's quarterly modules require a separate call shape
        f = self._summary(ticker)
        res = _result(f.body)
        blocks = {
            "income": (res.get("incomeStatementHistory", {}) or {}).get(
                "incomeStatementHistory", []) or [],
            "balance": (res.get("balanceSheetHistory", {}) or {}).get(
                "balanceSheetStatements", []) or [],
            "cashflow": (res.get("cashflowStatementHistory", {}) or {}).get(
                "cashflowStatements", []) or [],
        }
        by_date: dict[str, dict[str, Any]] = {}
        for key, rows in blocks.items():
            for row in rows:
                d = _yahoo_date(row.get("endDate"))
                if d:
                    by_date.setdefault(d, {})[key] = (f, _flatten(row))
        cur = currency or ((res.get("price", {}) or {}).get("currency") or "")
        out = []
        for d in sorted(by_date, reverse=True)[:limit]:
            out.append(build_period(by_date[d], FIELD_MAP, ticker=ticker,
                                    period_end=d, period_type="FY", currency=cur,
                                    schema=schema, confidence="low",
                                    source_name=self.name,
                                    unit_for=default_unit_for(cur)))
        return out

    def annual_periods(self, ticker: str, limit: int, *, schema: str = "industrial",
                       currency: str = "") -> list[Period]:
        return self._periods(ticker, "FY", limit, schema, currency)

    def quarterly_periods(self, ticker: str, limit: int, *, schema: str = "industrial",
                          currency: str = "") -> list[Period]:
        return self._periods(ticker, "Q", limit, schema, currency)


def _result(body: Any) -> dict[str, Any]:
    try:
        return body["quoteSummary"]["result"][0] or {}
    except (KeyError, IndexError, TypeError):
        return {}


def _flatten(row: dict[str, Any]) -> dict[str, Any]:
    """Yahoo wraps every figure as {'raw': x, 'fmt': '...'}.  Take raw only --
    the formatted string is rounded and locale-dependent."""
    out: dict[str, Any] = {}
    for k, v in (row or {}).items():
        if isinstance(v, dict) and "raw" in v:
            out[k] = v["raw"]
        elif not isinstance(v, dict):
            out[k] = v
    return out


def _yahoo_date(v: Any) -> str | None:
    from datetime import datetime, timezone

    raw = v.get("raw") if isinstance(v, dict) else v
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None
