"""End-to-end: a stub provider through refresh, storage, audit and the dashboard.

No network.  A stub adapter stands in for a vendor so the whole pipeline --
pull, reconcile, compute, store, audit, render -- is exercised in one test.
"""

from __future__ import annotations

import json

import pytest

from ledger import config, db
from ledger.audit import audit_rows, audit_summary, trace
from ledger.model import Company, Period
from ledger.provenance import Q
from ledger.refresh import RefreshResult, SourceResult, anchors_from, build_company
from tests.conftest import _industrial_year, fq, make_period


def stub_source(provider: str, *, revenue_scale: float = 1.0,
                close: float = 24.0) -> SourceResult:
    annual = []
    for i, d in enumerate(["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31",
                           "2021-12-31", "2020-12-31"]):
        f = _industrial_year(i)
        f["revenue"] = f["revenue"] * revenue_scale
        annual.append(make_period("TESTCO.TO", d, f))
    quarterly = [make_period("TESTCO.TO", d, {"revenue": 2500 * revenue_scale})
                 for d in ("2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31")]
    for p in quarterly:
        p.period_type = "Q"
    from ledger.providers.base import Quote
    return SourceResult(
        provider=provider, ok=True, detail=f"{provider}: stub",
        profile={"name": "Test Industrial Corp", "sector": "Industrials",
                 "industry": "Industrial Machinery", "currency": "CAD",
                 "exchange": "TSX", "cik": None, "schema": "industrial"},
        annual=annual, quarterly=quarterly,
        quote=Quote(last_close=fq(close, unit="CAD", label="last_close"),
                    market_cap=fq(close * 1000, unit="CAD", label="market_cap"),
                    shares_outstanding=fq(1000.0, unit="shares",
                                          label="shares_outstanding")))


def test_anchors_extracted_from_a_source():
    a = anchors_from(stub_source("fmp"))
    assert set(a) == {"last_close", "diluted_shares", "revenue_ttm"}
    assert a["last_close"].value == 24.0
    assert a["diluted_shares"].value == 1000
    assert a["revenue_ttm"].value == pytest.approx(10000.0)   # 4 x 2500


def test_company_is_built_from_the_primary_source_only():
    """Vendors are never blended field-by-field into one statement."""
    primary = stub_source("fmp")
    other = stub_source("eodhd", revenue_scale=1.5)
    co = build_company("TESTCO.TO", primary, [other])
    assert co.fy("revenue", 0).value == pytest.approx(10000.0)
    assert co.fy("revenue", 0).prov.source_name == "TEST FIXTURE"
    assert co.schema == "industrial"


def test_full_pipeline_stores_and_audits(tmp_path, monkeypatch, wacc):
    import ledger.refresh as R

    sources = {"fmp": stub_source("fmp"), "eodhd": stub_source("eodhd")}
    monkeypatch.setattr(R, "pull", lambda name, ticker, **kw: sources[name])

    db_path = tmp_path / "ledger.db"
    with db.session(db_path) as con:
        r = R.refresh_ticker("TESTCO.TO", wacc=wacc, deck=config.load_deck(),
                             providers=["fmp", "eodhd"], con=con)
        assert r.analysis is not None
        assert not r.reconciliation.blocked
        assert r.stages["store"] == "ok"

        s = audit_summary(con, "TESTCO.TO")
        assert s["metrics_populated"] > 20
        assert s["as_of"] == "2025-12-31"

        rows = audit_rows(con, "TESTCO.TO", include_statements=False)
        by = {x["metric"]: x for x in rows}
        assert by["roic"]["value"] == pytest.approx(0.20740740740740743, abs=1e-9)
        assert by["roic"]["method"] == "derived"
        assert by["roic"]["formula"] == "nopat / invested_capital"
        assert by["roic"]["input_provenance_ids"]

        # every populated metric must have a source url to click through to
        for x in rows:
            if x["value"] is not None:
                assert x["source_url"], f"{x['metric']} has no source url"

        t = trace(con, "TESTCO.TO", "roic")
        assert t["formula"] == "nopat / invested_capital"
        assert t["history"]


def test_pipeline_blocks_on_provider_disagreement(tmp_path, monkeypatch, wacc):
    import ledger.refresh as R

    sources = {"fmp": stub_source("fmp"),
               "eodhd": stub_source("eodhd", revenue_scale=1.20)}  # 20% apart
    monkeypatch.setattr(R, "pull", lambda name, ticker, **kw: sources[name])

    with db.session(tmp_path / "l.db") as con:
        r = R.refresh_ticker("TESTCO.TO", wacc=wacc, deck=config.load_deck(),
                             providers=["fmp", "eodhd"], con=con)
        assert r.reconciliation.blocked
        assert r.stages["reconcile"] == "blocked"
        assert all(not q.ok for q in r.analysis.metrics.values())
        rec = db.get_reconciliation(con, "TESTCO.TO")
        assert rec["blocked"] == 1
        assert "RECONCILIATION_FAILED" in rec["flags"]


def test_pipeline_flags_a_low_confidence_cross_check(tmp_path, monkeypatch, wacc):
    """Agreement with Yahoo is not evidence, and the flag says so."""
    import ledger.refresh as R

    sources = {"fmp": stub_source("fmp"), "yahoo": stub_source("yahoo")}
    monkeypatch.setattr(R, "pull", lambda name, ticker, **kw: sources[name])
    with db.session(tmp_path / "l.db") as con:
        r = R.refresh_ticker("TESTCO.TO", wacc=wacc, deck=config.load_deck(),
                             providers=["fmp", "yahoo"], con=con)
    assert "CROSS_CHECK_IS_LOW_CONFIDENCE_SOURCE" in r.reconciliation.flags


def test_one_bad_ticker_does_not_abort_the_universe(monkeypatch, wacc):
    import ledger.refresh as R

    def boom(ticker, **kw):
        if ticker == "BAD.TO":
            raise RuntimeError("provider exploded")
        return RefreshResult(ticker=ticker, run_id="x", company=None,
                             analysis=None, reconciliation=None)

    monkeypatch.setattr(R, "refresh_ticker", boom)
    out = R.refresh_universe(["OK.TO", "BAD.TO", "OK2.TO"], wacc=wacc)
    assert [r.ticker for r in out] == ["OK.TO", "BAD.TO", "OK2.TO"]
    assert "provider exploded" in out[1].errors[0]


def test_dashboard_renders_from_the_database_only(tmp_path, monkeypatch, wacc):
    import ledger.refresh as R
    from fastapi.testclient import TestClient

    sources = {"fmp": stub_source("fmp"), "eodhd": stub_source("eodhd")}
    monkeypatch.setattr(R, "pull", lambda name, ticker, **kw: sources[name])
    db_path = tmp_path / "ledger.db"
    with db.session(db_path) as con:
        R.refresh_ticker("TESTCO.TO", wacc=wacc, deck=config.load_deck(),
                         providers=["fmp", "eodhd"], con=con)

    monkeypatch.setattr(config, "path", lambda k: db_path if k == "db"
                        else config.ROOT / config.settings()["paths"][k])
    import ledger.web.app as W
    monkeypatch.setattr(W.config, "path", lambda k: db_path if k == "db"
                        else config.ROOT / config.settings()["paths"][k])

    client = TestClient(W.app)
    idx = client.get("/")
    assert idx.status_code == 200
    assert "TESTCO.TO" in idx.text

    page = client.get("/company/TESTCO.TO")
    assert page.status_code == 200
    assert "as of" in page.text
    assert "2025-12-31" in page.text
    assert "populated" in page.text
    assert "/audit/TESTCO.TO" in page.text

    aud = client.get("/audit/TESTCO.TO")
    assert aud.status_code == 200
    assert "nopat / invested_capital" in aud.text

    api = client.get("/api/trace/TESTCO.TO/roic").json()
    assert api["formula"] == "nopat / invested_capital"


def test_dashboard_shows_insufficient_data_not_a_number(tmp_path, monkeypatch, wacc):
    import ledger.refresh as R
    from fastapi.testclient import TestClient

    src = stub_source("fmp")
    for p in src.annual:
        p.drop("cash_from_operations")
    sources = {"fmp": src, "eodhd": stub_source("eodhd")}
    monkeypatch.setattr(R, "pull", lambda name, ticker, **kw: sources[name])
    db_path = tmp_path / "ledger.db"
    with db.session(db_path) as con:
        R.refresh_ticker("TESTCO.TO", wacc=wacc, deck=config.load_deck(),
                         providers=["fmp", "eodhd"], con=con)

    import ledger.web.app as W
    monkeypatch.setattr(W.config, "path", lambda k: db_path if k == "db"
                        else config.ROOT / config.settings()["paths"][k])
    page = TestClient(W.app).get("/company/TESTCO.TO")
    assert "insufficient data" in page.text
    assert "cash_from_operations" in page.text, (
        "the UI must name the missing field, not just say 'no data'")
