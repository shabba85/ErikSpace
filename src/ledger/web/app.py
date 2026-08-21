"""Read-only dashboard.

This process NEVER fetches and NEVER computes.  It opens the database, renders
what `ledger refresh` put there, and shows the as_of date and coverage counts on
every screen so a stale or partial pull is visible rather than disguised.

If you find yourself wanting to add a fetch here, add it to the CLI instead.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .. import config, db
from ..audit import audit_rows, audit_summary, trace

app = FastAPI(title="ledger", docs_url=None, redoc_url=None)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _con() -> sqlite3.Connection:
    p = config.path("db")
    if not p.exists():
        raise HTTPException(503, "database not initialized -- run `ledger init` "
                                 "and `ledger refresh TICKER`")
    con = db.connect(p)
    db.init(con)
    return con


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    con = _con()
    try:
        companies = [dict(r) for r in db.list_companies(con)]
        for c in companies:
            s = audit_summary(con, c["ticker"])
            c.update(populated=s["metrics_populated"], total=s["metrics_total"],
                     as_of=s["as_of"], low_conf=s["low_confidence_values"],
                     blocked=bool(s["reconciliation"] and s["reconciliation"]["blocked"]),
                     block_reason=(s["reconciliation"] or {}).get("reason"))
        deck = config.load_deck()
        return TEMPLATES.TemplateResponse(
            request, "index.html",
            {"companies": companies, "deck": deck.header(),
             "deck_warnings": deck.warnings()})
    finally:
        con.close()


@app.get("/company/{ticker}", response_class=HTMLResponse)
def company(request: Request, ticker: str):
    con = _con()
    try:
        s = audit_summary(con, ticker)
        rows = audit_rows(con, ticker, include_statements=False)
        if not rows:
            raise HTTPException(404, f"no data for {ticker}; run "
                                     f"`ledger refresh {ticker}`")
        co = con.execute("SELECT * FROM companies WHERE ticker=?",
                         (ticker,)).fetchone()
        schema = co["schema_kind"] if co else "industrial"
        from ..model import FORBIDDEN_FOR_FINANCIALS
        na_fields = list(FORBIDDEN_FOR_FINANCIALS) if schema in ("bank", "insurer") else []
        deck = config.load_deck()
        return TEMPLATES.TemplateResponse(
            request, "company.html",
            {"ticker": ticker, "summary": s, "rows": rows,
             "company": dict(co) if co else {}, "schema": schema,
             "na_fields": na_fields, "deck_warnings": deck.warnings()})
    finally:
        con.close()


@app.get("/audit/{ticker}", response_class=HTMLResponse)
def audit_view(request: Request, ticker: str, metric: str | None = None):
    con = _con()
    try:
        rows = audit_rows(con, ticker, include_statements=True, metric_filter=metric)
        if not rows:
            raise HTTPException(404, f"no data for {ticker}")
        return TEMPLATES.TemplateResponse(
            request, "audit.html",
            {"ticker": ticker, "rows": rows,
             "summary": audit_summary(con, ticker), "filter": metric})
    finally:
        con.close()


@app.get("/api/trace/{ticker}/{metric}")
def api_trace(ticker: str, metric: str) -> JSONResponse:
    con = _con()
    try:
        return JSONResponse(json.loads(json.dumps(trace(con, ticker, metric),
                                                  default=str)))
    finally:
        con.close()


@app.get("/api/company/{ticker}")
def api_company(ticker: str) -> JSONResponse:
    con = _con()
    try:
        return JSONResponse({"summary": json.loads(json.dumps(
            audit_summary(con, ticker), default=str)),
            "rows": json.loads(json.dumps(
                audit_rows(con, ticker, include_statements=False), default=str))})
    finally:
        con.close()
