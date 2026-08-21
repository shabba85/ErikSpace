"""Runtime guards.

These exist because the rules they enforce are the ones that would be most
expensive to violate quietly, and because a rule enforced only by convention is
a rule that will eventually be broken by a well-meaning edit.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .model import FORBIDDEN_FOR_FINANCIALS
from .provenance import Q


class GuardViolation(AssertionError):
    pass


def assert_no_financial_bleed(schema: str, metrics: Mapping[str, Any]) -> None:
    """A bank or insurer must not carry industrial metrics -- at all.

    Not as zero, not as a placeholder, not as a not-applicable Q sitting in the
    dict where a template might render it.  Absent.
    """
    if schema not in ("bank", "insurer"):
        return
    present = [k for k in metrics if k in FORBIDDEN_FOR_FINANCIALS]
    if present:
        raise GuardViolation(
            f"{schema} metric set contains industrial metrics {sorted(present)}; "
            "these must not be computed for a financial business model"
        )


def assert_no_fabricated_numbers(metrics: Mapping[str, Any]) -> None:
    """Every populated Q must carry provenance.  No exceptions."""
    bad = []
    for k, v in metrics.items():
        if isinstance(v, Q) and v.ok and v.prov is None:
            bad.append(k)
    if bad:
        raise GuardViolation(
            f"populated quantities without provenance: {sorted(bad)}. "
            "Every number must name its source."
        )


def assert_comparable(schema_a: str, schema_b: str) -> None:
    """Scores are never comparable across the financial boundary.

    A bank's composite and an industrial's composite are built from disjoint
    input vectors.  Ranking them together produces an ordering that means
    nothing, so the code refuses rather than the documentation discouraging it.
    """
    fin_a = schema_a in ("bank", "insurer")
    fin_b = schema_b in ("bank", "insurer")
    if fin_a != fin_b:
        raise GuardViolation(
            f"cannot compare a {schema_a} with a {schema_b}: financial and "
            "non-financial scores are built from disjoint inputs and have no "
            "common scale"
        )
