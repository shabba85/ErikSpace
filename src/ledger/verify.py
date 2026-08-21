"""Reconcile computed metrics against a primary filing.

The vendors are a convenience.  The filing is the authority.  This module takes
a set of figures transcribed from (or fetched from) the issuer's own annual
statements and compares every corresponding computed metric, producing the table
that answers "does this system actually agree with the filing?".

A filing fixture is a JSON file:

    {
      "ticker": "XYZ.TO",
      "period_end": "2025-12-31",
      "source_name": "XYZ Ltd 2025 Annual Report, consolidated statements",
      "source_url": "https://www.sedarplus.ca/...",
      "currency": "CAD",
      "unit_scale": 1000000,        # figures below are in millions
      "figures": {"revenue": 10000, "net_income": 1350, ...}
    }

Nothing here invents a figure: if a filing does not disclose a line, it is
absent from `figures` and reported as "not disclosed", not as a mismatch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .model import Company
from .provenance import Q


@dataclass
class FilingComparison:
    ticker: str
    period_end: str
    source_name: str
    source_url: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    tolerance_pct: float = 0.5

    @property
    def compared(self) -> int:
        return sum(1 for r in self.rows if r["status"] in ("match", "mismatch"))

    @property
    def matches(self) -> int:
        return sum(1 for r in self.rows if r["status"] == "match")

    @property
    def mismatches(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["status"] == "mismatch"]

    @property
    def reconciles(self) -> bool:
        return self.compared > 0 and not self.mismatches

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker, "period_end": self.period_end,
            "source_name": self.source_name, "source_url": self.source_url,
            "tolerance_pct": self.tolerance_pct,
            "compared": self.compared, "matches": self.matches,
            "mismatches": len(self.mismatches), "reconciles": self.reconciles,
            "rows": self.rows,
        }

    def table(self) -> str:
        w = max([len(r["field"]) for r in self.rows] + [10])
        head = (f"{'field'.ljust(w)}  {'filing':>18}  {'computed':>18}  "
                f"{'diff %':>8}  status")
        lines = [f"{self.ticker}  period_end={self.period_end}",
                 f"source: {self.source_name}", f"        {self.source_url}",
                 f"tolerance: {self.tolerance_pct}%", "", head, "-" * len(head)]
        for r in self.rows:
            f = ("-".rjust(18) if r["filing"] is None
                 else f"{r['filing']:>18,.2f}")
            c = ("-".rjust(18) if r["computed"] is None
                 else f"{r['computed']:>18,.2f}")
            d = "-".rjust(8) if r["diff_pct"] is None else f"{r['diff_pct']:>8.3f}"
            lines.append(f"{r['field'].ljust(w)}  {f}  {c}  {d}  {r['status']}")
        lines.append("")
        lines.append(f"{self.matches}/{self.compared} fields reconcile within "
                     f"{self.tolerance_pct}%"
                     + ("" if self.reconciles else
                        f"  --  {len(self.mismatches)} MISMATCH"))
        return "\n".join(lines)


def load_filing(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def compare_to_filing(computed: Mapping[str, Q], filing: Mapping[str, Any], *,
                      tolerance_pct: float = 0.5) -> FilingComparison:
    """Compare each computed metric against the filing figure of the same name."""
    scale = float(filing.get("unit_scale", 1) or 1)
    figures = {k: (None if v is None else float(v) * scale)
               for k, v in (filing.get("figures") or {}).items()}
    cmp = FilingComparison(
        ticker=filing.get("ticker", ""), period_end=filing.get("period_end", ""),
        source_name=filing.get("source_name", ""),
        source_url=filing.get("source_url", ""), tolerance_pct=tolerance_pct)

    for field_name in sorted(set(figures) | set(computed)):
        filed = figures.get(field_name)
        q = computed.get(field_name)
        cv = q.value if isinstance(q, Q) and q.ok else None

        if filed is None and cv is None:
            status, diff = "not disclosed / not computed", None
        elif filed is None:
            status, diff = "not disclosed in filing", None
        elif cv is None:
            status = ("not computed: "
                      + (q.reason or "absent") if isinstance(q, Q) else "not computed")
            diff = None
        else:
            denom = max(abs(filed), abs(cv))
            diff = 0.0 if denom == 0 else abs(filed - cv) / denom * 100.0
            status = "match" if diff <= tolerance_pct else "mismatch"

        cmp.rows.append({
            "field": field_name, "filing": filed, "computed": cv,
            "diff_pct": diff, "status": status,
            "missing": list(q.missing) if isinstance(q, Q) and not q.ok else [],
            "source_url": (q.prov.source_url if isinstance(q, Q) and q.prov else None),
        })
    return cmp


def computed_figures(co: Company, analysis: Any) -> dict[str, Q]:
    """The set of figures that can be checked directly against a filing.

    Statement line items plus the derived figures a filing itself states
    (EBITDA is not one -- companies define it differently, so comparing ours to
    theirs would compare two different definitions).
    """
    out: dict[str, Q] = {}
    p = co.latest_annual()
    if p:
        for name, q in p.fields.items():
            out[name] = q
    for name in ("free_cash_flow", "nopat", "invested_capital", "market_cap",
                 "enterprise_value"):
        q = analysis.metrics.get(name)
        if isinstance(q, Q):
            out[name] = q
    return out
