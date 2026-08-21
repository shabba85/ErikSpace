"""Macro sources: Bank of Canada Valet, Statistics Canada WDS, EIA.

All keyless or near-keyless and all authoritative, which means there is no
excuse for a stale macro figure in this system.  Every one of these returns a
provenance-carrying quantity with the publisher's own as_of date -- never a
remembered level, never a rounded "about 3%".
"""

from __future__ import annotations

from typing import Any

from .. import config
from ..http import get_json
from ..provenance import NOT_FETCHED, Q

# ---------------------------------------------------------------------------
# Bank of Canada Valet -- policy rate, GoC curve, USD/CAD
# ---------------------------------------------------------------------------

VALET = "https://www.bankofcanada.ca/valet/observations"

BOC_SERIES = {
    "policy_rate": ("V39079", "pct", "Target for the overnight rate"),
    "usd_cad": ("FXUSDCAD", "USD/CAD", "Daily USD/CAD exchange rate"),
    "goc_2y": ("BD.CDN.2YR.DQ.YLD", "pct", "GoC benchmark 2-year yield"),
    "goc_5y": ("BD.CDN.5YR.DQ.YLD", "pct", "GoC benchmark 5-year yield"),
    "goc_10y": ("BD.CDN.10YR.DQ.YLD", "pct", "GoC benchmark 10-year yield"),
    "goc_30y": ("BD.CDN.LONG.DQ.YLD", "pct", "GoC long-term benchmark yield"),
}


def boc_series(key: str, *, recent: int = 1) -> Q:
    if key not in BOC_SERIES:
        raise KeyError(f"unknown BoC series {key!r}; known: {sorted(BOC_SERIES)}")
    code, unit, desc = BOC_SERIES[key]
    f = get_json("boc", f"{VALET}/{code}/json", ticker=f"macro_{key}", kind="macro",
                 params={"recent": recent})
    obs = (f.body or {}).get("observations", []) if isinstance(f.body, dict) else []
    if not obs:
        return Q.null(NOT_FETCHED, missing=(f"BoC series {code} returned no "
                                            "observations",), unit=unit, label=key)
    o = obs[-1]
    raw = (o.get(code) or {}).get("v")
    scale = 0.01 if unit == "pct" else 1.0
    return Q.reported(None if raw in (None, "") else float(raw) * scale,
                      unit="ratio" if unit == "pct" else unit,
                      as_of=o.get("d"), source_name="Bank of Canada Valet",
                      source_url=f.url, retrieved_at=f.retrieved_at,
                      payload_path=f.payload_path, label=key, note=desc)


def goc_curve() -> dict[str, Q]:
    """The full GoC benchmark curve.  The discount-rate anchor for a Canadian
    issuer -- never a remembered 'about 3.5%'."""
    return {k: boc_series(k) for k in ("goc_2y", "goc_5y", "goc_10y", "goc_30y")}


# ---------------------------------------------------------------------------
# Statistics Canada WDS
# ---------------------------------------------------------------------------

STATCAN = "https://www150.statcan.gc.ca/t1/wds/rest"

#: vector -> (label, unit, description).  Vectors, not tables: a vector is a
#: stable identity for one series, whereas table coordinates shift.
STATCAN_VECTORS = {
    "cpi_headline_yoy": ("v41690973", "ratio", "CPI all-items, 12-month change"),
    "cpi_trim": ("v108785713", "ratio", "CPI-trim, 12-month change"),
    "cpi_median": ("v108785712", "ratio", "CPI-median, 12-month change"),
    "cpi_ex_gasoline": ("v41690914", "ratio", "CPI excluding gasoline, 12-month change"),
    "unemployment_rate": ("v2062815", "ratio", "Unemployment rate, seasonally adjusted"),
    "gdp_monthly": ("v65201210", "CAD_m", "GDP at basic prices, chained 2017 dollars"),
}


def statcan_vector(key: str, *, periods: int = 1) -> Q:
    if key not in STATCAN_VECTORS:
        raise KeyError(f"unknown StatCan vector {key!r}")
    vec, unit, desc = STATCAN_VECTORS[key]
    url = f"{STATCAN}/getDataFromVectorsAndLatestNPeriods"
    f = get_json("statcan", url, ticker=f"macro_{key}", kind="macro",
                 params={"_body": f'[{{"vectorId":{vec.lstrip("v")},'
                                  f'"latestN":{periods}}}]'})
    return _statcan_parse(f, key, unit, desc, vec)


def _statcan_parse(f, key: str, unit: str, desc: str, vec: str) -> Q:
    body = f.body
    rows = body if isinstance(body, list) else [body]
    try:
        obj = rows[0]["object"]
        point = obj["vectorDataPoint"][-1]
    except (KeyError, IndexError, TypeError):
        return Q.null(NOT_FETCHED,
                      missing=(f"StatCan vector {vec} returned no data points",),
                      unit=unit, label=key)
    val = point.get("value")
    scale = 0.01 if unit == "ratio" else 1.0
    return Q.reported(None if val is None else float(val) * scale, unit=unit,
                      as_of=point.get("refPer"), source_name="Statistics Canada WDS",
                      source_url=f.url, retrieved_at=f.retrieved_at,
                      payload_path=f.payload_path, label=key, note=desc)


def cpi_panel() -> dict[str, Q]:
    """Headline plus the three core measures the Bank actually watches.

    Reporting headline CPI alone is how you misread a Bank of Canada decision:
    the target is headline, but the reaction function is trim and median.
    """
    return {k: statcan_vector(k) for k in
            ("cpi_headline_yoy", "cpi_trim", "cpi_median", "cpi_ex_gasoline")}


# ---------------------------------------------------------------------------
# EIA -- WTI
# ---------------------------------------------------------------------------

EIA = "https://api.eia.gov/v2"


def eia_wti() -> Q:
    """WTI spot, Cushing OK, daily."""
    key = config.api_key("eia")
    if not key:
        return Q.null(NOT_FETCHED, missing=("EIA_API_KEY is not set",),
                      unit="USD/bbl", label="wti_spot")
    f = get_json("eia", f"{EIA}/petroleum/pri/spt/data/", ticker="macro_wti",
                 kind="macro",
                 params={"api_key": key, "frequency": "daily", "data[0]": "value",
                         "facets[series][]": "RWTC", "sort[0][column]": "period",
                         "sort[0][direction]": "desc", "length": 1},
                 redact=("api_key",))
    try:
        row = f.body["response"]["data"][0]
    except (KeyError, IndexError, TypeError):
        return Q.null(NOT_FETCHED, missing=("EIA returned no WTI observations",),
                      unit="USD/bbl", label="wti_spot")
    return Q.reported(row.get("value"), unit="USD/bbl", as_of=row.get("period"),
                      source_name="U.S. Energy Information Administration",
                      source_url=f.url, retrieved_at=f.retrieved_at,
                      payload_path=f.payload_path, label="wti_spot",
                      note="WTI spot FOB, Cushing OK (series RWTC)")


def wcs_differential(quote_url: str | None = None) -> Q:
    """WCS differential to WTI.

    There is no free authoritative API for WCS.  Rather than invent a
    differential -- which for an oil-sands name is one of the two numbers that
    determine the whole valuation -- this returns a null naming the gap until a
    quote source is configured.  Set `macro.wcs_quote_url` in settings.yaml to a
    JSON endpoint you trust.
    """
    url = quote_url or (config.settings().get("macro", {}) or {}).get("wcs_quote_url")
    if not url:
        return Q.null(
            NOT_FETCHED,
            missing=("WCS differential: no quote source configured. This figure "
                     "drives oil-sands valuation and will not be assumed. Set "
                     "macro.wcs_quote_url in config/settings.yaml.",),
            unit="USD/bbl", label="wcs_differential")
    f = get_json("eia", url, ticker="macro_wcs", kind="macro")
    body = f.body if isinstance(f.body, dict) else {}
    return Q.reported(body.get("value"), unit="USD/bbl", as_of=body.get("date"),
                      source_name=body.get("source", "configured WCS quote source"),
                      source_url=f.url, retrieved_at=f.retrieved_at,
                      payload_path=f.payload_path, label="wcs_differential")


def macro_panel() -> dict[str, Q]:
    """Everything the interpretation layer is allowed to cite as 'the macro'."""
    out: dict[str, Q] = {}
    for k in ("policy_rate", "usd_cad"):
        out[k] = boc_series(k)
    out.update(goc_curve())
    out.update(cpi_panel())
    out["unemployment_rate"] = statcan_vector("unemployment_rate")
    out["wti_spot"] = eia_wti()
    out["wcs_differential"] = wcs_differential()
    return out
