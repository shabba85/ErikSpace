"""The reconciliation gate.

Before any row may be scored, three anchors must agree across two independent
sources: last close, diluted share count, and TTM revenue.

Those three are chosen deliberately.  Price is the input every valuation
multiplies by.  Share count is the field vendors most often get wrong on
Canadian small caps -- and a share count error is invisible in the aggregate
statements while corrupting every per-share figure.  Revenue is the top line
that anchors the whole income statement, and disagreement there means the two
sources are looking at different consolidations or different currencies.

Disagreement beyond tolerance does NOT resolve to a preferred source.  It
blocks the row and raises RECONCILIATION_FAILED, because the honest statement
is "two sources disagree and I do not know which is right", and quietly picking
one is how a wrong number acquires false authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .config import settings
from .provenance import RECONCILIATION_FAILED, Q

RECONCILIATION_FAILED_FLAG = "RECONCILIATION_FAILED"
NO_SECOND_SOURCE_FLAG = "NO_SECOND_SOURCE"


@dataclass
class AnchorCheck:
    anchor: str
    primary: Q
    secondary: Q
    primary_source: str
    secondary_source: str
    disagreement_pct: float | None
    tolerance_pct: float
    status: str          # agree | disagree | uncheckable

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor": self.anchor,
            "primary": self.primary.to_dict(),
            "secondary": self.secondary.to_dict(),
            "primary_source": self.primary_source,
            "secondary_source": self.secondary_source,
            "disagreement_pct": self.disagreement_pct,
            "tolerance_pct": self.tolerance_pct,
            "status": self.status,
        }


@dataclass
class Reconciliation:
    ticker: str
    checks: list[AnchorCheck] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    blocked: bool = False
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "blocked": self.blocked,
            "flags": self.flags,
            "reason": self.reason,
            "checks": [c.to_dict() for c in self.checks],
        }

    def blocking_message(self) -> str | None:
        if not self.blocked:
            return None
        bad = [f"{c.anchor}: {c.primary_source}={c.primary.value!r} vs "
               f"{c.secondary_source}={c.secondary.value!r} "
               f"({c.disagreement_pct:.2f}% apart)"
               for c in self.checks
               if c.status == "disagree" and c.disagreement_pct is not None]
        if bad:
            return ("RECONCILIATION_FAILED -- sources disagree beyond tolerance: "
                    + "; ".join(bad))
        return self.reason


def relative_disagreement(a: float, b: float) -> float | None:
    """Percentage disagreement on the larger magnitude.

    Using the larger denominator keeps the measure symmetric: A vs B and B vs A
    give the same answer, which a naive (a-b)/b does not.
    """
    denom = max(abs(a), abs(b))
    if denom == 0:
        return 0.0 if a == b else None
    return abs(a - b) / denom * 100.0


def check_anchor(anchor: str, primary: Q, secondary: Q, primary_source: str,
                 secondary_source: str, tolerance_pct: float) -> AnchorCheck:
    if not (primary.ok and secondary.ok):
        return AnchorCheck(anchor, primary, secondary, primary_source,
                           secondary_source, None, tolerance_pct, "uncheckable")
    d = relative_disagreement(float(primary.value), float(secondary.value))
    if d is None:
        return AnchorCheck(anchor, primary, secondary, primary_source,
                           secondary_source, None, tolerance_pct, "uncheckable")
    status = "agree" if d <= tolerance_pct else "disagree"
    return AnchorCheck(anchor, primary, secondary, primary_source,
                       secondary_source, d, tolerance_pct, status)


def reconcile(ticker: str, primary: Mapping[str, Q], secondary: Mapping[str, Q],
              *, primary_source: str, secondary_source: str,
              anchors: Sequence[str] | None = None,
              tolerance_pct: float | None = None,
              require_second_source: bool | None = None) -> Reconciliation:
    """Run the gate.

    ``primary`` and ``secondary`` map anchor name -> Q from two different
    sources.
    """
    cfg = settings()["reconciliation"]
    anchors = list(anchors or cfg["anchors"])
    tol = float(cfg["tolerance_pct"] if tolerance_pct is None else tolerance_pct)
    require = bool(cfg["require_second_source"] if require_second_source is None
                   else require_second_source)

    rec = Reconciliation(ticker=ticker)

    if not secondary:
        rec.flags.append(NO_SECOND_SOURCE_FLAG)
        if require:
            rec.blocked = True
            rec.reason = (
                "no second source answered; a single-source figure cannot be "
                "cross-checked and this row is not scoreable")
        return rec

    for a in anchors:
        c = check_anchor(a,
                         primary.get(a, Q.null("not_fetched", missing=(a,), label=a)),
                         secondary.get(a, Q.null("not_fetched", missing=(a,), label=a)),
                         primary_source, secondary_source, tol)
        rec.checks.append(c)

    if any(c.status == "disagree" for c in rec.checks):
        rec.blocked = True
        rec.flags.append(RECONCILIATION_FAILED_FLAG)
        rec.reason = rec.blocking_message()
        return rec

    uncheckable = [c.anchor for c in rec.checks if c.status == "uncheckable"]
    if uncheckable:
        rec.flags.append(NO_SECOND_SOURCE_FLAG)
        if require:
            rec.blocked = True
            rec.reason = ("anchors could not be cross-checked: "
                          + ", ".join(uncheckable))
    return rec


def blocked_metric(rec: Reconciliation, label: str) -> Q:
    """The null a blocked row substitutes for every score."""
    return Q.null(RECONCILIATION_FAILED,
                  missing=(rec.blocking_message() or "reconciliation failed",),
                  label=label)
