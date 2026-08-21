"""The seven acceptance tests.

These are the conditions the system must satisfy before any of its output is
trusted.  Each test name states the requirement it enforces.

Two of the seven (1 and 4) have a *live* variant that needs real filings and
network access; those are marked and skip cleanly when a filing fixture is not
present, printing what to supply.  Their machinery is tested here against
synthetic data, so a skip means "not yet run against your filing", never
"untested code".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ledger import config
from ledger.analyze import analyze
from ledger.db import canonical_json
from ledger.methods import cyclical, valuation
from ledger.model import FORBIDDEN_FOR_FINANCIALS
from ledger.provenance import NOT_APPLICABLE, Q
from ledger.reconcile import (RECONCILIATION_FAILED_FLAG, reconcile)
from ledger.verify import compare_to_filing, computed_figures

FIXTURES = Path(__file__).parent / "fixtures"
FILINGS = FIXTURES / "filings"


# ===========================================================================
# 1. Every computed metric reconciles to the latest annual filing
# ===========================================================================


def test_acceptance_1_filing_comparison_detects_agreement(industrial, wacc):
    """The comparison machinery reports agreement when the figures agree."""
    a = analyze(industrial, wacc=wacc, deck=config.load_deck())
    computed = computed_figures(industrial, a)
    filing = {
        "ticker": "TESTCO.TO", "period_end": "2025-12-31",
        "source_name": "TEST FIXTURE annual statements",
        "source_url": "test://filing", "unit_scale": 1,
        "figures": {"revenue": 10000, "net_income": 1350,
                    "operating_income": 2000, "total_assets": 9700,
                    "cash_from_operations": 2200, "total_equity": 4700,
                    "free_cash_flow": 1200},
    }
    cmp = compare_to_filing(computed, filing, tolerance_pct=0.5)
    assert cmp.compared == 7
    assert cmp.reconciles, cmp.table()
    print("\n" + cmp.table())


def test_acceptance_1_filing_comparison_detects_a_real_mismatch(industrial, wacc):
    """And -- more importantly -- reports disagreement when they disagree."""
    a = analyze(industrial, wacc=wacc, deck=config.load_deck())
    computed = computed_figures(industrial, a)
    filing = {
        "ticker": "TESTCO.TO", "period_end": "2025-12-31",
        "source_name": "TEST FIXTURE", "source_url": "test://filing",
        "figures": {"revenue": 10000, "net_income": 1600},   # 1600 != 1350
    }
    cmp = compare_to_filing(computed, filing)
    assert not cmp.reconciles
    assert [r["field"] for r in cmp.mismatches] == ["net_income"]


def test_acceptance_1_undisclosed_line_is_not_a_mismatch(industrial, wacc):
    """A line the filing does not disclose must not read as disagreement."""
    a = analyze(industrial, wacc=wacc, deck=config.load_deck())
    cmp = compare_to_filing(computed_figures(industrial, a),
                            {"figures": {"revenue": 10000}})
    statuses = {r["field"]: r["status"] for r in cmp.rows}
    assert statuses["revenue"] == "match"
    assert statuses["net_income"] == "not disclosed in filing"
    assert not cmp.mismatches


@pytest.mark.skipif(not list(FILINGS.glob("*.json")) if FILINGS.exists() else True,
                    reason=("no real filing fixture supplied. Drop a JSON file in "
                            "tests/fixtures/filings/ (see ledger.verify docstring) "
                            "and run `ledger verify-filing TICKER --filing <path>` "
                            "on a machine with network access."))
def test_acceptance_1_live_reconciles_to_real_filing():
    """LIVE: every computed metric reconciles to a real annual filing."""
    from ledger.db import session
    from ledger.refresh import refresh_ticker
    from ledger.verify import load_filing

    for path in sorted(FILINGS.glob("*.json")):
        filing = load_filing(path)
        with session() as con:
            r = refresh_ticker(filing["ticker"], wacc=Q.null("not_needed"),
                               deck=config.load_deck(), con=con)
        assert r.company is not None, f"no data pulled for {filing['ticker']}"
        cmp = compare_to_filing(computed_figures(r.company, r.analysis), filing)
        print("\n" + cmp.table())
        assert cmp.reconciles, f"{filing['ticker']} does not reconcile:\n{cmp.table()}"


# ===========================================================================
# 2. Delete an input field -> dependent metric is None, names the field, and
#    no default is substituted anywhere
# ===========================================================================


def test_acceptance_2_deleted_field_nulls_dependents_and_names_it(industrial, wacc):
    deck = config.load_deck()
    before = analyze(industrial, wacc=wacc, deck=deck)
    assert before.metrics["free_cash_flow"].ok

    for p in industrial.annual:
        p.drop("cash_from_operations")
    after = analyze(industrial, wacc=wacc, deck=deck)

    fcf = after.metrics["free_cash_flow"]
    assert fcf.value is None, "dependent metric must be None, not 0"
    assert any("cash_from_operations" in m for m in fcf.missing), (
        f"the missing field must be named; got {fcf.missing}")

    report = {r["metric"]: r for r in after.missing_report()}
    assert "free_cash_flow" in report
    assert any("cash_from_operations" in m for m in report["free_cash_flow"]["missing"])


def test_acceptance_2_no_default_is_substituted_anywhere(industrial, wacc):
    """Removing an input may only turn values into nulls -- never into other
    values.  A metric that changes to a *different number* proves a default was
    substituted somewhere."""
    deck = config.load_deck()
    before = analyze(industrial, wacc=wacc, deck=deck)
    for p in industrial.annual:
        p.drop("cash_from_operations")
    after = analyze(industrial, wacc=wacc, deck=deck)

    substituted = []
    for name, q_before in before.metrics.items():
        q_after = after.metrics[name]
        if q_before.ok and q_after.ok and q_before.value != q_after.value:
            substituted.append((name, q_before.value, q_after.value))
    assert not substituted, (
        f"removing an input changed these metrics to different values instead of "
        f"nulling them -- a default was substituted: {substituted}")

    nulled = [n for n, q in before.metrics.items()
              if q.ok and not after.metrics[n].ok]
    assert nulled, "expected at least one dependent metric to go null"


def test_acceptance_2_nulls_are_never_rendered_as_zero(industrial, wacc):
    for p in industrial.annual:
        p.drop("cash_from_operations")
    a = analyze(industrial, wacc=wacc, deck=config.load_deck())
    for name, q in a.metrics.items():
        if not q.ok:
            assert q.value is None, f"{name} rendered a null as {q.value!r}"
            d = q.to_dict()
            assert d["value"] is None
            assert d["reason"], f"{name} is null with no reason given"


# ===========================================================================
# 3. A bank must not carry Altman Z, ROIC, EV or gross margin -- absent, not zero
# ===========================================================================


def test_acceptance_3_bank_has_no_industrial_metrics(bank, wacc):
    a = analyze(bank, wacc=wacc, deck=config.load_deck())
    for forbidden in ("altman_z", "roic", "enterprise_value", "gross_margin",
                      "current_ratio", "incremental_roic", "owner_earnings_low",
                      "epv", "acquirers_multiple", "nopat", "invested_capital"):
        assert forbidden not in a.metrics, (
            f"{forbidden} must be ABSENT from a bank's metric set, not present "
            f"as {a.metrics.get(forbidden)}")


def test_acceptance_3_bank_metrics_are_not_zero_placeholders(bank, wacc):
    a = analyze(bank, wacc=wacc, deck=config.load_deck())
    for name, q in a.metrics.items():
        assert not (q.value == 0 and name in FORBIDDEN_FOR_FINANCIALS)
    assert a.metrics["rotce"].ok
    assert a.metrics["price_to_tangible_book"].ok
    assert a.metrics["cet1_ratio"].ok


def test_acceptance_3_bank_schema_refuses_industrial_fields(bank):
    p = bank.latest_annual()
    q = p.get("gross_profit")
    assert q.reason == NOT_APPLICABLE
    assert not q.ok and q.value is None
    with pytest.raises(KeyError):
        p.set("total_current_assets", Q.null("x"))


def test_acceptance_3_altman_is_not_applicable_not_zero(bank):
    from ledger.methods.forensics import altman_z
    card = altman_z(bank)
    assert card.total.value is None
    assert card.total.reason == NOT_APPLICABLE
    assert card.components == {}


def test_acceptance_3_scores_never_cross_the_financial_boundary(bank, industrial, wacc):
    from ledger.guard import GuardViolation
    from ledger.scoring import rank_by
    rows = [{"ticker": "TESTBANK.TO", "schema": "bank", "metrics": {}},
            {"ticker": "TESTCO.TO", "schema": "industrial", "metrics": {}}]
    with pytest.raises(GuardViolation):
        rank_by("roic", rows)


# ===========================================================================
# 4. An oil producer shows TTM and mid-cycle side by side, with the deck printed
# ===========================================================================


def test_acceptance_4_oil_producer_shows_both_valuations(oil_producer, wacc,
                                                         realized_wti):
    deck = config.load_deck()
    a = analyze(oil_producer, wacc=wacc, deck=deck,
                realized_prices={"wti": realized_wti})

    assert a.metrics["ebitda_ttm"].ok
    assert a.metrics["ebitda_mid_cycle"].ok
    assert a.metrics["ebitda_ttm"].value != a.metrics["ebitda_mid_cycle"].value

    assert a.metrics["ev_to_ebitda_ttm"].ok
    assert a.metrics["ev_to_ebitda_mid_cycle"].ok
    assert (a.metrics["ev_to_ebitda_ttm"].value
            != a.metrics["ev_to_ebitda_mid_cycle"].value)

    assert a.metrics["cyclical_distortion"].ok
    assert a.metrics["reserve_life_index"].value == pytest.approx(12.0)
    assert a.metrics["corporate_breakeven_wti"].ok
    assert a.metrics["net_debt_to_mid_cycle_ebitda"].ok


def test_acceptance_4_deck_assumptions_are_printed_with_the_output(oil_producer,
                                                                   wacc, realized_wti):
    a = analyze(oil_producer, wacc=wacc, deck=config.load_deck(),
                realized_prices={"wti": realized_wti})
    mc = a.extras["mid_cycle"]
    assert mc["deck"]["deck_version"]
    assert mc["deck"]["prices"]["wti"]["value"] == 65.0
    assert mc["deck"]["prices"]["wti"]["set_on"]
    assert "user assumptions" in mc["deck"]["disclaimer"].lower()
    assert "NORMALIZED" in mc["note"]
    assert a.metrics["ebitda_mid_cycle"].prov.method == "normalized"


def test_acceptance_4_non_cyclical_says_so_rather_than_faking_a_deck(industrial,
                                                                     wacc):
    a = analyze(industrial, wacc=wacc, deck=config.load_deck())
    assert a.extras["mid_cycle"]["applicable"] is False
    assert "ebitda_mid_cycle" not in a.metrics


def test_acceptance_4_stale_deck_warns(oil_producer, wacc, realized_wti):
    from datetime import date
    a = analyze(oil_producer, wacc=wacc, deck=config.load_deck(),
                realized_prices={"wti": realized_wti},
                today=date(2030, 1, 1))
    warnings = a.extras["mid_cycle"]["deck"]["warnings"]
    assert any("stale" in w or "days ago" in w for w in warnings)


# ===========================================================================
# 5. A >2% disagreement between two providers blocks and flags the row
# ===========================================================================


def _anchor(v, label, source):
    return Q.reported(v, unit="CAD", as_of="2025-12-31", source_name=source,
                      source_url=f"http://{source}", retrieved_at="2026-01-15T12:00:00+00:00",
                      label=label)


def test_acceptance_5_disagreement_blocks_and_flags():
    primary = {"last_close": _anchor(24.00, "last_close", "fmp"),
               "diluted_shares": _anchor(1000.0, "diluted_shares", "fmp"),
               "revenue_ttm": _anchor(10000.0, "revenue_ttm", "fmp")}
    secondary = {"last_close": _anchor(24.00, "last_close", "eodhd"),
                 "diluted_shares": _anchor(1000.0, "diluted_shares", "eodhd"),
                 "revenue_ttm": _anchor(10400.0, "revenue_ttm", "eodhd")}  # 3.85%
    rec = reconcile("TESTCO.TO", primary, secondary, primary_source="fmp",
                    secondary_source="eodhd")
    assert rec.blocked
    assert RECONCILIATION_FAILED_FLAG in rec.flags
    msg = rec.blocking_message()
    assert "revenue_ttm" in msg and "3.8" in msg


def test_acceptance_5_within_tolerance_passes():
    primary = {"last_close": _anchor(24.00, "last_close", "fmp"),
               "diluted_shares": _anchor(1000.0, "diluted_shares", "fmp"),
               "revenue_ttm": _anchor(10000.0, "revenue_ttm", "fmp")}
    secondary = {"last_close": _anchor(24.10, "last_close", "eodhd"),   # 0.41%
                 "diluted_shares": _anchor(1010.0, "diluted_shares", "eodhd"),  # 0.99%
                 "revenue_ttm": _anchor(10150.0, "revenue_ttm", "eodhd")}  # 1.48%
    rec = reconcile("TESTCO.TO", primary, secondary, primary_source="fmp",
                    secondary_source="eodhd")
    assert not rec.blocked
    assert RECONCILIATION_FAILED_FLAG not in rec.flags


def test_acceptance_5_blocked_row_is_not_scoreable(industrial, wacc):
    primary = {"last_close": _anchor(24.00, "last_close", "fmp"),
               "diluted_shares": _anchor(1000.0, "diluted_shares", "fmp"),
               "revenue_ttm": _anchor(10000.0, "revenue_ttm", "fmp")}
    secondary = dict(primary, revenue_ttm=_anchor(12000.0, "revenue_ttm", "eodhd"))
    rec = reconcile("TESTCO.TO", primary, secondary, primary_source="fmp",
                    secondary_source="eodhd")
    a = analyze(industrial, wacc=wacc, deck=config.load_deck(), reconciliation=rec)
    assert a.blocked
    assert all(not q.ok for q in a.metrics.values()), (
        "a blocked row must expose no scoreable metric")
    assert all(q.reason == "reconciliation_failed" for q in a.metrics.values())


def test_acceptance_5_no_second_source_also_blocks():
    primary = {"last_close": _anchor(24.00, "last_close", "fmp")}
    rec = reconcile("X.TO", primary, {}, primary_source="fmp",
                    secondary_source="none")
    assert rec.blocked
    assert "NO_SECOND_SOURCE" in rec.flags


def test_acceptance_5_disagreement_measure_is_symmetric():
    from ledger.reconcile import relative_disagreement
    assert (relative_disagreement(100, 104)
            == pytest.approx(relative_disagreement(104, 100)))


def test_acceptance_5_the_gate_does_not_silently_pick_a_source():
    """The blocking message must name BOTH figures, not resolve to one."""
    primary = {"last_close": _anchor(24.00, "last_close", "fmp"),
               "diluted_shares": _anchor(1000.0, "diluted_shares", "fmp"),
               "revenue_ttm": _anchor(10000.0, "revenue_ttm", "fmp")}
    secondary = dict(primary, revenue_ttm=_anchor(12000.0, "revenue_ttm", "eodhd"))
    rec = reconcile("X.TO", primary, secondary, primary_source="fmp",
                    secondary_source="eodhd")
    msg = rec.blocking_message()
    assert "fmp=10000.0" in msg and "eodhd=12000.0" in msg


# ===========================================================================
# 6. Two consecutive runs on unchanged data produce byte-identical output
# ===========================================================================


def test_acceptance_6_recomputation_is_byte_identical(industrial, wacc):
    deck = config.load_deck()
    first = canonical_json(analyze(industrial, wacc=wacc, deck=deck).to_dict())
    second = canonical_json(analyze(industrial, wacc=wacc, deck=deck).to_dict())
    assert first == second
    assert len(first) > 5000, "sanity: the payload should be substantial"


def test_acceptance_6_holds_for_banks_and_cyclicals(bank, oil_producer, wacc,
                                                    realized_wti):
    deck = config.load_deck()
    b1 = canonical_json(analyze(bank, wacc=wacc, deck=deck).to_dict())
    b2 = canonical_json(analyze(bank, wacc=wacc, deck=deck).to_dict())
    assert b1 == b2
    o1 = canonical_json(analyze(oil_producer, wacc=wacc, deck=deck,
                                realized_prices={"wti": realized_wti}).to_dict())
    o2 = canonical_json(analyze(oil_producer, wacc=wacc, deck=deck,
                                realized_prices={"wti": realized_wti}).to_dict())
    assert o1 == o2


def test_acceptance_6_provenance_ids_are_stable_across_runs(industrial, wacc):
    deck = config.load_deck()
    a1 = analyze(industrial, wacc=wacc, deck=deck)
    a2 = analyze(industrial, wacc=wacc, deck=deck)
    for name, q in a1.metrics.items():
        if q.ok:
            assert q.prov.id == a2.metrics[name].prov.id, f"{name} provenance drifted"


def test_acceptance_6_storage_is_idempotent(industrial, wacc, tmp_path):
    from ledger.db import session, upsert_metric
    deck = config.load_deck()
    a = analyze(industrial, wacc=wacc, deck=deck)
    db_path = tmp_path / "t.db"
    with session(db_path) as con:
        new1 = sum(upsert_metric(con, "TESTCO.TO", n, q, schema_kind="industrial",
                                 now="2026-01-15T12:00:00+00:00")
                   for n, q in a.metrics.items())
        new2 = sum(upsert_metric(con, "TESTCO.TO", n, q, schema_kind="industrial",
                                 now="2026-01-16T12:00:00+00:00")
                   for n, q in a.metrics.items())
    assert new1 > 0
    assert new2 == 0, "a second identical run must write no new rows"


# ===========================================================================
# 7. No hardcoded financial constants outside the config deck
# ===========================================================================


def test_acceptance_7_no_hardcoded_financial_constants():
    r = subprocess.run(
        [sys.executable, str(config.ROOT / "scripts" / "check_no_hardcoded_constants.py")],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    print("\n" + r.stdout.strip())


def test_acceptance_7_model_coefficients_live_in_config_with_citations():
    from ledger.methods.forensics import models
    m = models()
    for block in ("altman_z", "altman_z_double_prime", "beneish_m", "montier_c",
                  "piotroski_f"):
        assert m[block].get("citation"), f"{block} has no citation"


def test_acceptance_7_deck_values_come_from_the_config_deck_only():
    deck = config.load_deck()
    q = deck.q("wti")
    assert q.prov.source_url.startswith("file://config/deck.yaml")
    assert "USER ASSUMPTION" in q.prov.note
    assert q.prov.confidence == "low"
