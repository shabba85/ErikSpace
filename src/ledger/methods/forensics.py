"""Forensic and earnings-quality screens.

Every component is computed field-by-field from statements.  Where a component
cannot be computed the composite refuses to produce a total: a 6-of-9 Piotroski
reported as "6" is indistinguishable from a genuine 6 and is therefore a lie.
We return the components, the count computed, and a null total.

All model coefficients come from config/models.yaml with their citations.
Altman is suppressed entirely for financials -- not zeroed, absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import CONFIG_DIR, _load_yaml
from ..model import Company
from ..provenance import (
    INSUFFICIENT_HISTORY, MISSING_INPUT, NOT_APPLICABLE, Q, combine, normalized,
    ratio, sub,
)

_MODELS = None


def models() -> dict[str, Any]:
    global _MODELS
    if _MODELS is None:
        _MODELS = _load_yaml(CONFIG_DIR / "models.yaml")
    return _MODELS


@dataclass
class ScoreCard:
    """A composite plus the full input vector that produced it."""

    name: str
    total: Q
    components: dict[str, Q] = field(default_factory=dict)
    computed: int = 0
    possible: int = 0
    citation: str = ""
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total": self.total.to_dict(),
            "components": {k: v.to_dict() for k, v in sorted(self.components.items())},
            "computed": self.computed,
            "possible": self.possible,
            "citation": self.citation,
            "missing": sorted(set(self.missing)),
        }


def _flag(label: str, cond_inputs: dict[str, Q], test) -> Q:
    """A 0/1 Piotroski-style flag that is null when any input is null."""
    return combine(label, f"1 if {label} else 0", "flag",
                   lambda **kw: 1.0 if test(**kw) else 0.0, cond_inputs)


# ---------------------------------------------------------------------------
# Piotroski F-score
# ---------------------------------------------------------------------------


def piotroski_f(co: Company) -> ScoreCard:
    m = models()["piotroski_f"]
    c: dict[str, Q] = {}

    roa0 = ratio("roa_t0", "ratio", co.fy("net_income", 0), co.fy("total_assets", 0))
    roa1 = ratio("roa_t1", "ratio", co.fy("net_income", 1), co.fy("total_assets", 1))
    cfo0 = co.fy("cash_from_operations", 0)
    cfo_ta = ratio("cfo_to_assets", "ratio", cfo0, co.fy("total_assets", 0))

    c["f1_roa_positive"] = _flag("f1_roa_positive", {"roa": roa0}, lambda roa: roa > 0)
    c["f2_cfo_positive"] = _flag("f2_cfo_positive", {"cfo": cfo0}, lambda cfo: cfo > 0)
    c["f3_roa_improving"] = _flag("f3_roa_improving", {"a": roa0, "b": roa1},
                                  lambda a, b: a > b)
    c["f4_accruals_clean"] = _flag("f4_accruals_clean", {"cfo": cfo_ta, "roa": roa0},
                                   lambda cfo, roa: cfo > roa)

    lev0 = ratio("ltd_to_assets_t0", "ratio", co.fy("long_term_debt", 0),
                 co.fy("total_assets", 0))
    lev1 = ratio("ltd_to_assets_t1", "ratio", co.fy("long_term_debt", 1),
                 co.fy("total_assets", 1))
    c["f5_leverage_falling"] = _flag("f5_leverage_falling", {"a": lev0, "b": lev1},
                                     lambda a, b: a < b)

    cr0 = ratio("current_ratio_t0", "ratio", co.fy("total_current_assets", 0),
                co.fy("total_current_liabilities", 0))
    cr1 = ratio("current_ratio_t1", "ratio", co.fy("total_current_assets", 1),
                co.fy("total_current_liabilities", 1))
    c["f6_liquidity_improving"] = _flag("f6_liquidity_improving", {"a": cr0, "b": cr1},
                                        lambda a, b: a > b)

    c["f7_no_dilution"] = _flag("f7_no_dilution",
                                {"a": co.fy("diluted_shares", 0),
                                 "b": co.fy("diluted_shares", 1)},
                                lambda a, b: a <= b)

    gm0 = ratio("gross_margin_t0", "ratio", co.fy("gross_profit", 0), co.fy("revenue", 0))
    gm1 = ratio("gross_margin_t1", "ratio", co.fy("gross_profit", 1), co.fy("revenue", 1))
    c["f8_margin_improving"] = _flag("f8_margin_improving", {"a": gm0, "b": gm1},
                                     lambda a, b: a > b)

    at0 = ratio("asset_turnover_t0", "ratio", co.fy("revenue", 0), co.fy("total_assets", 0))
    at1 = ratio("asset_turnover_t1", "ratio", co.fy("revenue", 1), co.fy("total_assets", 1))
    c["f9_turnover_improving"] = _flag("f9_turnover_improving", {"a": at0, "b": at1},
                                       lambda a, b: a > b)

    return _finalize("piotroski_f", c, int(m["max_score"]), m["citation"],
                     lambda **kw: sum(kw.values()), "sum of 9 binary tests", "score")


def _finalize(name: str, comps: dict[str, Q], possible: int, citation: str,
              fn, formula: str, unit: str) -> ScoreCard:
    missing: list[str] = []
    for k, q in comps.items():
        if not q.ok:
            missing.extend(q.missing or (k,))
    computed = sum(1 for q in comps.values() if q.ok)
    if missing:
        total = Q.null(
            MISSING_INPUT,
            missing=[f"{name}: {computed}/{possible} components computed"] + missing,
            unit=unit, label=name,
        )
    else:
        total = combine(name, formula, unit, fn, comps)
    return ScoreCard(name=name, total=total, components=comps, computed=computed,
                     possible=possible, citation=citation, missing=missing)


# ---------------------------------------------------------------------------
# Beneish M-score
# ---------------------------------------------------------------------------


def beneish_m(co: Company) -> ScoreCard:
    m = models()["beneish_m"]
    k = m["coefficients"]

    def idx(label: str, num0: Q, den0: Q, num1: Q, den1: Q, invert: bool = False) -> Q:
        a = ratio(f"{label}_t0", "ratio", num0, den0)
        b = ratio(f"{label}_t1", "ratio", num1, den1)
        hi, lo = (b, a) if invert else (a, b)
        return ratio(label, "index", hi, lo)

    c: dict[str, Q] = {}
    c["dsri"] = idx("dsri", co.fy("receivables", 0), co.fy("revenue", 0),
                    co.fy("receivables", 1), co.fy("revenue", 1))
    c["gmi"] = idx("gmi", co.fy("gross_profit", 0), co.fy("revenue", 0),
                   co.fy("gross_profit", 1), co.fy("revenue", 1), invert=True)

    def aqi_side(i: int) -> Q:
        soft = combine(f"soft_assets_{i}",
                       "1 - (total_current_assets + ppe_net) / total_assets", "ratio",
                       lambda ca, ppe, ta: None if ta == 0 else 1.0 - (ca + ppe) / ta,
                       {"ca": co.fy("total_current_assets", i),
                        "ppe": co.fy("ppe_net", i), "ta": co.fy("total_assets", i)})
        return soft

    c["aqi"] = ratio("aqi", "index", aqi_side(0), aqi_side(1))
    c["sgi"] = ratio("sgi", "index", co.fy("revenue", 0), co.fy("revenue", 1))

    def dep_rate(i: int) -> Q:
        return combine(f"dep_rate_{i}", "dep / (dep + ppe_net)", "ratio",
                       lambda d, p: None if (d + p) == 0 else d / (d + p),
                       {"d": co.fy("depreciation_amortization_cf", i),
                        "p": co.fy("ppe_net", i)})

    c["depi"] = ratio("depi", "index", dep_rate(1), dep_rate(0))
    c["sgai"] = idx("sgai", co.fy("sga_expense", 0), co.fy("revenue", 0),
                    co.fy("sga_expense", 1), co.fy("revenue", 1))
    c["tata"] = combine("tata", "(net_income - cash_from_operations) / total_assets",
                        "ratio",
                        lambda ni, cfo, ta: None if ta == 0 else (ni - cfo) / ta,
                        {"ni": co.fy("net_income", 0),
                         "cfo": co.fy("cash_from_operations", 0),
                         "ta": co.fy("total_assets", 0)})

    def lev(i: int) -> Q:
        return combine(f"leverage_{i}",
                       "(long_term_debt + total_current_liabilities) / total_assets",
                       "ratio",
                       lambda d, cl, ta: None if ta == 0 else (d + cl) / ta,
                       {"d": co.fy("long_term_debt", i),
                        "cl": co.fy("total_current_liabilities", i),
                        "ta": co.fy("total_assets", i)})

    c["lvgi"] = ratio("lvgi", "index", lev(0), lev(1))

    intercept = float(m["intercept"])

    def _score(**kw: float) -> float:
        return intercept + sum(float(k[n]) * kw[n] for n in k)

    formula = f"{intercept} + " + " + ".join(f"{v}*{n.upper()}" for n, v in k.items())
    return _finalize("beneish_m", c, 8, m["citation"], _score, formula, "score")


def beneish_flag(card: ScoreCard) -> Q:
    """Is the M-score above Beneish's manipulation threshold?"""
    thr = float(models()["beneish_m"]["threshold"])
    return combine("beneish_above_threshold", f"m_score > {thr}", "flag",
                   lambda m: 1.0 if m > thr else 0.0, {"m": card.total})


# ---------------------------------------------------------------------------
# Sloan accruals
# ---------------------------------------------------------------------------


def sloan_accruals(co: Company) -> Q:
    """(net income - CFO - CFI) / average total assets.

    The cash-flow-statement formulation.  High positive accruals mean reported
    profit is not showing up as cash, which Sloan showed predicts subsequent
    underperformance.
    """
    avg_ta = combine("average_total_assets", "(ta_t0 + ta_t1) / 2",
                     co.fy("total_assets", 0).unit,
                     lambda a, b: (a + b) / 2.0,
                     {"a": co.fy("total_assets", 0), "b": co.fy("total_assets", 1)})
    return combine(
        "sloan_accruals_ratio",
        "(net_income - cash_from_operations - cash_from_investing) / average_total_assets",
        "ratio",
        lambda ni, cfo, cfi, ta: None if ta == 0 else (ni - cfo - cfi) / ta,
        {"ni": co.fy("net_income", 0), "cfo": co.fy("cash_from_operations", 0),
         "cfi": co.fy("cash_from_investing", 0), "ta": avg_ta},
    )


# ---------------------------------------------------------------------------
# Montier C-score
# ---------------------------------------------------------------------------


def montier_c(co: Company) -> ScoreCard:
    m = models()["montier_c"]
    thr = float(m["asset_growth_threshold"])
    c: dict[str, Q] = {}

    def gap(i: int) -> Q:
        return sub(f"ni_less_cfo_{i}", co.fy("net_income", i).unit,
                   co.fy("net_income", i), co.fy("cash_from_operations", i))

    c["c1_ni_cfo_diverging"] = _flag("c1_ni_cfo_diverging", {"a": gap(0), "b": gap(1)},
                                     lambda a, b: a > b)

    def dso(i: int) -> Q:
        return ratio(f"dso_{i}", "ratio", co.fy("receivables", i), co.fy("revenue", i))

    c["c2_dso_rising"] = _flag("c2_dso_rising", {"a": dso(0), "b": dso(1)},
                               lambda a, b: a > b)

    def dsi(i: int) -> Q:
        return ratio(f"dsi_{i}", "ratio", co.fy("inventory", i),
                     co.fy("cost_of_revenue", i))

    c["c3_inventory_days_rising"] = _flag("c3_inventory_days_rising",
                                          {"a": dsi(0), "b": dsi(1)},
                                          lambda a, b: a > b)

    def oca(i: int) -> Q:
        return ratio(f"oca_to_rev_{i}", "ratio", co.fy("other_current_assets", i),
                     co.fy("revenue", i))

    c["c4_other_current_assets_rising"] = _flag("c4_other_current_assets_rising",
                                                {"a": oca(0), "b": oca(1)},
                                                lambda a, b: a > b)

    def dep_to_ppe(i: int) -> Q:
        return ratio(f"dep_to_gross_ppe_{i}", "ratio",
                     co.fy("depreciation_amortization_cf", i), co.fy("ppe_gross", i))

    c["c5_depreciation_falling"] = _flag("c5_depreciation_falling",
                                         {"a": dep_to_ppe(0), "b": dep_to_ppe(1)},
                                         lambda a, b: a < b)

    c["c6_asset_growth_high"] = _flag(
        "c6_asset_growth_high",
        {"a": co.fy("total_assets", 0), "b": co.fy("total_assets", 1)},
        lambda a, b: b != 0 and (a / b - 1.0) > thr,
    )

    return _finalize("montier_c", c, 6, m["citation"],
                     lambda **kw: sum(kw.values()), "sum of 6 binary flags", "score")


# ---------------------------------------------------------------------------
# Altman Z -- variant per sector, absent for financials
# ---------------------------------------------------------------------------


def altman_variant_for(sector: str) -> str:
    return models()["altman_variant_by_sector"].get(sector, "z_double_prime")


def altman_z(co: Company) -> ScoreCard:
    """Altman Z or Z'' depending on sector.  Financials get neither.

    For a bank the ratios that feed Z -- working capital, asset turnover, the
    very notion of current assets -- have no meaning.  Returning zero would be
    worse than returning nothing, so we return a not-applicable card whose total
    is a not_applicable null and whose components are empty.
    """
    if co.is_financial() or altman_variant_for(co.sector) == "none":
        return ScoreCard(
            name="altman_z",
            total=Q.not_applicable(
                "Altman Z is not applicable to a financial business model: "
                "working capital, current ratio and asset turnover are "
                "undefined for a bank or insurer balance sheet",
                label="altman_z"),
            components={}, computed=0, possible=0,
            citation=models()["altman_z"]["citation"],
            missing=[],
        )

    variant = altman_variant_for(co.sector)
    spec = models()["altman_z" if variant == "z" else "altman_z_double_prime"]
    k = spec["coefficients"]
    ta = co.fy("total_assets", 0)

    wc = sub("working_capital", ta.unit, co.fy("total_current_assets", 0),
             co.fy("total_current_liabilities", 0))
    c: dict[str, Q] = {
        "x1_working_capital_to_assets": ratio("x1", "ratio", wc, ta),
        "x2_retained_earnings_to_assets": ratio("x2", "ratio",
                                                co.fy("retained_earnings", 0), ta),
        "x3_ebit_to_assets": ratio("x3", "ratio", co.fy("operating_income", 0), ta),
    }
    if variant == "z":
        from .valuation import market_cap
        c["x4_market_equity_to_liabilities"] = ratio(
            "x4", "ratio", market_cap(co), co.fy("total_liabilities", 0))
        c["x5_sales_to_assets"] = ratio("x5", "ratio", co.fy("revenue", 0), ta)
        weights = [k["working_capital_to_assets"], k["retained_earnings_to_assets"],
                   k["ebit_to_assets"], k["market_equity_to_liabilities"],
                   k["sales_to_assets"]]
    else:
        c["x4_book_equity_to_liabilities"] = ratio(
            "x4", "ratio", co.fy("total_equity", 0), co.fy("total_liabilities", 0))
        weights = [k["working_capital_to_assets"], k["retained_earnings_to_assets"],
                   k["ebit_to_assets"], k["book_equity_to_liabilities"]]

    keys = list(c.keys())
    wmap = dict(zip(keys, [float(w) for w in weights]))

    def _score(**kw: float) -> float:
        return sum(wmap[n] * v for n, v in kw.items())

    formula = " + ".join(f"{wmap[n]}*{n}" for n in keys)
    card = _finalize(f"altman_{variant}", c, len(keys), spec["citation"], _score,
                     formula, "score")
    card.name = "altman_z" if variant == "z" else "altman_z_double_prime"
    return card


def altman_zone(card: ScoreCard) -> str | None:
    if not card.total.ok:
        return None
    spec = models()["altman_z" if card.name == "altman_z" else "altman_z_double_prime"]
    z = card.total.value
    if z < float(spec["zones"]["distress_below"]):
        return "distress"
    if z > float(spec["zones"]["safe_above"]):
        return "safe"
    return "grey"


# ---------------------------------------------------------------------------
# trend screens
# ---------------------------------------------------------------------------


def non_cash_working_capital(co: Company, i: int = 0) -> Q:
    """(current assets - cash) - (current liabilities - short-term debt)."""
    return combine(
        "non_cash_working_capital",
        "(total_current_assets - cash) - (total_current_liabilities - short_term_debt)",
        co.fy("total_current_assets", i).unit,
        lambda ca, cash, cl, std: (ca - cash) - (cl - std),
        {"ca": co.fy("total_current_assets", i),
         "cash": co.fy("cash_and_equivalents", i),
         "cl": co.fy("total_current_liabilities", i),
         "std": co.fy("short_term_debt", i)},
    )


def ncwc_to_revenue_trend(co: Company, years: int = 5) -> list[Q]:
    """NCWC as a share of revenue, newest first.  A rising trend means growth is
    being funded by the balance sheet rather than by customers."""
    return [ratio(f"ncwc_to_revenue_{i}", "ratio", non_cash_working_capital(co, i),
                  co.fy("revenue", i)) for i in range(years)]


def share_count_trend(co: Company, years: int = 5) -> Q:
    """CAGR of diluted share count.  Negative = buyback, positive = dilution."""
    s0, sN = co.fy("diluted_shares", 0), co.fy("diluted_shares", years)
    return combine(
        "share_count_cagr", f"(shares_t0 / shares_t-{years})^(1/{years}) - 1", "ratio",
        lambda a, b: None if (b <= 0 or a <= 0) else (a / b) ** (1.0 / years) - 1.0,
        {"a": s0, "b": sN},
    )


def per_share_growth(co: Company, field_name: str, years: int = 5) -> Q:
    """Dilution-adjusted per-share CAGR of any flow item.

    Growth in aggregate that was bought with share issuance is not growth the
    owner received.  This is the version that matters.
    """
    def ps(i: int) -> Q:
        return ratio(f"{field_name}_per_share_{i}", "ratio", co.fy(field_name, i),
                     co.fy("diluted_shares", i))

    return combine(
        f"{field_name}_per_share_cagr",
        f"(ps_t0 / ps_t-{years})^(1/{years}) - 1", "ratio",
        lambda a, b: None if (b <= 0 or a <= 0) else (a / b) ** (1.0 / years) - 1.0,
        {"a": ps(0), "b": ps(years)},
    )
