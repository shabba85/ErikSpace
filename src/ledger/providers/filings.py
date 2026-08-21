"""Primary filings: SEDAR+ and SEC EDGAR.

These are the authority.  The vendors are a convenience that must agree with
them.  When a vendor and a filing disagree, the filing wins and the vendor
figure is flagged -- that is the entire point of spot-checking.

SEDAR+ is the right source for Canadian issuers.  EDGAR is NOT: most TSX names
never file there, and reaching for EDGAR first is the single most common way to
end up with no data for a Canadian company and conclude it does not exist.
EDGAR is used here only for cross-listed 40-F / 6-K filers.
"""

from __future__ import annotations

from typing import Any, Iterable

from .. import config
from ..http import FetchError, get_json
from ..provenance import NOT_FETCHED, Q

SEDAR_BASE = "https://www.sedarplus.ca"
EDGAR_CONCEPT = "https://data.sec.gov/api/xbrl/companyconcept"
EDGAR_FACTS = "https://data.sec.gov/api/xbrl/companyfacts"
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions"


# ---------------------------------------------------------------------------
# SEDAR+
# ---------------------------------------------------------------------------


def sedar_filing_search_url(issuer: str) -> str:
    """A clickable SEDAR+ search URL for an issuer.

    SEDAR+ has no documented public JSON API and its search is session-based.
    Rather than pretend to scrape it reliably, this returns the URL a human can
    click to reach the issuer's filings, and it is recorded in provenance as the
    authoritative document location for every figure spot-checked by hand.
    """
    from urllib.parse import quote_plus

    return (f"{SEDAR_BASE}/csa-party/records/company.html?"
            f"id={quote_plus(issuer)}")


def sedar_spot_check_stub(ticker: str, issuer_name: str, field: str) -> Q:
    """A placeholder that is explicitly NOT a number.

    Returns a null pointing at the SEDAR+ record so the audit trail leads a
    human to the MD&A page where the figure can be confirmed.  This is
    deliberately not automated scraping: a silently broken scraper that returns
    a wrong figure from a filing is worse than no automation at all.
    """
    return Q.null(
        NOT_FETCHED,
        missing=(f"{field}: confirm against SEDAR+ filing for {issuer_name} "
                 f"({sedar_filing_search_url(issuer_name)})",),
        label=field)


# ---------------------------------------------------------------------------
# SEC EDGAR -- cross-listed filers only
# ---------------------------------------------------------------------------

#: canonical field -> XBRL concepts, in preference order.  Several are tried
#: because IFRS filers (40-F) and US-GAAP filers tag differently.
EDGAR_CONCEPTS: dict[str, tuple[tuple[str, str], ...]] = {
    "revenue": (("us-gaap", "Revenues"),
                ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
                ("ifrs-full", "Revenue")),
    "net_income": (("us-gaap", "NetIncomeLoss"),
                   ("ifrs-full", "ProfitLoss")),
    "operating_income": (("us-gaap", "OperatingIncomeLoss"),
                         ("ifrs-full", "ProfitLossFromOperatingActivities")),
    "total_assets": (("us-gaap", "Assets"), ("ifrs-full", "Assets")),
    "total_equity": (("us-gaap", "StockholdersEquity"), ("ifrs-full", "Equity")),
    "cash_and_equivalents": (("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
                             ("ifrs-full", "CashAndCashEquivalents")),
    "diluted_shares": (("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
                       ("ifrs-full",
                        "WeightedAverageNumberOfDilutedOrdinarySharesOutstanding")),
    "cash_from_operations": (
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("ifrs-full", "CashFlowsFromUsedInOperatingActivities")),
}


def edgar_headers() -> dict[str, str]:
    """SEC fair-access policy requires a real contact in the User-Agent."""
    c = config.contact()
    if not c:
        raise RuntimeError(
            "SEC EDGAR requires a contact email. Set LEDGER_CONTACT in .env -- "
            "requests without one are throttled or blocked, and spoofing it "
            "violates the SEC's fair-access policy.")
    return {"User-Agent": f"ledger/0.1 ({c})"}


def edgar_concept(cik: str, field: str, *, unit_hint: str = "CAD",
                  period_type: str = "FY") -> Q:
    """Fetch one XBRL concept from EDGAR, trying each taxonomy in turn.

    Used to spot-check a vendor figure for a cross-listed name against what the
    issuer actually filed.
    """
    if field not in EDGAR_CONCEPTS:
        return Q.null(NOT_FETCHED, missing=(f"{field}: no EDGAR concept mapping",),
                      label=field)
    cik10 = str(cik).zfill(10)
    tried: list[str] = []
    for taxonomy, concept in EDGAR_CONCEPTS[field]:
        url = f"{EDGAR_CONCEPT}/CIK{cik10}/{taxonomy}/{concept}.json"
        try:
            f = get_json("sec", url, ticker=cik10, kind="filings",
                         headers=edgar_headers())
        except FetchError as exc:
            tried.append(f"{taxonomy}:{concept} -> {exc.status}")
            continue
        units = (f.body or {}).get("units", {}) if isinstance(f.body, dict) else {}
        for unit, rows in units.items():
            annual = [r for r in rows if r.get("form") in ("40-F", "10-K", "20-F")
                      and r.get("fp") == "FY"] or rows
            if not annual:
                continue
            r = max(annual, key=lambda x: str(x.get("end", "")))
            return Q.reported(
                r.get("val"), unit=unit, as_of=r.get("end"),
                source_name=f"SEC EDGAR XBRL ({taxonomy}:{concept})",
                source_url=url, retrieved_at=f.retrieved_at,
                payload_path=f.payload_path, label=field,
                note=(f"form {r.get('form')} accn {r.get('accn')} "
                      f"filed {r.get('filed')}"))
        tried.append(f"{taxonomy}:{concept} -> no usable units")
    return Q.null(NOT_FETCHED,
                  missing=(f"{field}: EDGAR had no usable concept ({'; '.join(tried)})",),
                  label=field)


def edgar_filing_index(cik: str) -> dict[str, Any]:
    """Recent filings, so an audit row can link to the actual document."""
    cik10 = str(cik).zfill(10)
    f = get_json("sec", f"{EDGAR_SUBMISSIONS}/CIK{cik10}.json", ticker=cik10,
                 kind="filings", headers=edgar_headers())
    body = f.body if isinstance(f.body, dict) else {}
    recent = (body.get("filings", {}) or {}).get("recent", {}) or {}
    out = []
    forms = recent.get("form", [])
    for i, form in enumerate(forms[:50]):
        if form in ("40-F", "6-K", "20-F", "10-K", "10-Q"):
            accn = recent["accessionNumber"][i].replace("-", "")
            out.append({
                "form": form, "filed": recent["filingDate"][i],
                "accession": recent["accessionNumber"][i],
                "url": (f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/"
                        f"{accn}/{recent['primaryDocument'][i]}"),
            })
    return {"cik": cik10, "name": body.get("name", ""), "filings": out,
            "source_url": f.url, "retrieved_at": f.retrieved_at}


def is_cross_listed(cik: str | None) -> bool:
    """Only cross-listed issuers have a CIK worth querying.

    A .TO name with no CIK is not a data gap to work around -- it simply does
    not file with the SEC, and SEDAR+ is where its filings are.
    """
    return bool(cik)
