"""Regression: EvidenceItem.confidence coerces qualitative/string grades to float.

Guards the 2026-06-10 calibration-loop abort: the iter-2 investigator emitted
`confidence` as "high"/"medium" strings, failing schema validation across all
three repair attempts and aborting a ~30-min run. The coercer (schemas.py
``_coerce_confidence``) absorbs the value the agent DID provide, while a
genuinely MISSING confidence stays required (repair-loop path intact).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pydantic
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fraud_investigator.schemas import EvidenceItem, InvestigatorOutput  # noqa: E402


def _mk(confidence):
    return EvidenceItem(
        id="ev-x", tier="confirmed_fact", fact="a long enough fact",
        citation="src", confidence=confidence,
    )


@pytest.mark.parametrize("raw,expected", [
    ("high", 0.9), ("High", 0.9), ("very high", 0.95),
    ("medium", 0.6), ("moderate", 0.6), ("med", 0.6),
    ("low", 0.3), ("very low", 0.15),
    ("0.85", 0.85), ("95%", 0.95), ("95", 0.95),  # numeric strings + percent
    (0.9, 0.9), (1, 1.0),                          # numbers pass through
])
def test_confidence_coerced(raw, expected):
    assert _mk(raw).confidence == pytest.approx(expected)


def test_unmapped_string_still_raises():
    # An unmappable value must NOT be silently coerced — Pydantic raises a clear
    # error so the claude_p repair loop re-prompts (don't fabricate a grade).
    with pytest.raises(pydantic.ValidationError):
        _mk("banana")


def test_out_of_range_still_raises():
    # ge/le bounds still enforced after coercion.
    with pytest.raises(pydantic.ValidationError):
        _mk(1.5)


def test_missing_confidence_still_required():
    # A genuinely missing confidence stays required → "field required" → repair.
    with pytest.raises(pydantic.ValidationError) as ei:
        EvidenceItem(id="ev-x", tier="confirmed_fact",
                     fact="a long enough fact", citation="src")
    assert "confidence" in str(ei.value)


# ── InvestigatorOutput freeform list[str] coercion (surprises / absences / hyps) ──
# The 2026-06-10 iter-2 abort: the agent emitted these as lists of {id, description}
# objects (and nulls). Recover plain list[str] instead of failing validation.

def _inv(**kw):
    return InvestigatorOutput(case_id="c", iteration="2", **kw)


@pytest.mark.parametrize("field", ["surprises", "unexpected_absences", "working_hypotheses"])
def test_str_list_drops_nulls(field):
    assert getattr(_inv(**{field: [None, "real", None]}), field) == ["real"]


@pytest.mark.parametrize("field", ["surprises", "unexpected_absences", "working_hypotheses"])
def test_str_list_collapses_objects_to_description(field):
    o = _inv(**{field: [{"id": "x-1", "description": "the surprise text"}, "plain"]})
    assert getattr(o, field) == ["the surprise text", "plain"]


def test_str_list_object_without_text_falls_back_to_json():
    assert _inv(surprises=[{"foo": "bar"}]).surprises == ['{"foo":"bar"}']


def test_str_list_all_null_becomes_empty():
    # [] still validates — the prompt (not the schema) enforces the ≥1 norm.
    assert _inv(surprises=[None, None]).surprises == []
