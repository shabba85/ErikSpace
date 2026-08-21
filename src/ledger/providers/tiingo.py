"""Tiingo adapter.

Cleanest price data of the three and the best-documented survivorship handling,
but the thinnest Canadian fundamentals coverage: many .V names return no
statements at all.  When that happens the adapter returns an empty period list
and the caller records "tiingo: no statements" -- it does not fall through to a
different vendor's numbers under Tiingo's name.

Tiingo returns statements as (dataCode, value) pairs rather than a flat object,
so the shape is pivoted before the standard field map applies.
"""

from __future__ import annotations

from typing import Any

from .. import config
from ..classify import classify
from ..http import get_json
from ..model import Period
from .base import ProviderUnavailable, Quote, build_period, default_unit_for, field_q

BASE = "https://api.tiingo.com/tiingo"

FIELD_MAP = {
    "revenue": ("stmt", "revenue"), "cost_of_revenue": ("stmt", "costRev"),
    "gross_profit": ("stmt", "grossProfit"), "sga_expense": ("stmt", "sga"),
    "rnd_expense": ("stmt", "rnd"), "operating_income": ("stmt", "opinc"),
    "depreciation_amortization": ("stmt", "depamor"),
    "interest_expense": ("stmt", "intexp"), "pretax_income": ("stmt", "ebt"),
    "income_tax_expense": ("stmt", "taxExp"), "net_income": ("stmt", "netinc"),
    "eps_diluted": ("stmt", "epsDil"), "diluted_shares": ("stmt", "shareswaDil"),
    "basic_shares": ("stmt", "shareswa"),
    "cash_and_equivalents": ("stmt", "cashAndEq"),
    "short_term_investments": ("stmt", "investmentsCurrent"),
    "receivables": ("stmt", "acctRec"), "inventory": ("stmt", "inventory"),
    "total_current_assets": ("stmt", "assetsCurrent"),
    "ppe_net": ("stmt", "ppeq"), "goodwill": ("stmt", "goodwill"),
    "intangibles": ("stmt", "intangibles"), "total_assets": ("stmt", "totalAssets"),
    "payables": ("stmt", "acctPay"),
    "total_current_liabilities": ("stmt", "liabilitiesCurrent"),
    "short_term_debt": ("stmt", "debtCurrent"),
    "long_term_debt": ("stmt", "debtNonCurrent"), "total_debt": ("stmt", "debt"),
    "total_liabilities": ("stmt", "totalLiabilities"),
    "retained_earnings": ("stmt", "retainedEarnings"),
    "total_equity": ("stmt", "equity"),
    "cash_from_operations": ("stmt", "ncfo"),
    "capital_expenditure": ("stmt", "capex"),
    "depreciation_amortization_cf": ("stmt", "depamor"),
    "stock_based_compensation": ("stmt", "sbcomp"),
    "change_in_working_capital": ("stmt", "ncfworkingcap"),
    "dividends_paid": ("stmt", "payDiv"),
    "share_repurchase": ("stmt", "ncfcommon"),
    "cash_from_investing": ("stmt", "ncfi"),
    "cash_from_financing": ("stmt", "ncff"),
}


class Tiingo:
    name = "tiingo"
    confidence = "high"

    def available(self) -> bool:
        return bool(config.api_key("tiingo"))

    def _headers(self) -> dict[str, str]:
        k = config.api_key("tiingo")
        if not k:
            raise ProviderUnavailable("TIINGO_API_KEY is not set")
        return {"Authorization": f"Token {k}"}

    def symbol(self, ticker: str) -> str:
        """Tiingo drops the Canadian suffix for cross-listed names and uses
        `TICKER-CN` style for TSX-only names on some plans.  We pass the base
        symbol and record what we asked for, rather than guessing a mapping we
        cannot verify."""
        return ticker.upper().replace(".TO", "").replace(".V", "")

    def profile(self, ticker: str) -> dict[str, Any]:
        f = get_json(self.name, f"{BASE}/daily/{self.symbol(ticker)}",
                     ticker=ticker, kind="statements", headers=self._headers())
        r = f.body if isinstance(f.body, dict) else {}
        return {
            "_fetched": f, "name": r.get("name", ""), "sector": "", "industry": "",
            "currency": "", "exchange": r.get("exchangeCode", "") or "", "cik": None,
            "schema": classify("", "", r.get("name", "") or ""),
        }

    def quote(self, ticker: str) -> Quote:
        f = get_json(self.name, f"{BASE}/daily/{self.symbol(ticker)}/prices",
                     ticker=ticker, kind="prices", headers=self._headers())
        rows = f.body if isinstance(f.body, list) else []
        r = rows[-1] if rows else {}
        cur = "CAD" if ticker.upper().endswith((".TO", ".V")) else "USD"
        as_of = str(r.get("date", ""))[:10] or None
        from ..provenance import NOT_FETCHED, Q
        return Quote(
            last_close=field_q(f, r, "close", unit=cur, as_of=as_of,
                               label="last_close", confidence=self.confidence),
            market_cap=Q.null(NOT_FETCHED,
                              missing=("market_cap: tiingo daily prices do not "
                                       "carry market cap",), unit=cur,
                              label="market_cap"),
            shares_outstanding=Q.null(NOT_FETCHED,
                                      missing=("shares_outstanding: not in tiingo "
                                               "daily prices",), unit="shares",
                                      label="shares_outstanding"),
        )

    def _periods(self, ticker: str, want: str, limit: int, schema: str,
                 currency: str) -> list[Period]:
        f = get_json(self.name, f"{BASE}/fundamentals/{self.symbol(ticker)}/statements",
                     ticker=ticker, kind="statements", headers=self._headers())
        rows = f.body if isinstance(f.body, list) else []
        out: list[Period] = []
        cur = currency or ("CAD" if ticker.upper().endswith((".TO", ".V")) else "USD")
        for row in rows:
            if str(row.get("quarter", "")) != ("0" if want == "FY" else row.get("quarter")):
                pass
            is_annual = int(row.get("quarter", 0) or 0) == 0
            if (want == "FY") != is_annual:
                continue
            flat: dict[str, Any] = {}
            for _block, items in (row.get("statementData") or {}).items():
                for item in items or []:
                    if isinstance(item, dict) and "dataCode" in item:
                        flat[item["dataCode"]] = item.get("value")
            d = str(row.get("date", ""))[:10]
            if not d:
                continue
            out.append(build_period({"stmt": (f, flat)}, FIELD_MAP, ticker=ticker,
                                    period_end=d, period_type=want,
                                    currency=cur, schema=schema,
                                    confidence=self.confidence, source_name=self.name,
                                    unit_for=default_unit_for(cur)))
        out.sort(key=lambda p: p.period_end, reverse=True)
        return out[:limit]

    def annual_periods(self, ticker: str, limit: int, *, schema: str = "industrial",
                       currency: str = "") -> list[Period]:
        return self._periods(ticker, "FY", limit, schema, currency)

    def quarterly_periods(self, ticker: str, limit: int, *, schema: str = "industrial",
                          currency: str = "") -> list[Period]:
        return self._periods(ticker, "Q", limit, schema, currency)
