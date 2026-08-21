"""The LLM guard, the scoring rules, and portfolio-level analysis."""

from __future__ import annotations

import pytest

from ledger import config
from ledger.analyze import analyze
from ledger.guard import GuardViolation
from ledger.interpret import (
    Interpretation, allowed_number_strings, check_confidence_calibration,
    check_forbidden, interpret, verify_no_new_numbers,
)
from ledger.methods.portfolio import (
    KellyScenario, Position, base_rate_check, base_rate_for, kelly_size,
    look_through_exposure,
)
from ledger.provenance import MISSING_INPUT, Q
from ledger.scoring import LENSES, composite, rank_by


# ---------------------------------------------------------------------------
# the LLM may not write numbers
# ---------------------------------------------------------------------------


def test_guard_catches_a_fabricated_percentage(industrial, wacc):
    a = analyze(industrial, wacc=wacc, deck=config.load_deck())
    allowed = allowed_number_strings(a)
    assert verify_no_new_numbers("ROIC is 20.7%", allowed) == []
    assert verify_no_new_numbers("ROIC is around 31.4%", allowed) == ["31.4"]


def test_guard_catches_a_fabricated_multiple_and_dollar_figure(industrial, wacc):
    a = analyze(industrial, wacc=wacc, deck=config.load_deck())
    allowed = allowed_number_strings(a)
    assert verify_no_new_numbers("it trades at 19x earnings", allowed) == ["19"]
    assert verify_no_new_numbers("worth $47 per share", allowed) == ["47"]


def test_guard_allows_counts_and_years(industrial, wacc):
    a = analyze(industrial, wacc=wacc, deck=config.load_deck())
    allowed = allowed_number_strings(a)
    assert verify_no_new_numbers(
        "In 2025, 3 of the 9 Piotroski components favoured the company.",
        allowed) == []


def test_guard_rejects_ratings_and_price_targets():
    assert check_forbidden("We rate the shares a buy rating") == ["rating language"]
    assert check_forbidden("our price target is unchanged") == ["price target"]
    assert check_forbidden("fair value is materially higher") == [
        "fair-value point estimate"]
    assert check_forbidden("The market prices durable growth.") == []


def test_guard_rejects_confidence_over_null_heavy_inputs():
    low = {"populated_pct": 0.30}
    assert check_confidence_calibration("Margins will certainly rise.", low)
    assert check_confidence_calibration("Margins may rise.", low) == []
    high = {"populated_pct": 0.95}
    assert check_confidence_calibration("Margins will certainly rise.", high) == []


class _FakeClient:
    """Returns a fabricated number first, then a clean answer."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    class _Block:
        type = "text"

        def __init__(self, text):
            self.text = text

    class _Resp:
        def __init__(self, text):
            self.content = [_FakeClient._Block(text)]

    class _Messages:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kw):
            self.outer.prompts.append(kw["messages"][0]["content"])
            return _FakeClient._Resp(self.outer.replies.pop(0))

    @property
    def messages(self):
        return _FakeClient._Messages(self)


def test_interpret_retries_then_accepts(industrial, wacc):
    a = analyze(industrial, wacc=wacc, deck=config.load_deck())
    client = _FakeClient([
        "## What the market is pricing in\nROIC of 44.4% supports the price.",
        "## What the market is pricing in\nThe multiple implies durable returns.",
    ])
    out = interpret(a, client=client, max_retries=2)
    assert out.accepted
    assert "44.4" not in out.prose
    assert "REJECTED" in client.prompts[1], "the violation must be fed back"


def test_interpret_refuses_after_repeated_fabrication(industrial, wacc):
    a = analyze(industrial, wacc=wacc, deck=config.load_deck())
    client = _FakeClient(["ROIC is 44.4%.", "Still 44.4% here."])
    out = interpret(a, client=client, max_retries=2)
    assert not out.accepted
    assert out.prose == ""
    assert "44.4" in out.unverified_numbers


def test_interpret_is_unavailable_without_a_key(industrial, wacc, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    a = analyze(industrial, wacc=wacc, deck=config.load_deck())
    out = interpret(a)
    assert not out.accepted
    assert "ANTHROPIC_API_KEY" in out.violations[0]


def test_context_includes_nulls_so_the_model_can_report_them(industrial, wacc):
    from ledger.interpret import build_context
    for p in industrial.annual:
        p.drop("cash_from_operations")
    a = analyze(industrial, wacc=wacc, deck=config.load_deck())
    ctx = build_context(a)
    assert "NULL" in ctx
    assert "cash_from_operations" in ctx
    assert "COVERAGE" in ctx


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def _row(ticker, key, value, schema="industrial", blocked=None):
    q = (Q(value=value, unit="ratio", as_of="2025-12-31", prov=None, label=key)
         if value is not None
         else Q.null(MISSING_INPUT, missing=(key,), label=key))
    return {"ticker": ticker, "schema": schema, "metrics": {key: q},
            "blocked": blocked}


def test_rank_orders_by_the_selected_lens():
    rows = [_row("A", "ebit_ev_yield", 0.05), _row("B", "ebit_ev_yield", 0.12),
            _row("C", "ebit_ev_yield", 0.08)]
    r = rank_by("ebit_ev_yield", rows)
    assert [x["ticker"] for x in r.rows] == ["B", "C", "A"]


def test_lower_is_better_lenses_invert():
    rows = [_row("A", "acquirers_multiple", 14.0),
            _row("B", "acquirers_multiple", 7.0)]
    assert [x["ticker"] for x in rank_by("acquirers_multiple", rows).rows] == ["B", "A"]


def test_nulls_are_excluded_not_sorted_to_the_bottom():
    """Unknown must not read as worst."""
    rows = [_row("A", "incremental_roic", 0.20), _row("B", "incremental_roic", None)]
    r = rank_by("incremental_roic", rows)
    assert [x["ticker"] for x in r.rows] == ["A"]
    assert r.excluded[0]["ticker"] == "B"
    assert "incremental_roic" in r.excluded[0]["missing"]


def test_blocked_rows_are_excluded_from_ranking():
    rows = [_row("A", "roic", 0.20), _row("B", "roic", 0.30, blocked="sources disagree")]
    r = rank_by("roic", rows)
    assert [x["ticker"] for x in r.rows] == ["A"]
    assert r.excluded[0]["reason"] == "RECONCILIATION_FAILED"


def test_ranking_across_the_financial_boundary_raises():
    rows = [_row("A", "roic", 0.2), _row("BANK", "roic", 0.15, schema="bank")]
    with pytest.raises(GuardViolation):
        rank_by("roic", rows)


def test_composite_is_off_by_default():
    inputs = {"a": Q(value=1.0, unit="", as_of=None, prov=None),
              "b": Q(value=2.0, unit="", as_of=None, prov=None)}
    c = composite(inputs, {"a": 1, "b": 1})
    assert c.refused and c.reason == "composite_disabled"
    assert c.value.value is None


def test_composite_refuses_above_the_null_threshold():
    inputs = {f"m{i}": Q(value=1.0, unit="", as_of=None, prov=None) for i in range(4)}
    inputs["m4"] = Q.null(MISSING_INPUT, missing=("m4",))   # 20% null -> ok
    ok = composite(inputs, {k: 1 for k in inputs}, enabled=True)
    assert not ok.refused

    inputs["m3"] = Q.null(MISSING_INPUT, missing=("m3",))   # 40% null -> refuse
    bad = composite(inputs, {k: 1 for k in inputs}, enabled=True)
    assert bad.refused and bad.reason == "too_many_nulls"
    assert bad.null_fraction == pytest.approx(0.4)


def test_composite_always_shows_its_full_input_vector():
    inputs = {"a": Q(value=1.0, unit="", as_of=None, prov=None),
              "b": Q.null(MISSING_INPUT, missing=("b",))}
    c = composite(inputs, {"a": 1, "b": 1}, enabled=True)
    d = c.to_dict()
    assert set(d["input_vector"]) == {"a", "b"}
    assert "never the headline" in d["note"]


# ---------------------------------------------------------------------------
# portfolio
# ---------------------------------------------------------------------------


def test_kelly_caps_at_max_weight():
    out = kelly_size([KellyScenario("bull", 0.5, 1.0), KellyScenario("bear", 0.5, -0.3)],
                     fraction=0.25, max_weight=0.20)
    assert out["recommended_weight"] == 0.20
    assert out["capped_by_max_weight"]


def test_kelly_is_zero_when_expected_return_is_not_positive():
    out = kelly_size([KellyScenario("up", 0.4, 0.2), KellyScenario("down", 0.6, -0.2)],
                     fraction=0.25, max_weight=0.20)
    assert out["kelly_fraction"] == 0.0


def test_kelly_rejects_probabilities_that_do_not_sum_to_one():
    out = kelly_size([KellyScenario("a", 0.5, 0.2), KellyScenario("b", 0.6, -0.1)],
                     fraction=0.25, max_weight=0.2)
    assert out["kelly_fraction"] is None and "sum to" in out["error"]


def test_look_through_excludes_unmapped_names_rather_than_assuming_zero(industrial):
    """Treating an unknown exposure as zero would understate concentration."""
    e = look_through_exposure([Position("TESTCO.TO", 1.0, industrial)],
                              "oil", {"mappings": {}})
    assert e.share_of_portfolio_earnings.value is None
    assert "TESTCO.TO" in e.excluded


def test_look_through_computes_from_disclosed_segments(industrial):
    from tests.conftest import fq
    industrial.segments = {"Widgets": fq(400.0, label="Widgets"),
                           "Energy Services": fq(950.0, label="Energy Services")}
    mapping = {"mappings": {"TESTCO.TO": {"Energy Services": ["oil"],
                                          "Widgets": ["canadian_gdp"]}}}
    e = look_through_exposure([Position("TESTCO.TO", 1.0, industrial)], "oil", mapping)
    # 950 of 1350 net income
    assert e.share_of_portfolio_earnings.value == pytest.approx(950 / 1350)


def test_base_rate_is_null_when_no_table_is_loaded(tmp_path):
    q = base_rate_for("Industrials", "large", 0.15, 10, path=tmp_path / "none.csv")
    assert q.value is None
    assert "base rate table not loaded" in q.missing[0]


def test_base_rate_is_only_demanded_above_the_threshold():
    low = Q(value=0.06, unit="ratio", as_of=None, prov=None, label="g")
    out = base_rate_check(low, "Industrials", "large", 10)
    assert out["applicable"] is False
    assert "below the 10%" in out["reason"]


def test_base_rate_lookup_from_a_supplied_table(tmp_path):
    p = tmp_path / "base_rates.csv"
    p.write_text(
        "sector,size_bucket,metric,growth_bucket_low,growth_bucket_high,"
        "horizon_years,frequency,n_firms,source_name,source_url,as_of\n"
        "Industrials,large,revenue_growth,0.10,0.20,10,0.07,1420,"
        "Base Rate Book,https://example.com/base-rates,2024-12-31\n")
    high = Q(value=0.15, unit="ratio", as_of=None, prov=None, label="g")
    out = base_rate_check(high, "Industrials", "large", 10, path=p)
    assert out["applicable"]
    assert out["base_rate"]["value"] == pytest.approx(0.07)
    assert "7.0% of Industrials firms" in out["claim"]
    assert out["base_rate"]["provenance"]["source_url"].startswith("https://")
