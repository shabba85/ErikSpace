"""The core invariant: no number without provenance, and nulls that explain
themselves."""

from __future__ import annotations

import pytest

from ledger.provenance import (
    INSUFFICIENT_HISTORY, MISSING_INPUT, NOT_APPLICABLE, UNDEFINED_MATH, Q,
    add, combine, derive, mean, ratio, stdev, sub,
)

R = "2026-01-15T12:00:00+00:00"


def rep(v, label="x", unit="CAD", as_of="2025-12-31", conf="high"):
    return Q.reported(v, unit=unit, as_of=as_of, source_name="s",
                      source_url="http://s", retrieved_at=R, confidence=conf,
                      label=label)


def test_populated_value_always_carries_provenance():
    q = rep(100)
    assert q.ok and q.prov is not None
    assert q.prov.source_url == "http://s"
    assert q.prov.method == "reported"


def test_null_names_the_missing_field():
    q = Q.null(MISSING_INPUT, missing=("depreciation_amortization_cf",), label="oe")
    assert not q.ok
    assert q.value is None
    assert "depreciation_amortization_cf" in q.missing


def test_null_propagates_through_arithmetic():
    good, bad = rep(100, "ni"), Q.null(MISSING_INPUT, missing=("da",), label="da")
    out = add("owner_earnings", "CAD", ni=good, da=bad)
    assert out.value is None
    assert "da" in out.missing
    assert out.reason == MISSING_INPUT


def test_no_default_is_ever_substituted():
    """A missing input must not silently become zero."""
    good = rep(100, "ni")
    zero_like = Q.null(MISSING_INPUT, missing=("maintenance_capex",))
    out = sub("oe", "CAD", good, zero_like)
    assert out.value is None, "a missing subtrahend must not be treated as 0"


def test_division_by_zero_is_null_not_inf():
    out = ratio("m", "x", rep(100), rep(0))
    assert out.value is None
    assert out.reason == UNDEFINED_MATH


def test_nan_and_inf_are_rejected():
    out = derive("bad", "f", "x", lambda a: float("nan"), a=rep(1))
    assert out.value is None and out.reason == UNDEFINED_MATH


def test_not_applicable_is_distinct_from_missing():
    na = Q.not_applicable("banks have no gross margin", label="gross_margin")
    assert na.reason == NOT_APPLICABLE
    assert na.is_na
    assert not na.ok
    out = ratio("m", "x", na, rep(10))
    assert out.reason == NOT_APPLICABLE, "n/a must not degrade to 'missing input'"


def test_derived_records_formula_and_input_provenance_ids():
    a, b = rep(6, "a"), rep(3, "b")
    out = ratio("q", "x", a, b)
    assert out.value == 2.0
    assert out.prov.method == "derived"
    assert out.prov.formula == "num / den"
    assert set(out.prov.inputs) == {a.prov.id, b.prov.id}


def test_derived_as_of_is_the_oldest_input():
    """A ratio is only as current as its stalest term."""
    new = rep(10, "new", as_of="2025-12-31")
    old = rep(5, "old", as_of="2023-12-31")
    assert ratio("q", "x", new, old).as_of == "2023-12-31"


def test_low_confidence_propagates_through_derivation():
    """A metric built from Yahoo inputs stays flagged low-confidence."""
    hi, lo = rep(10, "hi"), rep(5, "lo", conf="low")
    assert ratio("q", "x", hi, lo).prov.confidence == "low"


def test_derivation_never_calls_the_clock():
    """Derived provenance inherits input timestamps, so recomputation is stable."""
    a, b = rep(6), rep(3)
    first = ratio("q", "x", a, b)
    second = ratio("q", "x", a, b)
    assert first.prov.id == second.prov.id
    assert first.prov.retrieved_at == R


def test_provenance_id_is_a_content_hash():
    a = rep(1)
    same = Q.reported(1, unit="CAD", as_of="2025-12-31", source_name="s",
                      source_url="http://s", retrieved_at=R, label="x")
    assert a.prov.id == same.prov.id
    other = Q.reported(1, unit="CAD", as_of="2025-12-31", source_name="s",
                       source_url="http://OTHER", retrieved_at=R, label="x")
    assert a.prov.id != other.prov.id


def test_q_has_no_truth_value():
    """A null must never be mistaken for False in an `if q:` check."""
    with pytest.raises(TypeError):
        bool(Q.null(MISSING_INPUT))


def test_mean_refuses_below_minimum_periods():
    out = mean("m", "ratio", [rep(1), rep(2)], min_n=3)
    assert out.value is None
    assert out.reason == INSUFFICIENT_HISTORY
    assert "have 2" in out.missing[0]


def test_mean_ignores_nulls_but_counts_them_against_the_minimum():
    qs = [rep(1), rep(3), Q.null(MISSING_INPUT, missing=("y2",))]
    out = mean("m", "ratio", qs, min_n=2)
    assert out.value == 2.0
    out2 = mean("m", "ratio", qs, min_n=3)
    assert out2.value is None


def test_stdev_of_identical_values_is_zero_not_null():
    assert stdev("s", "ratio", [rep(2), rep(2), rep(2)]).value == 0.0


def test_combine_rejects_non_q_inputs():
    with pytest.raises(TypeError):
        combine("x", "f", "u", lambda a: a, {"a": 5})
