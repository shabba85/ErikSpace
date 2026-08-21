"""Method-layer unit tests.

Expected values are literals derived by hand from the fixture arithmetic (the
derivation is shown in each comment), so a test failure means the implementation
changed -- not that the test re-ran the implementation.
"""

from __future__ import annotations

import pytest

from ledger.methods import banks, capital, cashflow, cyclical, forensics, returns, valuation
from ledger.provenance import MISSING_INPUT, NOT_APPLICABLE, UNDEFINED_MATH, Q

APPROX = 1e-9


# ---------------------------------------------------------------------------
# cash flow
# ---------------------------------------------------------------------------


def test_free_cash_flow(industrial):
    # CFO 2200 - |capex 1000| = 1200
    assert cashflow.free_cash_flow(industrial, 0).value == pytest.approx(1200.0)


def test_cash_tax_rate_uses_cash_taxes_not_book(industrial):
    # cash taxes paid 400 / pretax 1800 = 0.2222...  (book rate would be 450/1800 = 0.25)
    q = cashflow.cash_tax_rate(industrial, 0)
    assert q.value == pytest.approx(0.2222222222222222, abs=APPROX)
    assert q.value != pytest.approx(0.25)


def test_maintenance_capex_ppe_trend(industrial):
    # PP&E intensity is 0.60 in every year; revenue change 10000-9200 = 800
    # growth capex = 800 * 0.60 = 480; maintenance = 1000 - 480 = 520
    assert cashflow.maintenance_capex_ppe_trend(industrial, 0).value == pytest.approx(520.0)


def test_maintenance_capex_is_a_band_not_a_point(industrial):
    r = cashflow.maintenance_capex(industrial, 0)
    assert r.low.value == pytest.approx(520.0)      # PP&E-to-sales
    assert r.high.value == pytest.approx(800.0)     # D&A proxy
    assert r.method_low == "PP&E-to-sales trend"
    assert r.method_high == "D&A proxy"


def test_owner_earnings_band(industrial):
    # base = NI 1350 + D&A 800 + other non-cash 50 = 2200
    # low  = 2200 - 800 (higher maint estimate) = 1400
    # high = 2200 - 520 = 1680
    oe = cashflow.owner_earnings(industrial, 0)
    assert oe.low.value == pytest.approx(1400.0)
    assert oe.high.value == pytest.approx(1680.0)


def test_owner_earnings_null_when_maintenance_capex_uncomputable(industrial):
    for p in industrial.annual:
        p.drop("ppe_net")
        p.drop("depreciation_amortization_cf")
        p.drop("depreciation_amortization")
    oe = cashflow.owner_earnings(industrial, 0)
    assert oe.low.value is None and oe.high.value is None


def test_fcf_conversion(industrial):
    # 1200 / 1350
    assert cashflow.fcf_conversion(industrial, 0).value == pytest.approx(
        0.8888888888888888, abs=APPROX)


def test_fcf_conversion_stats_reports_dispersion(industrial):
    s = cashflow.fcf_conversion_stats(industrial, 5)
    assert s["fcf_conversion_mean"].ok
    assert s["fcf_conversion_stdev"].ok
    assert s["fcf_conversion_years_used"].value == 5


def test_fcf_conversion_stats_refuses_on_short_history(industrial):
    industrial.annual = industrial.annual[:2]
    s = cashflow.fcf_conversion_stats(industrial, 5)
    assert s["fcf_conversion_mean"].value is None


# ---------------------------------------------------------------------------
# returns on capital
# ---------------------------------------------------------------------------


def test_nopat_and_invested_capital(industrial):
    # NOPAT = EBIT 2000 * (1 - 0.22222) = 1555.5556
    # IC = debt 3300 + equity 4700 - cash 500 = 7500
    assert returns.nopat(industrial, 0).value == pytest.approx(1555.5555555555557)
    assert returns.invested_capital(industrial, 0).value == pytest.approx(7500.0)


def test_roic(industrial):
    assert returns.roic(industrial, 0).value == pytest.approx(
        0.20740740740740743, abs=APPROX)


def test_incremental_roic(industrial):
    # (1555.5556 - 925.7143) / (7500 - 4700) = 0.2249433
    q = returns.incremental_roic(industrial, 5)
    assert q.value == pytest.approx(0.22494331065759637, abs=APPROX)


def test_incremental_roic_refuses_on_shrinking_capital_base(industrial):
    """The guard that stops a spectacular meaningless number."""
    # Make invested capital fall over the window instead of rise.
    industrial.annual[0].set("total_debt", industrial.annual[5].get("total_debt"))
    industrial.annual[0].set("total_equity", industrial.annual[5].get("total_equity"))
    industrial.annual[0].set("cash_and_equivalents",
                             industrial.annual[5].get("cash_and_equivalents"))
    q = returns.incremental_roic(industrial, 5)
    assert q.value is None
    assert q.reason == UNDEFINED_MATH
    assert any("shrinking capital base" in m or "below the" in m
               for m in q.missing)


def test_reinvestment_and_compounding_identity(industrial):
    r = returns.reinvestment_rate(industrial, 0)
    i = returns.incremental_roic(industrial, 5)
    c = returns.intrinsic_compounding_rate(industrial, 5)
    assert r.value == pytest.approx(0.03214285714285714, abs=APPROX)
    assert c.value == pytest.approx(r.value * i.value, abs=APPROX)


# ---------------------------------------------------------------------------
# valuation
# ---------------------------------------------------------------------------


def test_enterprise_value_and_multiples(industrial):
    # EV = mcap 24000 + debt 3300 - cash 500 = 26800
    assert valuation.enterprise_value(industrial).value == pytest.approx(26800.0)
    assert valuation.acquirers_multiple(industrial).value == pytest.approx(13.4)
    assert valuation.ebit_ev_yield(industrial).value == pytest.approx(
        0.07462686567164178, abs=APPROX)


def test_reverse_dcf_round_trips(industrial, wacc):
    """Solve for g, then verify that g reproduces the observed EV."""
    exp = valuation.reverse_dcf(industrial, wacc, fade_years=10, terminal_growth=0.02)
    assert exp.implied_growth.ok
    g = exp.implied_growth.value
    ev = valuation.enterprise_value(industrial).value
    fcf = cashflow.free_cash_flow(industrial, 0).value
    assert valuation._pv_of_growth(fcf, g, 10, 0.09, 0.02) == pytest.approx(ev, rel=1e-5)


def test_reverse_dcf_states_a_falsifiable_claim(industrial, wacc):
    exp = valuation.reverse_dcf(industrial, wacc, fade_years=10, terminal_growth=0.02)
    assert "the market is pricing" in exp.claim
    assert "%" in exp.claim
    assert "10 years" in exp.claim


def test_reverse_dcf_null_without_wacc(industrial):
    no_wacc = Q.null(MISSING_INPUT, missing=("wacc",), label="wacc")
    exp = valuation.reverse_dcf(industrial, no_wacc, fade_years=10, terminal_growth=0.02)
    assert exp.implied_growth.value is None
    assert "insufficient data" in exp.claim


def test_reverse_dcf_refuses_when_wacc_below_terminal_growth(industrial):
    bad = Q(value=0.01, unit="ratio", as_of="2026-01-01",
            prov=valuation.free_cash_flow(industrial, 0).prov, label="wacc")
    exp = valuation.reverse_dcf(industrial, bad, fade_years=10, terminal_growth=0.02)
    assert exp.implied_growth.value is None
    assert "diverges" in exp.implied_growth.missing[0]


def test_reverse_dcf_refuses_on_negative_fcf(industrial, wacc):
    neg = Q(value=-500.0, unit="CAD", as_of="2025-12-31",
            prov=cashflow.free_cash_flow(industrial, 0).prov, label="fcf")
    exp = valuation.reverse_dcf(industrial, wacc, fade_years=10,
                                terminal_growth=0.02, base_fcf=neg)
    assert exp.implied_growth.value is None
    assert "negative free cash flow" in exp.implied_growth.missing[0]


def test_greenwald_triplet_reported_together(industrial, wacc):
    t = valuation.greenwald_triplet(industrial, wacc, years=5)
    assert set(t) == {"epv", "reproduction_value", "franchise_value"}
    # reproduction value = assets 9700 - goodwill 800 - intangibles 200 = 8700
    assert t["reproduction_value"].value == pytest.approx(8700.0)
    assert t["franchise_value"].value == pytest.approx(
        t["epv"].value - t["reproduction_value"].value, abs=1e-6)


def test_forward_dcf_is_tagged_as_a_scenario(industrial):
    s = valuation.Scenario("bull", growth=0.12, years=10, terminal_growth=0.02,
                           wacc=0.09)
    q = valuation.forward_dcf(industrial, s)
    assert q.prov.method == "normalized"
    assert "SCENARIO OUTPUT" in q.prov.note
    assert "g=12.00%" in q.prov.formula


# ---------------------------------------------------------------------------
# forensics
# ---------------------------------------------------------------------------


def test_piotroski_computes_all_nine_components(industrial):
    card = forensics.piotroski_f(industrial)
    assert card.possible == 9
    assert card.computed == 9
    assert card.total.ok
    assert 0 <= card.total.value <= 9


def test_piotroski_refuses_a_partial_total(industrial):
    """A 6-of-9 reported as '6' is indistinguishable from a real 6."""
    for p in industrial.annual:
        p.drop("cash_from_operations")
    card = forensics.piotroski_f(industrial)
    assert card.total.value is None
    assert card.computed < 9
    assert any("components computed" in m for m in card.total.missing)
    assert any("cash_from_operations" in m for m in card.total.missing)


def test_beneish_uses_published_coefficients(industrial):
    card = forensics.beneish_m(industrial)
    assert card.computed == 8
    assert card.total.ok
    assert "-4.84" in card.total.prov.formula
    assert "Beneish" in card.citation


def test_altman_z_for_manufacturer(industrial):
    # 1.2*(1200/9700) + 1.4*(2500/9700) + 3.3*(2000/9700) + 0.6*(24000/5000)
    #   + 1.0*(10000/9700) = 5.100618556701031
    card = forensics.altman_z(industrial)
    assert card.name == "altman_z"
    assert card.total.value == pytest.approx(5.100618556701031, abs=1e-9)
    assert forensics.altman_zone(card) == "safe"


def test_altman_variant_switches_for_asset_light(industrial):
    industrial.sector = "Information Technology"
    card = forensics.altman_z(industrial)
    assert card.name == "altman_z_double_prime"
    assert "x5_sales_to_assets" not in card.components
    assert "x4_book_equity_to_liabilities" in card.components


def test_sloan_accruals(industrial):
    # (NI 1350 - CFO 2200 - CFI -1100) / avg assets ((9700+9000)/2 = 9350)
    expected = (1350 - 2200 - (-1100)) / 9350
    assert forensics.sloan_accruals(industrial).value == pytest.approx(expected, abs=APPROX)


def test_montier_c_score(industrial):
    card = forensics.montier_c(industrial)
    assert card.possible == 6 and card.computed == 6
    assert card.total.ok


def test_share_count_trend_detects_dilution(industrial):
    # shares rise going back in time in the fixture => count is FALLING now
    q = forensics.share_count_trend(industrial, 5)
    assert q.ok and q.value < 0


def test_per_share_growth_is_dilution_adjusted(industrial):
    agg = industrial.fy("revenue", 0).value / industrial.fy("revenue", 5).value - 1
    ps = forensics.per_share_growth(industrial, "revenue", 5)
    assert ps.ok
    # aggregate revenue grew; per-share growth differs because share count moved
    assert ps.value != pytest.approx(agg)


# ---------------------------------------------------------------------------
# capital allocation
# ---------------------------------------------------------------------------


def test_dividend_coverage_uses_fcf_not_eps(industrial):
    # FCF 1200 / dividends 400 = 3.0
    assert capital.dividend_coverage_by_fcf(industrial, 0).value == pytest.approx(3.0)


def test_average_repurchase_price_null_when_share_count_rose(industrial):
    """Buying back less than you issue has no meaningful repurchase price."""
    # fixture share count FALLS over time going forward, so flip it
    industrial.annual[1].set("diluted_shares",
                             industrial.annual[0].get("diluted_shares"))
    q = capital.average_repurchase_price(industrial, 0)
    assert q.value is None


def test_repurchase_multiple_is_reported_with_the_buyback(industrial):
    out = capital.repurchase_multiple_paid(industrial, 0)
    assert "average_repurchase_price" in out
    assert "repurchase_multiple_of_owner_earnings_low" in out
    assert "repurchase_multiple_of_owner_earnings_high" in out


def test_debt_ladder_null_without_a_schedule(industrial, wacc):
    ladder = capital.debt_maturity_ladder(industrial, wacc)
    assert ladder.weighted_average_coupon.value is None


# ---------------------------------------------------------------------------
# banks
# ---------------------------------------------------------------------------


def test_rotce_uses_average_tangible_equity(bank):
    # TCE_t0 = 95000 - 11000 - 3000 = 81000; TCE_t1 = 90000 - 14000 = 76000
    # avg = 78500; NI to common = 16000 - 300 = 15700 => 0.2
    q = banks.rotce(bank, 0)
    assert q.value == pytest.approx(15700 / 78500, abs=APPROX)


def test_efficiency_ratio(bank):
    # non-interest expense 15400 / total revenue 28000 = 0.55
    assert banks.efficiency_ratio(bank, 0).value == pytest.approx(0.55)


def test_pcl_split_separates_performing_from_impaired(bank):
    s = banks.pcl_split(bank, 0)
    assert s["pcl_performing"].value == 600
    assert s["pcl_impaired"].value == 1400
    assert s["pcl_performing_to_loans"].ok and s["pcl_impaired_to_loans"].ok


def test_cet1_prefers_the_reported_figure(bank):
    assert banks.cet1_ratio(bank, 0).value == pytest.approx(0.132)


def test_bank_metrics_contain_no_industrial_metrics(bank):
    from ledger.model import FORBIDDEN_FOR_FINANCIALS
    m = banks.bank_metrics(bank)
    assert not set(m) & set(FORBIDDEN_FOR_FINANCIALS)


def test_ptbv_rotce_regression_and_residuals():
    from tests.conftest import make_bank
    # Five banks on a perfect line: P/TBV = 0.5 + 10 * ROTCE, plus one outlier.
    peers = [make_bank(f"B{i}.TO", r, 0.5 + 10 * r)
             for i, r in enumerate([0.10, 0.12, 0.14, 0.16, 0.18])]
    outlier = make_bank("RICH.TO", 0.12, 0.5 + 10 * 0.12 + 0.40)
    reg = banks.ptbv_rotce_regression(peers + [outlier])
    assert reg.n == 6
    assert reg.slope.ok and reg.intercept.ok
    assert reg.residual["RICH.TO"].value > 0.2, "outlier must show a positive residual"
    assert abs(reg.residual["B0.TO"].value) < 0.2


def test_regression_excludes_rather_than_imputes_missing_peers():
    from tests.conftest import make_bank
    peers = [make_bank(f"B{i}.TO", r, 0.5 + 10 * r)
             for i, r in enumerate([0.10, 0.12, 0.14, 0.16])]
    broken = make_bank("GAP.TO", 0.12, 1.7)
    for p in broken.annual:
        p.drop("common_equity")
    reg = banks.ptbv_rotce_regression(peers + [broken])
    assert "GAP.TO" in reg.excluded
    assert reg.n == 4
    assert reg.residual["GAP.TO"].value is None


def test_regression_refuses_below_minimum_peers():
    from tests.conftest import make_bank
    reg = banks.ptbv_rotce_regression([make_bank("A.TO", 0.12, 1.7)], min_n=4)
    assert reg.slope.value is None
    assert "needs 4 peers" in reg.slope.missing[0]


# ---------------------------------------------------------------------------
# cyclical
# ---------------------------------------------------------------------------


def test_mid_cycle_bridge_uses_disclosed_sensitivity(oil_producer, realized_wti):
    from ledger.config import load_deck
    deck = load_deck()
    base = cyclical.ebitda(oil_producer, 0)     # EBIT 3000 + D&A 800 = 3800
    b = cyclical.mid_cycle_bridge(oil_producer, deck, base=base,
                                  realized={"wti": realized_wti},
                                  commodities=["wti"])
    # (deck 65 - realized 78) * 120 = -1560; mid-cycle = 3800 - 1560 = 2240
    assert base.value == pytest.approx(3800.0)
    assert b.mid_cycle.value == pytest.approx(2240.0)
    assert b.distortion.value == pytest.approx(1560.0)


def test_mid_cycle_is_null_without_a_disclosed_sensitivity(oil_producer, realized_wti):
    from ledger.config import load_deck
    oil_producer.sensitivities = {}
    b = cyclical.mid_cycle_bridge(oil_producer, load_deck(),
                                  base=cyclical.ebitda(oil_producer, 0),
                                  realized={"wti": realized_wti}, commodities=["wti"])
    assert b.mid_cycle.value is None
    assert "does not disclose" in " ".join(b.mid_cycle.missing)


def test_mid_cycle_is_null_when_deck_price_unset(oil_producer):
    from ledger.config import load_deck
    oil_producer.sensitivities["cash_flow_per_usd_copper_lb"] = \
        oil_producer.sensitivities["cash_flow_per_usd_wti"]
    b = cyclical.mid_cycle_bridge(oil_producer, load_deck(),
                                  base=cyclical.ebitda(oil_producer, 0),
                                  realized={}, commodities=["copper"])
    assert b.mid_cycle.value is None
    assert "copper" in b.unpriced


def test_reserve_life_index(oil_producer):
    # 1800 mmboe / 150 mmboe per year = 12 years
    assert cyclical.reserve_life_index(oil_producer).value == pytest.approx(12.0)


def test_corporate_breakeven_wti(oil_producer, realized_wti):
    # shortfall = |capex 1000| + |div 400| - CFO 2200 = -800
    # breakeven = 78 + (-800 / 120) = 71.333...
    q = cyclical.corporate_breakeven_wti(oil_producer, realized_wti)
    assert q.value == pytest.approx(78 - 800 / 120, abs=1e-9)


def test_dual_multiple_reports_both_sides(oil_producer, realized_wti):
    from ledger.config import load_deck
    ev = valuation.enterprise_value(oil_producer)
    base = cyclical.ebitda(oil_producer, 0)
    b = cyclical.mid_cycle_bridge(oil_producer, load_deck(), base=base,
                                  realized={"wti": realized_wti}, commodities=["wti"])
    d = cyclical.dual_multiple("ev_to_ebitda", ev, b.reported, b.mid_cycle)
    assert d["ev_to_ebitda_ttm"].ok and d["ev_to_ebitda_mid_cycle"].ok
    assert d["ev_to_ebitda_ttm"].value != d["ev_to_ebitda_mid_cycle"].value
