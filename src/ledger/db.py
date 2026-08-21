"""SQLite storage.

The metrics table is keyed (ticker, metric, as_of) so history accumulates: when
a vendor restates a figure, the old row stays and the movement is visible.
That is deliberate.  A research database that silently overwrites is a database
that cannot tell you a number changed under you.

Idempotence: a row carries a content hash of its canonical JSON.  Re-running
over unchanged data produces the same hash and the write is a no-op, which is
what makes acceptance test 6 (byte-identical output) hold at the storage layer
as well as in memory.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from . import config
from .provenance import Q

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    ticker      TEXT NOT NULL,
    metric      TEXT NOT NULL,
    as_of       TEXT,
    value       REAL,
    unit        TEXT,
    reason      TEXT,
    missing     TEXT,
    method      TEXT,
    source_name TEXT,
    source_url  TEXT,
    retrieved_at TEXT,
    formula     TEXT,
    inputs      TEXT,
    confidence  TEXT,
    payload_path TEXT,
    note        TEXT,
    schema_kind TEXT,
    content_hash TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (ticker, metric, as_of, content_hash)
);
CREATE INDEX IF NOT EXISTS ix_metrics_ticker ON metrics(ticker);
CREATE INDEX IF NOT EXISTS ix_metrics_metric ON metrics(ticker, metric);

CREATE TABLE IF NOT EXISTS companies (
    ticker    TEXT PRIMARY KEY,
    name      TEXT,
    exchange  TEXT,
    sector    TEXT,
    industry  TEXT,
    currency  TEXT,
    schema_kind TEXT,
    cik       TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    ticker     TEXT,
    stage      TEXT,
    status     TEXT,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS reconciliations (
    ticker    TEXT NOT NULL,
    as_of     TEXT NOT NULL,
    blocked   INTEGER NOT NULL,
    flags     TEXT,
    reason    TEXT,
    detail    TEXT,
    PRIMARY KEY (ticker, as_of)
);

CREATE TABLE IF NOT EXISTS interpretations (
    ticker     TEXT NOT NULL,
    as_of      TEXT NOT NULL,
    model      TEXT,
    prose      TEXT,
    inputs_hash TEXT,
    created_at TEXT,
    PRIMARY KEY (ticker, as_of, inputs_hash)
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or config.path("db")
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.commit()


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    con = connect(path)
    try:
        init(con)
        yield con
        con.commit()
    finally:
        con.close()


def canonical_json(obj: Any) -> str:
    """Deterministic serialization.  Sorted keys, no whitespace drift."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(q: Q) -> str:
    return hashlib.sha256(canonical_json(q.to_dict()).encode()).hexdigest()[:16]


def upsert_metric(con: sqlite3.Connection, ticker: str, metric: str, q: Q, *,
                  schema_kind: str, now: str) -> bool:
    """Write a metric.  Returns True if this is new content.

    An identical figure re-observed only moves last_seen, so the accumulated
    history records *changes*, not refresh noise.
    """
    h = content_hash(q)
    p = q.prov
    row = con.execute(
        "SELECT 1 FROM metrics WHERE ticker=? AND metric=? AND as_of IS ? AND content_hash=?",
        (ticker, metric, q.as_of, h)).fetchone()
    if row:
        con.execute(
            "UPDATE metrics SET last_seen=? WHERE ticker=? AND metric=? "
            "AND as_of IS ? AND content_hash=?",
            (now, ticker, metric, q.as_of, h))
        return False
    con.execute(
        "INSERT INTO metrics (ticker, metric, as_of, value, unit, reason, missing,"
        " method, source_name, source_url, retrieved_at, formula, inputs,"
        " confidence, payload_path, note, schema_kind, content_hash, first_seen,"
        " last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ticker, metric, q.as_of, q.value, q.unit, q.reason,
         canonical_json(list(q.missing)), p.method if p else None,
         p.source_name if p else None, p.source_url if p else None,
         p.retrieved_at if p else None, p.formula if p else None,
         canonical_json(list(p.inputs)) if p else None,
         p.confidence if p else None, p.payload_path if p else None,
         p.note if p else None, schema_kind, h, now, now))
    return True


def history(con: sqlite3.Connection, ticker: str, metric: str) -> list[sqlite3.Row]:
    """How a figure moved between refreshes."""
    return con.execute(
        "SELECT * FROM metrics WHERE ticker=? AND metric=? "
        "ORDER BY as_of DESC, first_seen DESC", (ticker, metric)).fetchall()


def latest_metrics(con: sqlite3.Connection, ticker: str) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT m.* FROM metrics m JOIN (SELECT metric, MAX(last_seen) ls FROM "
        "metrics WHERE ticker=? GROUP BY metric) x ON m.metric=x.metric AND "
        "m.last_seen=x.ls WHERE m.ticker=? ORDER BY m.metric", (ticker, ticker)
    ).fetchall()


def upsert_company(con: sqlite3.Connection, **kw: Any) -> None:
    con.execute(
        "INSERT INTO companies (ticker,name,exchange,sector,industry,currency,"
        "schema_kind,cik,updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ticker) DO UPDATE SET name=excluded.name, "
        "exchange=excluded.exchange, sector=excluded.sector, "
        "industry=excluded.industry, currency=excluded.currency, "
        "schema_kind=excluded.schema_kind, cik=excluded.cik, "
        "updated_at=excluded.updated_at",
        (kw["ticker"], kw.get("name"), kw.get("exchange"), kw.get("sector"),
         kw.get("industry"), kw.get("currency"), kw.get("schema_kind"),
         kw.get("cik"), kw["updated_at"]))


def list_companies(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM companies ORDER BY ticker").fetchall()


def save_reconciliation(con: sqlite3.Connection, ticker: str, as_of: str,
                        rec: Mapping[str, Any]) -> None:
    con.execute(
        "INSERT INTO reconciliations (ticker,as_of,blocked,flags,reason,detail) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(ticker,as_of) DO UPDATE SET "
        "blocked=excluded.blocked, flags=excluded.flags, reason=excluded.reason, "
        "detail=excluded.detail",
        (ticker, as_of, 1 if rec.get("blocked") else 0,
         canonical_json(rec.get("flags", [])), rec.get("reason"),
         canonical_json(rec)))


def get_reconciliation(con: sqlite3.Connection, ticker: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM reconciliations WHERE ticker=? ORDER BY as_of DESC LIMIT 1",
        (ticker,)).fetchone()


def log_run(con: sqlite3.Connection, run_id: str, ticker: str, stage: str,
            status: str, detail: str, started: str, finished: str) -> None:
    con.execute(
        "INSERT INTO runs (run_id,started_at,finished_at,ticker,stage,status,detail)"
        " VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
        "finished_at=excluded.finished_at, status=excluded.status, "
        "detail=excluded.detail",
        (run_id, started, finished, ticker, stage, status, detail))


def completed_stages(con: sqlite3.Connection, ticker: str) -> set[str]:
    """Which stages already succeeded -- this is what makes refresh resumable."""
    rows = con.execute(
        "SELECT stage FROM runs WHERE ticker=? AND status='ok'", (ticker,)).fetchall()
    return {r["stage"] for r in rows}
