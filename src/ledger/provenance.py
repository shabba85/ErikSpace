"""Provenance-carrying quantities.

The single invariant this module exists to enforce:

    A number may exist in this system only if it can name where it came from.

There is no constructor for a populated quantity that does not take a
Provenance record.  Derived quantities cannot be built except through
``derive``/``combine``, which record the formula and the provenance ids of
every input.  If any input is null, the output is null and carries the reason
and the names of the missing fields, which is what the UI renders.

Nulls are first-class.  ``Q.null(...)`` is a correct answer; a plausible
invented number is a defect.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

Method = Literal["reported", "derived", "normalized"]
Confidence = Literal["high", "low"]

#: Reasons a quantity can be null.  Kept as a closed vocabulary so the UI can
#: render them consistently and so tests can assert on them.
MISSING_INPUT = "missing_input"
NOT_APPLICABLE = "not_applicable"
NOT_FETCHED = "not_fetched"
INSUFFICIENT_HISTORY = "insufficient_history"
UNDEFINED_MATH = "undefined_math"
RECONCILIATION_FAILED = "reconciliation_failed"
DECK_NOT_SET = "deck_not_set"
UPSTREAM_ERROR = "upstream_error"


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a number came from.

    ``id`` is a content hash, never a random uuid: two identical fetches of
    identical bytes produce the same id, which is what makes recomputation
    byte-identical (acceptance test 6).
    """

    source_name: str
    source_url: str
    retrieved_at: str
    method: Method
    formula: str | None = None
    inputs: tuple[str, ...] = ()
    confidence: Confidence = "high"
    payload_path: str | None = None
    note: str | None = None

    @property
    def id(self) -> str:
        return hashlib.sha256(
            _canon(
                [
                    self.source_name,
                    self.source_url,
                    self.retrieved_at,
                    self.method,
                    self.formula,
                    list(self.inputs),
                    self.confidence,
                    self.payload_path,
                    self.note,
                ]
            ).encode()
        ).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "method": self.method,
            "formula": self.formula,
            "inputs": list(self.inputs),
            "confidence": self.confidence,
            "payload_path": self.payload_path,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class Q:
    """A quantity that is either populated-with-provenance or null-with-reason.

    Never construct a populated Q directly with a fabricated Provenance.  Use
    ``Q.reported`` at the adapter boundary (where a real fetch happened) and
    ``derive``/``combine`` everywhere downstream.
    """

    value: float | int | None
    unit: str = ""
    as_of: str | None = None
    prov: Provenance | None = None
    reason: str | None = None
    missing: tuple[str, ...] = ()
    label: str | None = None

    # ---- construction -------------------------------------------------

    @staticmethod
    def reported(
        value: float | int | None,
        *,
        unit: str,
        as_of: str | date | None,
        source_name: str,
        source_url: str,
        retrieved_at: str,
        confidence: Confidence = "high",
        payload_path: str | None = None,
        label: str | None = None,
        note: str | None = None,
    ) -> "Q":
        """A figure as reported by a source.  ``value=None`` yields a null."""
        if value is None:
            return Q.null(NOT_FETCHED, missing=(label or "value",), unit=unit, label=label)
        return Q(
            value=value,
            unit=unit,
            as_of=_as_of_str(as_of),
            prov=Provenance(
                source_name=source_name,
                source_url=source_url,
                retrieved_at=retrieved_at,
                method="reported",
                confidence=confidence,
                payload_path=payload_path,
                note=note,
            ),
            label=label,
        )

    @staticmethod
    def null(
        reason: str,
        *,
        missing: Iterable[str] = (),
        unit: str = "",
        label: str | None = None,
    ) -> "Q":
        return Q(
            value=None,
            unit=unit,
            as_of=None,
            prov=None,
            reason=reason,
            missing=tuple(sorted(set(missing))),
            label=label,
        )

    @staticmethod
    def not_applicable(why: str, *, label: str | None = None) -> "Q":
        """For fields that are meaningless for a business model (bank ROIC).

        Rendered as "not applicable to this business model", never as a number
        and never as zero.
        """
        return Q(value=None, unit="", as_of=None, prov=None, reason=NOT_APPLICABLE,
                 missing=(), label=label, )._with_note(why)

    def _with_note(self, why: str) -> "Q":
        return replace(self, missing=(why,)) if why else self

    # ---- interrogation -------------------------------------------------

    @property
    def ok(self) -> bool:
        return self.value is not None

    @property
    def is_na(self) -> bool:
        return self.reason == NOT_APPLICABLE

    def named(self, label: str) -> "Q":
        return replace(self, label=label)

    def __bool__(self) -> bool:  # pragma: no cover - guard against truthiness bugs
        raise TypeError(
            "Q has no truth value; a null is not False. Use .ok explicitly."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "as_of": self.as_of,
            "reason": self.reason,
            "missing": list(self.missing),
            "provenance": self.prov.to_dict() if self.prov else None,
        }


def _as_of_str(v: str | date | None) -> str | None:
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    return str(v)[:10]


# ---------------------------------------------------------------------------
# derivation
# ---------------------------------------------------------------------------


def combine(
    label: str,
    formula: str,
    unit: str,
    fn: Callable[..., float | None],
    inputs: Mapping[str, Q],
    *,
    as_of: str | None = None,
    note: str | None = None,
    source_name: str = "ledger:derived",
) -> Q:
    """Compute a derived quantity, or explain precisely why it cannot be.

    ``fn`` receives the raw float of each input by keyword.  It is only called
    when every input is populated, so ``fn`` never has to defend against None.
    If ``fn`` itself returns None (a division by zero, a log of a negative), the
    result is a null with reason ``undefined_math`` rather than a nan.
    """
    missing: list[str] = []
    for name, q in inputs.items():
        if not isinstance(q, Q):
            raise TypeError(f"input {name!r} to {label!r} is {type(q).__name__}, not Q")
        if not q.ok:
            missing.extend(q.missing or (q.label or name,))
    if missing:
        reason = NOT_APPLICABLE if any(q.is_na for q in inputs.values()) else MISSING_INPUT
        return Q.null(reason, missing=missing, unit=unit, label=label)

    raw = {k: q.value for k, q in inputs.items()}
    try:
        out = fn(**raw)
    except ZeroDivisionError:
        return Q.null(UNDEFINED_MATH, missing=(f"{label}: division by zero",), unit=unit, label=label)
    if out is None or (isinstance(out, float) and (math.isnan(out) or math.isinf(out))):
        return Q.null(UNDEFINED_MATH, missing=(f"{label}: undefined",), unit=unit, label=label)

    # Determinism: derived provenance inherits timestamps from its inputs, it
    # never calls the clock.  Two runs over unchanged data therefore produce
    # identical provenance ids.
    provs = [q.prov for q in inputs.values() if q.prov is not None]
    retrieved = max((p.retrieved_at for p in provs), default="")
    conf: Confidence = "low" if any(p.confidence == "low" for p in provs) else "high"
    urls = sorted({p.source_url for p in provs})
    # as_of of a derived figure is the OLDEST of its inputs: a ratio is only as
    # current as its stalest term.
    eff_as_of = as_of or min((q.as_of for q in inputs.values() if q.as_of), default=None)

    return Q(
        value=out,
        unit=unit,
        as_of=eff_as_of,
        prov=Provenance(
            source_name=source_name,
            source_url=urls[0] if len(urls) == 1 else ";".join(urls),
            retrieved_at=retrieved,
            method="derived",
            formula=formula,
            inputs=tuple(sorted({p.id for p in provs})),
            confidence=conf,
            note=note,
        ),
        label=label,
    )


def derive(label: str, formula: str, unit: str, fn: Callable[..., float | None], **inputs: Q) -> Q:
    return combine(label, formula, unit, fn, inputs)


def normalized(
    label: str,
    value: float,
    *,
    unit: str,
    as_of: str | None,
    basis: Q | Sequence[Q],
    formula: str,
    note: str | None = None,
) -> Q:
    """A figure restated onto a different basis (mid-cycle deck, per-share, TTM).

    Carries method='normalized' and the provenance ids of whatever it was
    restated from.
    """
    basis_list = [basis] if isinstance(basis, Q) else list(basis)
    provs = [b.prov for b in basis_list if b.prov is not None]
    return Q(
        value=value,
        unit=unit,
        as_of=as_of,
        prov=Provenance(
            source_name="ledger:normalized",
            source_url=";".join(sorted({p.source_url for p in provs})) or "ledger://normalized",
            retrieved_at=max((p.retrieved_at for p in provs), default=""),
            method="normalized",
            formula=formula,
            inputs=tuple(sorted({p.id for p in provs})),
            confidence="low" if any(p.confidence == "low" for p in provs) else "high",
            note=note,
        ),
        label=label,
    )


# ---------------------------------------------------------------------------
# small helpers used all over the method layer
# ---------------------------------------------------------------------------


def add(label: str, unit: str, **terms: Q) -> Q:
    names = " + ".join(terms)
    return combine(label, names, unit, lambda **kw: sum(kw.values()), terms)


def sub(label: str, unit: str, a: Q, b: Q) -> Q:
    return combine(label, "a - b", unit, lambda a, b: a - b, {"a": a, "b": b})


def ratio(label: str, unit: str, num: Q, den: Q, *, formula: str | None = None) -> Q:
    return combine(
        label,
        formula or "num / den",
        unit,
        lambda num, den: None if den == 0 else num / den,
        {"num": num, "den": den},
    )


def mul(label: str, unit: str, **terms: Q) -> Q:
    names = " * ".join(terms)

    def _m(**kw: float) -> float:
        out = 1.0
        for v in kw.values():
            out *= v
        return out

    return combine(label, names, unit, _m, terms)


def scale(label: str, unit: str, q: Q, k: float, *, formula: str) -> Q:
    """Multiply by a *dimensionless constant that is part of a formula*, not a
    fabricated financial input (e.g. the 1.2 in Altman Z)."""
    return combine(label, formula, unit, lambda q: q * k, {"q": q})


def mean(label: str, unit: str, qs: Sequence[Q], *, min_n: int = 1) -> Q:
    populated = [q for q in qs if q.ok]
    if len(populated) < min_n:
        return Q.null(
            INSUFFICIENT_HISTORY,
            missing=(f"{label}: need {min_n} periods, have {len(populated)}",),
            unit=unit,
            label=label,
        )
    return combine(
        label,
        f"mean of {len(populated)} periods",
        unit,
        lambda **kw: sum(kw.values()) / len(kw),
        {f"p{i}": q for i, q in enumerate(populated)},
    )


def stdev(label: str, unit: str, qs: Sequence[Q], *, min_n: int = 2) -> Q:
    populated = [q for q in qs if q.ok]
    if len(populated) < max(2, min_n):
        return Q.null(
            INSUFFICIENT_HISTORY,
            missing=(f"{label}: need {max(2, min_n)} periods, have {len(populated)}",),
            unit=unit,
            label=label,
        )

    def _sd(**kw: float) -> float:
        vals = list(kw.values())
        m = sum(vals) / len(vals)
        return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))

    return combine(
        label,
        f"sample stdev of {len(populated)} periods",
        unit,
        _sd,
        {f"p{i}": q for i, q in enumerate(populated)},
    )


@dataclass(frozen=True, slots=True)
class Range:
    """Two estimates of the same unobservable quantity, reported as a band.

    Used where a point estimate would be false precision -- maintenance capex
    is the canonical case.
    """

    low: Q
    high: Q
    label: str
    method_low: str
    method_high: str

    @property
    def ok(self) -> bool:
        return self.low.ok and self.high.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "low": self.low.to_dict(),
            "high": self.high.to_dict(),
            "method_low": self.method_low,
            "method_high": self.method_high,
        }
