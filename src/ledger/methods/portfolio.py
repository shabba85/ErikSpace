"""Portfolio-level analysis.

The specific failure this module exists to catch: a concentrated TSX portfolio
that looks diversified by ticker and is in fact one macro bet.  Two banks, a
pipeline and a telco are four names and roughly one exposure to Canadian
household credit and the level of rates.  Position-level thinking cannot see
that; look-through can.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..config import CONFIG_DIR, ROOT, _load_yaml, settings as _settings
from ..model import Company
from ..provenance import (
    INSUFFICIENT_HISTORY, MISSING_INPUT, NOT_FETCHED, Q, add, combine, ratio, sub,
)


def factor_map() -> dict[str, Any]:
    return _load_yaml(CONFIG_DIR / "factors.yaml")


@dataclass
class Position:
    ticker: str
    weight: float               # portfolio weight, user-supplied
    company: Company


# ---------------------------------------------------------------------------
# look-through factor exposure
# ---------------------------------------------------------------------------


@dataclass
class FactorExposure:
    factor: str
    share_of_portfolio_earnings: Q
    by_ticker: dict[str, Q] = field(default_factory=dict)
    unmapped: dict[str, list[str]] = field(default_factory=dict)
    excluded: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor": self.factor,
            "share_of_portfolio_earnings": self.share_of_portfolio_earnings.to_dict(),
            "by_ticker": {k: v.to_dict() for k, v in sorted(self.by_ticker.items())},
            "unmapped_segments": self.unmapped,
            "excluded_positions": sorted(self.excluded),
        }


def company_factor_earnings(co: Company, factor: str, mapping: dict[str, Any]) -> Q:
    """Sum of the company's disclosed segment earnings that load on `factor`.

    Segment earnings must be *disclosed and fetched*.  A company that does not
    break out segments yields a null naming that, not an assumption that the
    whole business loads on the factor.
    """
    seg_map = (mapping.get("mappings") or {}).get(co.ticker)
    if not seg_map:
        return Q.null(MISSING_INPUT,
                      missing=(f"{co.ticker} has no segment->factor mapping in "
                               "config/factors.yaml",),
                      label=f"{factor}_earnings")
    hits = {name: co.segments.get(name) for name, factors in seg_map.items()
            if factor in (factors or [])}
    if not hits:
        return Q(value=0.0, unit="", as_of=None, prov=None,
                 label=f"{factor}_earnings") if False else Q.null(
            MISSING_INPUT,
            missing=(f"{co.ticker} has no segment mapped to {factor}",),
            label=f"{factor}_earnings")
    terms: dict[str, Q] = {}
    for name, q in hits.items():
        terms[_key(name)] = q if q is not None else Q.null(
            NOT_FETCHED, missing=(f"{co.ticker} segment earnings: {name}",),
            label=name)
    return add(f"{factor}_earnings",
               next((q.unit for q in terms.values() if q.ok), ""), **terms)


def _key(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s).strip("_").lower() or "seg"


def look_through_exposure(positions: Sequence[Position], factor: str,
                          mapping: dict[str, Any] | None = None) -> FactorExposure:
    """Fraction of portfolio earnings that depend on one macro factor.

        share = SUM_i weight_i * factor_earnings_i / SUM_i weight_i * total_earnings_i

    Positions whose factor earnings cannot be computed are EXCLUDED from both
    numerator and denominator and listed by name.  Treating an unknown exposure
    as zero would understate concentration, which is the one direction of error
    that matters here.
    """
    m = mapping or factor_map()
    num_terms: dict[str, Q] = {}
    den_terms: dict[str, Q] = {}
    by_ticker: dict[str, Q] = {}
    excluded: list[str] = []
    unmapped: dict[str, list[str]] = {}

    for p in positions:
        co = p.company
        fe = company_factor_earnings(co, factor, m)
        total = co.fy("net_income", 0)
        seg_map = (m.get("mappings") or {}).get(co.ticker, {})
        missing_segs = [name for name in seg_map if name not in co.segments]
        if missing_segs:
            unmapped[co.ticker] = sorted(missing_segs)

        if not (fe.ok and total.ok):
            excluded.append(co.ticker)
            by_ticker[co.ticker] = Q.null(
                MISSING_INPUT,
                missing=tuple(fe.missing) + tuple(total.missing) or ("factor earnings",),
                unit="ratio", label=f"{factor}_share")
            continue
        by_ticker[co.ticker] = ratio(f"{factor}_share", "ratio", fe, total)
        k = _key(co.ticker)
        num_terms[k] = _weight(fe, p.weight, f"{k}_num")
        den_terms[k] = _weight(total, p.weight, f"{k}_den")

    if not num_terms:
        return FactorExposure(
            factor=factor,
            share_of_portfolio_earnings=Q.null(
                MISSING_INPUT,
                missing=(f"no position has computable {factor} exposure",),
                unit="ratio", label=f"{factor}_portfolio_share"),
            by_ticker=by_ticker, unmapped=unmapped, excluded=excluded)

    num = add(f"{factor}_weighted_earnings", "", **num_terms)
    den = add("portfolio_weighted_earnings", "", **den_terms)
    share = ratio(f"{factor}_portfolio_share", "ratio", num, den,
                  formula="sum(weight * factor earnings) / sum(weight * total earnings)")
    return FactorExposure(factor=factor, share_of_portfolio_earnings=share,
                          by_ticker=by_ticker, unmapped=unmapped, excluded=excluded)


def _weight(q: Q, w: float, label: str) -> Q:
    return combine(label, f"value * portfolio weight {w}", q.unit,
                   lambda v: v * w, {"v": q})


def concentration_report(positions: Sequence[Position],
                         factors: Iterable[str] | None = None) -> dict[str, Any]:
    """Every configured factor at once.

    The output to look at is not any single number but the shape: if three
    factors each read 60%+, the portfolio is one bet wearing several tickers.
    """
    m = factor_map()
    names = list(factors or (m.get("factors") or {}).keys())
    return {
        "positions": [{"ticker": p.ticker, "weight": p.weight} for p in positions],
        "factors": {f: look_through_exposure(positions, f, m).to_dict() for f in names},
        "note": ("Shares are of portfolio *earnings*, not market value, and do "
                 "not sum to 1: a single segment can load on several factors, "
                 "which is precisely the concentration this is built to expose."),
    }


# ---------------------------------------------------------------------------
# position sizing
# ---------------------------------------------------------------------------


@dataclass
class KellyScenario:
    """A user-supplied outcome.  Probabilities are the user's judgement; the
    system never estimates them."""

    name: str
    probability: float
    return_pct: float           # total return over the horizon, e.g. 0.60 or -0.35


def kelly_size(scenarios: Sequence[KellyScenario], *, fraction: float,
               max_weight: float, tol: float = 1e-9,
               ruin_guard: float | None = None) -> dict[str, Any]:
    """Fractional Kelly over user-supplied scenario probabilities.

    Solves for the f maximizing expected log wealth:

        max_f  SUM_i p_i * ln(1 + f * r_i)

    then scales by `fraction` and caps at `max_weight`.  Full Kelly is far too
    aggressive for a concentrated equity book with estimated probabilities,
    which is why the fraction is mandatory rather than optional.
    """
    probs = [s.probability for s in scenarios]
    if not scenarios:
        return {"error": "no scenarios supplied", "kelly_fraction": None}
    if abs(sum(probs) - 1.0) > 1e-6:
        return {"error": f"probabilities sum to {sum(probs):.4f}, not 1.0",
                "kelly_fraction": None}
    if any(p < 0 for p in probs):
        return {"error": "negative probability", "kelly_fraction": None}

    worst = min(s.return_pct for s in scenarios)
    ev = sum(s.probability * s.return_pct for s in scenarios)
    if ev <= 0:
        return {"expected_return": ev, "kelly_fraction": 0.0,
                "fractional_kelly": 0.0, "recommended_weight": 0.0,
                "note": "expected return is not positive; Kelly size is zero"}

    # f is bounded by the point at which the worst case wipes out the position.
    # The guard keeps the search strictly inside ruin, where log wealth diverges.
    if ruin_guard is None:
        ruin_guard = float(_settings()["portfolio"]["kelly_ruin_guard"])
    hi = ruin_guard / abs(worst) if worst < 0 else 100.0

    def dlog(f: float) -> float:
        return sum(s.probability * s.return_pct / (1.0 + f * s.return_pct)
                   for s in scenarios)

    lo = 0.0
    if dlog(lo) <= 0:
        f_star = 0.0
    elif dlog(hi) > 0:
        f_star = hi
    else:
        for _ in range(200):
            mid = (lo + hi) / 2.0
            if dlog(mid) > 0:
                lo = mid
            else:
                hi = mid
            if hi - lo < tol:
                break
        f_star = (lo + hi) / 2.0

    frac = f_star * fraction
    return {
        "expected_return": ev,
        "worst_case_return": worst,
        "kelly_fraction": f_star,
        "kelly_multiplier": fraction,
        "fractional_kelly": frac,
        "recommended_weight": min(frac, max_weight),
        "capped_by_max_weight": frac > max_weight,
        "scenarios": [{"name": s.name, "probability": s.probability,
                       "return_pct": s.return_pct} for s in scenarios],
        "note": ("Probabilities are user judgement, not model output. Kelly is "
                 "exquisitely sensitive to them: halve the win probability and "
                 "the size more than halves."),
    }


# ---------------------------------------------------------------------------
# base rates
# ---------------------------------------------------------------------------

BASE_RATE_PATH = ROOT / "data" / "base_rates.csv"
BASE_RATE_SCHEMA = ("sector", "size_bucket", "metric", "growth_bucket_low",
                    "growth_bucket_high", "horizon_years", "frequency",
                    "n_firms", "source_name", "source_url", "as_of")


def load_base_rates(path: Path | None = None) -> list[dict[str, str]]:
    """Load the base-rate table.

    The system ships NO base-rate data.  These are empirical frequencies from a
    specific study over a specific period, and inventing them would be exactly
    the failure mode this whole design forbids.  Supply a CSV with the columns
    in BASE_RATE_SCHEMA (the Mauboussin/Callahan base-rate book is the obvious
    source) and every row carries its own citation into the audit log.
    """
    p = path or BASE_RATE_PATH
    if not p.exists():
        return []
    with p.open() as fh:
        rows = list(csv.DictReader(fh))
    missing = [c for c in BASE_RATE_SCHEMA if rows and c not in rows[0]]
    if missing:
        raise ValueError(f"base rate table missing columns: {missing}")
    return rows


def base_rate_for(sector: str, size_bucket: str, growth: float,
                  horizon_years: int, *, metric: str = "revenue_growth",
                  path: Path | None = None) -> Q:
    """Historical frequency of firms of this size and sector sustaining `growth`.

    Returns null -- loudly -- when no table is loaded.  A growth assumption
    above 10% with no base rate behind it is exactly the assumption that should
    make you uncomfortable, and the system says so rather than staying quiet.
    """
    rows = load_base_rates(path)
    if not rows:
        return Q.null(
            NOT_FETCHED,
            missing=("base rate table not loaded (data/base_rates.csv); the "
                     "system ships no base-rate data because inventing empirical "
                     "frequencies is the error it exists to prevent",),
            unit="ratio", label="base_rate")
    for r in rows:
        if (r["sector"] == sector and r["size_bucket"] == size_bucket
                and r["metric"] == metric
                and int(r["horizon_years"]) == horizon_years
                and float(r["growth_bucket_low"]) <= growth < float(r["growth_bucket_high"])):
            return Q.reported(
                float(r["frequency"]), unit="ratio", as_of=r["as_of"],
                source_name=r["source_name"], source_url=r["source_url"],
                retrieved_at=f"{r['as_of']}T00:00:00+00:00",
                label="base_rate",
                note=(f"{r['n_firms']} firms; {sector}/{size_bucket}; "
                      f"{float(r['growth_bucket_low']):.0%}-"
                      f"{float(r['growth_bucket_high']):.0%} {metric} over "
                      f"{horizon_years}y"))
    return Q.null(
        MISSING_INPUT,
        missing=(f"no base-rate row for {sector}/{size_bucket}/{metric} at "
                 f"{growth:.1%} over {horizon_years}y",),
        unit="ratio", label="base_rate")


def base_rate_check(implied_growth: Q, sector: str, size_bucket: str,
                    horizon_years: int, *, threshold: float | None = None,
                    path: Path | None = None) -> dict[str, Any]:
    """Attach a base rate to any growth assumption above the threshold.

    Below the threshold no base rate is required: 6% growth is unremarkable.
    Above it, the market is claiming something historically uncommon and the
    frequency of that claim coming true is the relevant context.
    """
    if threshold is None:
        from .forensics import models
        threshold = float(models()["base_rates"]["growth_threshold"])
    if not implied_growth.ok:
        return {"applicable": False,
                "reason": "implied growth could not be computed",
                "missing": list(implied_growth.missing)}
    if implied_growth.value <= threshold:
        return {"applicable": False,
                "reason": (f"implied growth {implied_growth.value:.1%} is at or "
                           f"below the {threshold:.0%} base-rate threshold")}
    br = base_rate_for(sector, size_bucket, implied_growth.value, horizon_years,
                       path=path)
    return {
        "applicable": True,
        "implied_growth": implied_growth.to_dict(),
        "base_rate": br.to_dict(),
        "claim": (f"The price implies {implied_growth.value:.1%} growth sustained "
                  f"for {horizon_years} years."
                  + (f" Historically {br.value:.1%} of {sector} firms in the "
                     f"{size_bucket} bucket achieved that."
                     if br.ok else " No base rate is loaded for that claim.")),
    }
