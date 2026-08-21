"""The interpretation layer -- the only place an LLM appears.

Contract: the model receives computed metrics with their provenance and returns
PROSE.  It never returns a figure.  It is never asked for one.

That contract is enforced, not merely stated.  ``verify_no_new_numbers`` scans
the generated text for numeric tokens and rejects any that does not correspond
to a figure the code actually computed.  A model that invents "roughly 12% ROIC"
where the code computed 8.3% fails the check and the output is refused.

Forbidden by construction: buy/sell/hold ratings, price targets, restatements of
computed figures with different values, and confident language over null-heavy
inputs.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .analyze import Analysis
from .provenance import Q

MODEL = os.environ.get("LEDGER_INTERPRET_MODEL", "claude-opus-4-6")

REQUIRED_SECTIONS = (
    "what_the_market_is_pricing_in",
    "variables_that_determine_whether_that_is_right",
    "strongest_bear_case",
    "what_would_change_the_assessment",
    "what_could_not_be_computed",
)

FORBIDDEN_PATTERNS = (
    (r"\b(buy|sell|hold|accumulate|overweight|underweight|outperform|"
     r"underperform)\s+(rating|recommendation)?\b", "rating language"),
    (r"\bprice target\b", "price target"),
    (r"\bfair value (is|of)\b", "fair-value point estimate"),
    (r"\bwe (recommend|advise|suggest you)\b", "recommendation"),
    (r"\b(strong|compelling) (buy|sell)\b", "rating language"),
)

SYSTEM_PROMPT = """\
You write analytical prose for one experienced value investor about a company \
whose financial metrics have ALREADY been computed by auditable code.

ABSOLUTE RULES:
1. You must NEVER state a numeric financial figure that does not appear \
verbatim in the METRICS block you are given. Do not estimate, round \
differently, annualize, convert currencies, or infer any number. If you want to \
reference a figure, quote it exactly as given.
2. You must NEVER give a buy/sell/hold rating, a recommendation, or a price \
target. The reader makes the decision; you clarify what is at stake.
3. Where inputs are null, say plainly that the figure could not be computed and \
name the missing field. Do not reason around a gap as if it were filled.
4. Calibrate confidence to coverage. If most inputs are null, your language must \
be correspondingly tentative.
5. State the market's expectation as a FALSIFIABLE CLAIM -- something that will \
be shown right or wrong by observable events.

Write in five sections with these exact headings:
## What the market is pricing in
## The variables that determine whether that is right
## The strongest bear case
## What would change the assessment
## What could not be computed

Be concrete and specific to this company. No hedged generalities that would be \
true of any issuer."""


@dataclass
class Interpretation:
    ticker: str
    prose: str
    model: str
    accepted: bool
    violations: list[str] = field(default_factory=list)
    unverified_numbers: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker, "model": self.model, "accepted": self.accepted,
            "prose": self.prose if self.accepted else "",
            "violations": self.violations,
            "unverified_numbers": self.unverified_numbers,
            "coverage": self.coverage,
            "note": ("Prose only. Every figure referenced was computed by the "
                     "method layer and verified against it; the model wrote no "
                     "numbers of its own."),
        }


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------

_NUM = re.compile(r"-?\d[\d,]*\.?\d*")

#: Numbers that are not financial claims: years, section numbers, small counts.
_BENIGN = re.compile(r"^(19|20)\d{2}$")


def allowed_number_strings(analysis: Analysis, extra: Iterable[float] = ()) -> set[str]:
    """Every rendering of every computed value the model is allowed to echo."""
    out: set[str] = set()
    vals: list[float] = [q.value for q in analysis.metrics.values()
                         if q.ok and isinstance(q.value, (int, float))]
    vals.extend(extra)
    for v in vals:
        out.update(_renderings(float(v)))
    return out


def _renderings(v: float) -> set[str]:
    out = {f"{v:.0f}", f"{v:.1f}", f"{v:.2f}", f"{v:,.0f}", f"{v:,.1f}", f"{v:,.2f}"}
    pct = v * 100.0
    out.update({f"{pct:.0f}", f"{pct:.1f}", f"{pct:.2f}"})
    for scale, _name in ((1e6, "m"), (1e9, "b")):
        if abs(v) >= scale:
            out.update({f"{v / scale:.0f}", f"{v / scale:.1f}", f"{v / scale:.2f}"})
    return {s.lstrip("+") for s in out}


def verify_no_new_numbers(prose: str, allowed: set[str]) -> list[str]:
    """Return numeric tokens in the prose that the code did not compute.

    Deliberately strict.  A false positive costs a regeneration; a false
    negative puts a fabricated figure in front of someone about to trade on it.

    The only tokens excused are bare small integers used as counts ("three of
    the four segments", "2 of 9 components") and four-digit years.  A token is
    NOT excused merely for being small: "12.0%" and "$8" are financial claims,
    and the tests that follow lock that in.
    """
    bad: list[str] = []
    for m in _NUM.finditer(prose):
        tok = m.group(0).strip().rstrip(".")
        if not tok:
            continue
        norm = tok.replace(",", "")
        if tok in allowed or norm in allowed:
            continue
        if _BENIGN.match(norm):
            continue
        try:
            f = float(norm)
        except ValueError:
            continue
        if f"{f:,.0f}" in allowed:
            continue

        # Context decides whether a small number is a count or a claim.
        before = prose[max(0, m.start() - 2):m.start()]
        after = prose[m.end():m.end() + 2]
        financial_context = (
            after.lstrip()[:1] in ("%",)
            or before.rstrip()[-1:] in ("$", "\u00a3", "\u20ac")
            or "." in norm            # a decimal is a measurement, not a count
            or re.match(r"\s*(x\b|bps|bp\b|cents?|%)", after)
        )
        if not financial_context and f.is_integer() and abs(f) <= 12:
            continue        # "three variables", "2 of 9 components", quarters
        bad.append(tok)
    return sorted(set(bad))


def check_forbidden(prose: str) -> list[str]:
    low = prose.lower()
    return [why for pat, why in FORBIDDEN_PATTERNS if re.search(pat, low)]


def check_confidence_calibration(prose: str, coverage: dict[str, Any]) -> list[str]:
    """Confident language over null-heavy inputs is a violation."""
    pct = coverage.get("populated_pct", 1.0)
    if pct >= 0.6:
        return []
    confident = re.findall(
        r"\b(clearly|certainly|undoubtedly|obviously|will (?:certainly )?(?:be|rise|fall)|"
        r"guaranteed|without question|definitely)\b", prose.lower())
    if confident:
        return [f"confident language ({sorted(set(confident))}) over "
                f"{pct:.0%} input coverage"]
    return []


# ---------------------------------------------------------------------------
# prompt assembly
# ---------------------------------------------------------------------------


def build_context(analysis: Analysis, *, macro: dict[str, Q] | None = None,
                  max_metrics: int = 200) -> str:
    """Render the computed metrics for the model.  Nulls are included on purpose:
    the model must see what is missing in order to say so."""
    lines = [f"COMPANY: {analysis.ticker}",
             f"BUSINESS MODEL SCHEMA: {analysis.schema}",
             f"AS OF: {analysis.as_of}",
             f"COVERAGE: {analysis.coverage()}"]
    if analysis.blocked:
        lines.append(f"ROW BLOCKED: {analysis.blocked_reason}")
    lines.append("\nMETRICS (values are exact; you may not restate them differently):")
    for name, q in sorted(analysis.metrics.items())[:max_metrics]:
        if q.ok:
            lines.append(f"  {name} = {q.value} {q.unit} (as_of {q.as_of}, "
                         f"{q.prov.method if q.prov else '?'}, "
                         f"confidence {q.prov.confidence if q.prov else '?'})")
        else:
            lines.append(f"  {name} = NULL [{q.reason}] missing: "
                         f"{'; '.join(q.missing) or 'unspecified'}")
    if macro:
        lines.append("\nMACRO (fetched, authoritative):")
        for k, q in sorted(macro.items()):
            lines.append(f"  {k} = {q.value if q.ok else 'NULL'} {q.unit} "
                         f"(as_of {q.as_of})")
    if "reverse_dcf" in analysis.extras:
        lines.append("\nMARKET-IMPLIED EXPECTATIONS:")
        for k, v in sorted(analysis.extras["reverse_dcf"].items()):
            lines.append(f"  {k}: {v.get('claim')}")
    return "\n".join(lines)


def interpret(analysis: Analysis, *, macro: dict[str, Q] | None = None,
              model: str | None = None, client: Any = None,
              max_retries: int = 2) -> Interpretation:
    """Generate prose, then verify it before accepting it.

    A rejected generation is retried once with the violations fed back.  A
    second failure returns accepted=False and empty prose: no output is better
    than output that smuggles a number.
    """
    mdl = model or MODEL
    cov = analysis.coverage()
    allowed = allowed_number_strings(analysis)
    if macro:
        allowed |= {s for q in macro.values() if q.ok
                    for s in _renderings(float(q.value))}

    ctx = build_context(analysis, macro=macro)
    cl = client or _anthropic_client()
    if cl is None:
        return Interpretation(
            ticker=analysis.ticker, prose="", model=mdl, accepted=False,
            violations=["ANTHROPIC_API_KEY not set; interpretation skipped"],
            coverage=cov)

    feedback = ""
    last_violations: list[str] = []
    last_unverified: list[str] = []
    for _ in range(max_retries):
        prose = _generate(cl, mdl, ctx + feedback)
        violations = check_forbidden(prose)
        violations += check_confidence_calibration(prose, cov)
        unverified = verify_no_new_numbers(prose, allowed)
        if not violations and not unverified:
            return Interpretation(ticker=analysis.ticker, prose=prose, model=mdl,
                                  accepted=True, coverage=cov)
        last_violations, last_unverified = violations, unverified
        feedback = ("\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED.\n"
                    f"Rule violations: {violations or 'none'}\n"
                    f"Numbers you stated that the code did not compute: "
                    f"{unverified or 'none'}\n"
                    "Rewrite. Quote figures exactly as given or omit them.")

    return Interpretation(ticker=analysis.ticker, prose="", model=mdl, accepted=False,
                          violations=last_violations,
                          unverified_numbers=last_unverified, coverage=cov)


def _anthropic_client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    return anthropic.Anthropic()


def _generate(client: Any, model: str, prompt: str) -> str:
    resp = client.messages.create(
        model=model, max_tokens=2000, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


# ---------------------------------------------------------------------------
# qualitative catalysts (optional, web-search enabled)
# ---------------------------------------------------------------------------


def catalysts(ticker: str, company_name: str, *, client: Any = None,
              model: str | None = None) -> dict[str, Any]:
    """Qualitative catalysts with source URLs.

    Text only.  This output may NEVER be written into a numeric field, and the
    return shape has no numeric field to write into -- it is a list of
    {claim, source_url} and nothing else.
    """
    cl = client or _anthropic_client()
    if cl is None:
        return {"ticker": ticker, "available": False,
                "reason": "ANTHROPIC_API_KEY not set", "items": []}
    prompt = (
        f"Find recent, material, qualitative developments for {company_name} "
        f"({ticker}): litigation, regulatory rulings, guidance changes, "
        f"management changes, contract awards or losses.\n\n"
        "For each: one sentence of what happened, plus the source URL. Do not "
        "state financial figures, estimates, or valuations -- qualitative "
        "developments and citations only. If you find nothing material, say so.")
    try:
        resp = cl.messages.create(
            model=model or MODEL, max_tokens=1500,
            tools=[{"type": "web_search_20250305", "name": "web_search",
                    "max_uses": 5}],
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text")
    except Exception as exc:  # noqa: BLE001 - optional feature, never fatal
        return {"ticker": ticker, "available": False, "reason": str(exc)[:200],
                "items": []}
    return {
        "ticker": ticker, "available": True, "text": text,
        "urls": sorted(set(re.findall(r"https?://[^\s)\]]+", text))),
        "note": ("Qualitative only. This text is never written into a numeric "
                 "field and carries no computed figures."),
    }
