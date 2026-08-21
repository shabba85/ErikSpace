"""Ranking.

The composite is off by default and is never the headline.  A single number
that blends ROIC, leverage and valuation hides exactly the disagreement between
those inputs that makes a decision interesting.

What the system ranks by instead is one lens the user selects, applied to a
comparable cohort.  Cross-boundary ranking (bank vs. industrial) raises rather
than silently sorting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import settings
from .guard import assert_comparable
from .provenance import MISSING_INPUT, Q, combine

#: Named lenses. Each is (metric key, direction) where direction=-1 means
#: "lower is better".
LENSES: dict[str, tuple[str, int]] = {
    "ebit_ev_yield": ("ebit_ev_yield", 1),
    "acquirers_multiple": ("acquirers_multiple", -1),
    "incremental_roic": ("incremental_roic", 1),
    "roic": ("roic", 1),
    "expectations_gap": ("expectations_gap", -1),
    "fcf_conversion_mean": ("fcf_conversion_mean", 1),
    "ptbv_residual": ("ptbv_residual", -1),
    "rotce": ("rotce", 1),
}


@dataclass
class Ranked:
    lens: str
    direction: int
    rows: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"lens": self.lens,
                "direction": "higher is better" if self.direction > 0 else "lower is better",
                "rows": self.rows, "excluded": self.excluded}


def rank_by(lens: str, rows: Sequence[Mapping[str, Any]]) -> Ranked:
    """Rank a cohort by one lens.

    Rows whose lens metric is null are EXCLUDED and listed with the reason --
    never sorted to the bottom, which would read as "worst" rather than
    "unknown", and never imputed.
    """
    if lens not in LENSES:
        raise KeyError(f"unknown lens {lens!r}; available: {sorted(LENSES)}")
    key, direction = LENSES[lens]

    schemas = {r.get("schema", "industrial") for r in rows}
    if len(schemas) > 1:
        a, b = sorted(schemas)[:2]
        assert_comparable(a, b)

    out = Ranked(lens=lens, direction=direction)
    scored: list[tuple[float, dict[str, Any]]] = []
    for r in rows:
        q = r.get("metrics", {}).get(key)
        if r.get("blocked"):
            out.excluded.append({"ticker": r.get("ticker"),
                                 "reason": "RECONCILIATION_FAILED",
                                 "detail": r.get("blocked")})
            continue
        if not isinstance(q, Q) or not q.ok:
            out.excluded.append({
                "ticker": r.get("ticker"),
                "reason": (q.reason if isinstance(q, Q) else "metric absent"),
                "missing": list(q.missing) if isinstance(q, Q) else [key]})
            continue
        scored.append((q.value, {"ticker": r.get("ticker"), "value": q.value,
                                 "unit": q.unit, "as_of": q.as_of,
                                 "coverage": r.get("coverage"),
                                 "audit": f"ledger audit {r.get('ticker')}"}))
    scored.sort(key=lambda t: t[0], reverse=direction > 0)
    for i, (_, row) in enumerate(scored, 1):
        row["rank"] = i
        out.rows.append(row)
    return out


# ---------------------------------------------------------------------------
# the optional composite
# ---------------------------------------------------------------------------


@dataclass
class Composite:
    value: Q
    inputs: dict[str, Q]
    weights: dict[str, float]
    null_fraction: float
    refused: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value.to_dict(),
            "input_vector": {k: v.to_dict() for k, v in sorted(self.inputs.items())},
            "weights": self.weights,
            "null_fraction": self.null_fraction,
            "refused": self.refused,
            "reason": self.reason,
            "note": ("Composite is optional, off by default, and never the "
                     "headline. Its full input vector is shown above because a "
                     "composite without one is unfalsifiable."),
        }


def composite(inputs: Mapping[str, Q], weights: Mapping[str, float],
              *, enabled: bool | None = None,
              max_null_fraction: float | None = None) -> Composite:
    """A weighted composite that refuses to compute around missing data.

    Above the null threshold the composite is not merely less reliable, it is a
    different statistic -- a weighted average of whichever inputs happened to be
    available, which is not comparable to the same composite computed on a
    complete vector.  So it refuses.
    """
    cfg = settings()["scoring"]
    on = cfg["composite_enabled"] if enabled is None else enabled
    thr = float(cfg["max_null_fraction"] if max_null_fraction is None
                else max_null_fraction)

    if not on:
        return Composite(
            value=Q.null("disabled", missing=("composite scoring is disabled; "
                                              "enable in config/settings.yaml",),
                         label="composite"),
            inputs=dict(inputs), weights=dict(weights), null_fraction=0.0,
            refused=True, reason="composite_disabled")

    total = len(inputs)
    nulls = [k for k, q in inputs.items() if not q.ok]
    frac = (len(nulls) / total) if total else 1.0
    if frac > thr:
        return Composite(
            value=Q.null(MISSING_INPUT,
                         missing=[f"composite refused: {frac:.0%} of inputs null "
                                  f"(limit {thr:.0%})"] + nulls,
                         label="composite"),
            inputs=dict(inputs), weights=dict(weights), null_fraction=frac,
            refused=True, reason="too_many_nulls")

    used = {k: q for k, q in inputs.items() if q.ok}
    w = {k: float(weights.get(k, 0.0)) for k in used}
    wsum = sum(w.values())
    if wsum == 0:
        return Composite(
            value=Q.null(MISSING_INPUT, missing=("all weights are zero",),
                         label="composite"),
            inputs=dict(inputs), weights=dict(weights), null_fraction=frac,
            refused=True, reason="zero_weights")

    val = combine("composite", " + ".join(f"{w[k]}*{k}" for k in sorted(used)),
                  "score", lambda **kw: sum(w[k] * v for k, v in kw.items()) / wsum,
                  used)
    return Composite(value=val, inputs=dict(inputs), weights=dict(weights),
                     null_fraction=frac, refused=False)
