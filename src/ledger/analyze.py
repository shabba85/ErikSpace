"""Metric assembly.

Routes a company to the right metric set for its business model and returns a
flat dict of Q objects plus the structured extras (ranges, scorecards, bridges).

The routing is the point: a bank never enters the industrial branch, so the
forbidden metrics are not computed-then-hidden, they are never computed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Deck, settings
from .guard import assert_no_fabricated_numbers, assert_no_financial_bleed
from .methods import banks, capital, cashflow, cyclical, forensics, insurers, returns, valuation
from .model import Company
from .provenance import MISSING_INPUT, NOT_FETCHED, Q
from .reconcile import Reconciliation, blocked_metric


@dataclass
class Analysis:
    ticker: str
    schema: str
    metrics: dict[str, Q] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    blocked: bool = False
    blocked_reason: str | None = None
    as_of: str | None = None

    def coverage(self) -> dict[str, Any]:
        pop = sum(1 for q in self.metrics.values() if q.ok)
        na = sum(1 for q in self.metrics.values() if q.is_na)
        return {"populated": pop, "null": len(self.metrics) - pop,
                "not_applicable": na, "total": len(self.metrics),
                "populated_pct": (pop / len(self.metrics) if self.metrics else 0.0)}

    def missing_report(self) -> list[dict[str, Any]]:
        """Every null, with the field that caused it.  This is what the UI
        renders as 'insufficient data' and why."""
        out = []
        for name, q in sorted(self.metrics.items()):
            if not q.ok:
                out.append({"metric": name, "reason": q.reason,
                            "missing": list(q.missing)})
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker, "schema": self.schema, "as_of": self.as_of,
            "blocked": self.blocked, "blocked_reason": self.blocked_reason,
            "coverage": self.coverage(),
            "metrics": {k: v.to_dict() for k, v in sorted(self.metrics.items())},
            "extras": _serialize(self.extras),
            "missing": self.missing_report(),
            "audit_command": f"ledger audit {self.ticker}",
        }


def _serialize(obj: Any) -> Any:
    if isinstance(obj, Q):
        return obj.to_dict()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in sorted(obj.items(), key=lambda t: str(t[0]))}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    return obj


def analyze(co: Company, *, wacc: Q, deck: Deck | None = None,
            reconciliation: Reconciliation | None = None,
            realized_prices: dict[str, Q] | None = None,
            commodities: list[str] | None = None,
            refi_rate: Q | None = None, today=None) -> Analysis:
    """Compute everything admissible for this company."""
    a = Analysis(ticker=co.ticker, schema=co.schema)
    p = co.latest_annual()
    a.as_of = p.period_end if p else None

    if reconciliation is not None and reconciliation.blocked:
        a.blocked = True
        a.blocked_reason = reconciliation.blocking_message()
        a.extras["reconciliation"] = reconciliation.to_dict()

    if co.schema == "bank":
        a.metrics.update(banks.bank_metrics(co))
        a.extras["altman"] = forensics.altman_z(co).to_dict()
    elif co.schema == "insurer":
        a.metrics.update(insurers.insurer_metrics(co))
        a.extras["altman"] = forensics.altman_z(co).to_dict()
    else:
        _industrial(a, co, wacc=wacc, deck=deck, realized_prices=realized_prices or {},
                    commodities=commodities, refi_rate=refi_rate, today=today)

    assert_no_financial_bleed(co.schema, a.metrics)
    assert_no_fabricated_numbers(a.metrics)

    if a.blocked:
        # Every score becomes the same null: "two sources disagree". The
        # underlying figures stay visible in the audit trail so the user can see
        # exactly what disagreed -- but nothing is scoreable.
        a.metrics = {k: blocked_metric(reconciliation, k) for k in a.metrics}
    return a


def _industrial(a: Analysis, co: Company, *, wacc: Q, deck: Deck | None,
                realized_prices: dict[str, Q], commodities: list[str] | None,
                refi_rate: Q | None, today) -> None:
    m = a.metrics
    cfg = settings()["valuation"]

    m["free_cash_flow"] = cashflow.free_cash_flow(co, 0)
    m["cash_tax_rate"] = cashflow.cash_tax_rate(co, 0)
    m["nopat"] = returns.nopat(co, 0)
    m["invested_capital"] = returns.invested_capital(co, 0)
    m["roic"] = returns.roic(co, 0)
    m["incremental_roic"] = returns.incremental_roic(co, 5)
    m["reinvestment_rate"] = returns.reinvestment_rate(co, 0)
    m["intrinsic_compounding_rate"] = returns.intrinsic_compounding_rate(co, 5)

    oe = cashflow.owner_earnings(co, 0)
    mc = cashflow.maintenance_capex(co, 0)
    a.extras["owner_earnings"] = oe.to_dict()
    a.extras["maintenance_capex"] = mc.to_dict()
    m["owner_earnings_low"] = oe.low
    m["owner_earnings_high"] = oe.high

    conv = cashflow.fcf_conversion_stats(co, 5)
    a.extras["fcf_conversion"] = {
        k: _serialize(v) for k, v in conv.items()}
    m["fcf_conversion_mean"] = conv["fcf_conversion_mean"]
    m["fcf_conversion_stdev"] = conv["fcf_conversion_stdev"]

    m["market_cap"] = valuation.market_cap(co)
    m["enterprise_value"] = valuation.enterprise_value(co)
    m["acquirers_multiple"] = valuation.acquirers_multiple(co)
    m["ebit_ev_yield"] = valuation.ebit_ev_yield(co)

    trip = valuation.greenwald_triplet(co, wacc, years=int(cfg["epv"]["normalization_years"]))
    m.update(trip)

    implied: dict[str, Any] = {}
    for fy in cfg["reverse_dcf"]["fade_years"]:
        exp = valuation.reverse_dcf(
            co, wacc, fade_years=int(fy),
            terminal_growth=float(cfg["reverse_dcf"]["terminal_growth"]),
            bounds=tuple(cfg["reverse_dcf"]["solver_bounds"]),
            tol=float(cfg["reverse_dcf"]["solver_tol"]))
        implied[f"fade_{fy}y"] = exp.to_dict()
        m[f"reverse_dcf_implied_growth_{fy}y"] = exp.implied_growth
    a.extras["reverse_dcf"] = implied
    m["expectations_gap"] = valuation.expectations_gap(
        m.get("reverse_dcf_implied_growth_10y",
              Q.null(MISSING_INPUT, missing=("10y reverse DCF",))),
        m["intrinsic_compounding_rate"])

    # forensics
    for card in (forensics.piotroski_f(co), forensics.beneish_m(co),
                 forensics.montier_c(co), forensics.altman_z(co)):
        a.extras[card.name] = card.to_dict()
        m[card.name] = card.total
    m["sloan_accruals_ratio"] = forensics.sloan_accruals(co)
    m["share_count_cagr_5y"] = forensics.share_count_trend(co, 5)
    m["revenue_per_share_cagr_5y"] = forensics.per_share_growth(co, "revenue", 5)
    a.extras["ncwc_to_revenue_trend"] = [
        q.to_dict() for q in forensics.ncwc_to_revenue_trend(co, 5)]

    # capital allocation
    epv_ps = None
    if m["epv"].ok:
        from .provenance import ratio as _ratio
        epv_ps = _ratio("epv_per_share", "", m["epv"], co.fy("diluted_shares", 0))
        m["epv_per_share"] = epv_ps
    cap = capital.capital_allocation_scorecard(co, epv_per_share=epv_ps,
                                               current_refi_rate=refi_rate)
    for k, v in cap.items():
        if isinstance(v, Q):
            m[k] = v
        else:
            a.extras[k] = _serialize(v)

    # cyclical
    if cyclical.is_cyclical(co):
        if deck is None:
            a.extras["mid_cycle"] = {
                "error": "no deck supplied; mid-cycle metrics unavailable"}
        else:
            ebitda = cyclical.ebitda(co, 0)
            comms = commodities or _infer_commodities(co)
            bridge = cyclical.mid_cycle_bridge(
                co, deck, base=ebitda, realized=realized_prices,
                commodities=comms, today=today)
            a.extras["mid_cycle"] = bridge.to_dict()
            m["ebitda_ttm"] = bridge.reported
            m["ebitda_mid_cycle"] = bridge.mid_cycle
            m["cyclical_distortion"] = bridge.distortion
            m.update(cyclical.dual_multiple("ev_to_ebitda", m["enterprise_value"],
                                            bridge.reported, bridge.mid_cycle))
            m["net_debt_to_mid_cycle_ebitda"] = cyclical.net_debt_to_mid_cycle_ebitda(
                co, bridge.mid_cycle)
            m["reserve_life_index"] = cyclical.reserve_life_index(co)
            m["corporate_breakeven_wti"] = cyclical.corporate_breakeven_wti(
                co, realized_prices.get(
                    "wti", Q.null(NOT_FETCHED,
                                  missing=("realized WTI for the period",),
                                  unit="USD/bbl", label="realized_wti")))
    else:
        a.extras["mid_cycle"] = {
            "applicable": False,
            "reason": f"{co.sector or 'sector unknown'} is not a cyclical sector; "
                      "TTM figures are used directly"}


def _infer_commodities(co: Company) -> list[str]:
    """Which deck commodities this company actually discloses a sensitivity to.

    Driven by the company's own MD&A disclosures rather than by its sector
    label: an energy company that discloses only a WTI sensitivity is bridged on
    WTI alone, not on a guessed basket.
    """
    keys = {v: k for k, v in cyclical.SENSITIVITY_KEYS.items()}
    return sorted(keys[k] for k in co.sensitivities if k in keys)
