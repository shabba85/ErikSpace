"""`ledger audit TICKER` -- every field with its source URL and timestamp.

The promise this makes: for any number on any screen, you can reach the filing
or the API response it came from.  A derived figure prints its formula and the
provenance ids of its inputs, each of which resolves to another row here.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from . import db
from .provenance import Q


def audit_rows(con, ticker: str, *, include_statements: bool = True,
               metric_filter: str | None = None) -> list[dict[str, Any]]:
    rows = db.latest_metrics(con, ticker)
    out = []
    for r in rows:
        name = r["metric"]
        if not include_statements and name.startswith("stmt."):
            continue
        if metric_filter and metric_filter.lower() not in name.lower():
            continue
        out.append({
            "metric": name,
            "value": r["value"],
            "unit": r["unit"],
            "as_of": r["as_of"],
            "status": "populated" if r["value"] is not None else (r["reason"] or "null"),
            "missing": json.loads(r["missing"] or "[]"),
            "method": r["method"],
            "source_name": r["source_name"],
            "source_url": r["source_url"],
            "retrieved_at": r["retrieved_at"],
            "formula": r["formula"],
            "input_provenance_ids": json.loads(r["inputs"] or "[]"),
            "confidence": r["confidence"],
            "raw_payload": r["payload_path"],
            "note": r["note"],
        })
    return out


def audit_summary(con, ticker: str) -> dict[str, Any]:
    rows = audit_rows(con, ticker)
    computed = [r for r in rows if not r["metric"].startswith("stmt.")]
    pop = [r for r in computed if r["value"] is not None]
    low = [r for r in rows if r["confidence"] == "low" and r["value"] is not None]
    rec = db.get_reconciliation(con, ticker)
    company = con.execute("SELECT * FROM companies WHERE ticker=?",
                          (ticker,)).fetchone()
    return {
        "ticker": ticker,
        "company": dict(company) if company else None,
        "as_of": max((r["as_of"] for r in rows if r["as_of"]), default=None),
        "metrics_total": len(computed),
        "metrics_populated": len(pop),
        "metrics_null": len(computed) - len(pop),
        "statement_fields": len(rows) - len(computed),
        "low_confidence_values": len(low),
        "reconciliation": {
            "blocked": bool(rec["blocked"]), "flags": json.loads(rec["flags"] or "[]"),
            "reason": rec["reason"],
        } if rec else None,
        "distinct_sources": sorted({r["source_name"] for r in rows
                                    if r["source_name"]}),
    }


def trace(con, ticker: str, metric: str) -> dict[str, Any]:
    """Full provenance trace of one metric, including how it moved over time."""
    hist = db.history(con, ticker, metric)
    if not hist:
        return {"ticker": ticker, "metric": metric,
                "error": "no rows; has this ticker been refreshed?"}
    latest = hist[0]
    inputs = json.loads(latest["inputs"] or "[]")
    # Resolve input provenance ids back to the rows that produced them.
    resolved = []
    for r in db.latest_metrics(con, ticker):
        pid = _prov_id(r)
        if pid in inputs:
            resolved.append({"metric": r["metric"], "provenance_id": pid,
                             "value": r["value"], "unit": r["unit"],
                             "source_url": r["source_url"],
                             "retrieved_at": r["retrieved_at"]})
    return {
        "ticker": ticker, "metric": metric,
        "value": latest["value"], "unit": latest["unit"], "as_of": latest["as_of"],
        "method": latest["method"], "formula": latest["formula"],
        "source_name": latest["source_name"], "source_url": latest["source_url"],
        "retrieved_at": latest["retrieved_at"], "confidence": latest["confidence"],
        "raw_payload": latest["payload_path"],
        "missing": json.loads(latest["missing"] or "[]"),
        "input_provenance_ids": inputs,
        "resolved_inputs": resolved,
        "history": [{"as_of": h["as_of"], "value": h["value"],
                     "first_seen": h["first_seen"], "last_seen": h["last_seen"],
                     "source_name": h["source_name"]} for h in hist],
    }


def _prov_id(row) -> str | None:
    from .provenance import Provenance
    if not row["source_name"]:
        return None
    return Provenance(
        source_name=row["source_name"], source_url=row["source_url"] or "",
        retrieved_at=row["retrieved_at"] or "", method=row["method"] or "reported",
        formula=row["formula"], inputs=tuple(json.loads(row["inputs"] or "[]")),
        confidence=row["confidence"] or "high", payload_path=row["payload_path"],
        note=row["note"]).id
