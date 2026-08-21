"""Adapter mapping, HTTP behaviour and classification.

These test the adapter *contract* -- field mapping, provenance stamping, key
redaction, rate limiting, immutable archiving -- against recorded payload
shapes.  They do not hit the network.

What they cannot prove is that a vendor's live response still matches the shape
recorded here; only `ledger doctor` and a real pull can. That gap is the reason
the reconciliation gate exists.
"""

from __future__ import annotations

import json

import pytest

from ledger.classify import classify
from ledger.http import Fetched, RateLimiter, _cited_url, archive, read_archived
from ledger.providers.base import build_period, default_unit_for, field_q
from ledger.providers.fmp import FIELD_MAP as FMP_MAP


def fetched(source="fmp", url="https://api/x"):
    return Fetched(source=source, url=url, retrieved_at="2026-01-15T12:00:00+00:00",
                   payload_path="data/raw/fmp/X/p.json", body={}, from_cache=False,
                   status=200)


def test_field_q_stamps_full_provenance():
    q = field_q(fetched(), {"revenue": 1234.5}, "revenue", unit="CAD",
                as_of="2025-12-31", label="revenue")
    assert q.value == 1234.5
    assert q.prov.source_name == "fmp"
    assert q.prov.source_url == "https://api/x"
    assert q.prov.retrieved_at == "2026-01-15T12:00:00+00:00"
    assert q.prov.payload_path == "data/raw/fmp/X/p.json"
    assert q.prov.method == "reported"


def test_field_q_names_the_vendor_key_when_absent():
    q = field_q(fetched(), {}, "totalRevenue", unit="CAD", as_of="2025-12-31",
                label="revenue")
    assert q.value is None
    assert "totalRevenue" in q.missing[0]
    assert "fmp" in q.missing[0]


def test_field_q_rejects_nulls_and_non_numerics():
    assert field_q(fetched(), {"r": None}, "r", unit="CAD", as_of=None,
                   label="revenue").value is None
    assert field_q(fetched(), {"r": "n/a"}, "r", unit="CAD", as_of=None,
                   label="revenue").value is None
    assert field_q(fetched(), {"r": True}, "r", unit="CAD", as_of=None,
                   label="revenue").value is None


def test_yahoo_values_are_flagged_low_confidence():
    q = field_q(fetched("yahoo"), {"totalRevenue": 100}, "totalRevenue", unit="CAD",
                as_of="2025-12-31", label="revenue", confidence="low")
    assert q.prov.confidence == "low"


def test_build_period_maps_vendor_fields_onto_the_canonical_schema():
    income = {"revenue": 10000, "netIncome": 1350, "operatingIncome": 2000}
    balance = {"totalAssets": 9700, "cashAndCashEquivalents": 500}
    cash = {"netCashProvidedByOperatingActivities": 2200,
            "capitalExpenditure": -1000}
    p = build_period(
        {"income": (fetched(), income), "balance": (fetched(), balance),
         "cashflow": (fetched(), cash)},
        FMP_MAP, ticker="X.TO", period_end="2025-12-31", period_type="FY",
        currency="CAD", schema="industrial", confidence="high", source_name="fmp",
        unit_for=default_unit_for("CAD"))
    assert p.get("revenue").value == 10000
    assert p.get("cash_from_operations").value == 2200
    assert p.get("capital_expenditure").value == -1000
    assert p.get("inventory").value is None          # absent from the payload
    assert "inventory" in p.get("inventory").missing[0]


def test_build_period_will_not_write_a_field_outside_the_schema():
    p = build_period({"income": (fetched(), {"revenue": 1})}, FMP_MAP,
                     ticker="B.TO", period_end="2025-10-31", period_type="FY",
                     currency="CAD", schema="bank", confidence="high",
                     source_name="fmp", unit_for=default_unit_for("CAD"))
    assert "revenue" not in p.fields          # silently skipped, never mis-filed
    assert p.get("revenue").is_na


def test_units_are_assigned_per_field():
    u = default_unit_for("CAD")
    assert u("revenue") == "CAD"
    assert u("diluted_shares") == "shares"
    assert u("cet1_ratio") == "ratio"
    assert u("eps_diluted") == "CAD/share"


def test_api_keys_never_reach_provenance_or_logs():
    url = _cited_url("https://api/x", {"apikey": "SECRET", "symbol": "RY.TO"},
                     ("apikey",))
    assert "SECRET" not in url
    assert "REDACTED" in url
    assert "symbol=RY.TO" in url


def test_rate_limiter_blocks_past_the_window():
    t = [0.0]
    rl = RateLimiter(per_minute=3)
    rl.acquire(sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    rl.acquire(sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    rl.acquire(sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert t[0] == 0.0
    rl.acquire(sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert t[0] >= 60.0


def test_archive_is_immutable_and_reproducible(tmp_path, monkeypatch):
    from ledger import config, http
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "path", lambda k: tmp_path / k)
    body = {"revenue": 10000}
    p1 = http.archive("fmp", "X.TO", "https://api/x", body, "2026-01-15T12:00:00+00:00")
    p2 = http.archive("fmp", "X.TO", "https://api/x", body, "2026-01-15T12:00:00+00:00")
    assert p1 != p2, "an archived payload must never be overwritten"
    # the original bytes reproduce the figure
    assert http.read_archived(p1) == body
    assert json.loads((tmp_path / p1).read_text())["_meta"]["url"] == "https://api/x"


def test_classification_routes_canadian_names():
    assert classify("Financials", "Diversified Banks", "Royal Bank of Canada") == "bank"
    assert classify("Financials", "Life & Health Insurance", "Manulife") == "insurer"
    assert classify("Financials", "Asset Management", "Brookfield") == "industrial"
    assert classify("Energy", "Oil & Gas E&P", "Canadian Natural") == "industrial"
    assert classify("Financials", "", "Unknown Financial Corp") == "bank"


def test_unknown_financials_route_to_the_restrictive_schema():
    """Withholding a metric is an inconvenience; printing an Altman Z for a bank
    is a defect."""
    assert classify("Financials", "Something Unrecognised", "Mystery Corp") == "bank"


def test_provider_registry_orders_yahoo_last():
    from ledger import providers
    order = providers.configured("fundamentals")
    assert order[-1] == "yahoo"
    assert providers.get("yahoo").confidence == "low"
