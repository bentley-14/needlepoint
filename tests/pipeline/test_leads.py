"""Tests for pipeline.leads — the Lead contract + identity / HTML helpers."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from pipeline.base import url_to_doc_id
from pipeline.leads import Lead, make_lead_id, normalize_url, strip_html


# ---------------------------------------------------------------------------
# identity helpers
# ---------------------------------------------------------------------------


def test_normalize_url_lowercases_and_strips_trailing_slash():
    assert normalize_url("HTTPS://E.com/Foo/") == "https://e.com/foo"
    assert normalize_url("  https://e.com/foo  ") == "https://e.com/foo"


def test_make_lead_id_is_stable_across_case_and_trailing_slash():
    a = make_lead_id("https://e.com/foo")
    assert a == make_lead_id("https://e.com/foo/")
    assert a == make_lead_id("HTTPS://E.COM/FOO")


def test_make_lead_id_matches_systemwide_url_hash():
    # Lead identity must equal the DOJ scraper's doc_id for the same URL,
    # so a DOJ Lead and its curator_input row collapse to one id.
    url = "https://www.justice.gov/opa/pr/some-release"
    assert make_lead_id(url) == url_to_doc_id(normalize_url(url))
    assert len(make_lead_id(url)) == 16


# ---------------------------------------------------------------------------
# strip_html
# ---------------------------------------------------------------------------


def test_strip_html_removes_tags_unescapes_entities_keeps_paragraphs():
    out = strip_html("<p>First para.</p><p>Second &amp; third.</p>")
    assert "First para." in out
    assert "Second & third." in out      # entity unescaped
    assert "<p>" not in out and "&amp;" not in out
    assert "\n" in out                    # paragraph break preserved


def test_strip_html_handles_none_and_empty():
    assert strip_html(None) == ""
    assert strip_html("") == ""


# ---------------------------------------------------------------------------
# Lead contract
# ---------------------------------------------------------------------------


def _valid_kwargs(**over):
    base = dict(
        provenance="doj_direct",
        primary_source_url="https://e.com/a",
        raw_text="A Medicare fraud scheme...",
    )
    base.update(over)
    return base


def test_lead_builds_and_autoderives_lead_id():
    lead = Lead(**_valid_kwargs())
    assert lead.lead_id == make_lead_id("https://e.com/a")
    assert lead.provenance == "doj_direct"
    assert lead.discovery_meta == {}


def test_lead_explicit_lead_id_is_kept():
    lead = Lead(**_valid_kwargs(lead_id="abc123"))
    assert lead.lead_id == "abc123"


def test_lead_empty_raw_text_is_a_business_error():
    with pytest.raises(ValidationError, match="raw_text is empty"):
        Lead(**_valid_kwargs(raw_text="   "))


def test_lead_missing_url_rejected():
    with pytest.raises(ValidationError, match="primary_source_url is required"):
        Lead(**_valid_kwargs(primary_source_url=""))


def test_lead_wrong_typed_published_date_rejected_by_pydantic():
    # the epoch-int-where-an-ISO-string-belongs case — caught for free by the type.
    with pytest.raises(ValidationError):
        Lead(**_valid_kwargs(published_date=1780262187))


def test_lead_forbids_unknown_fields():
    # a source adapter must use discovery_meta, not smuggle new top-level fields.
    with pytest.raises(ValidationError):
        Lead(**_valid_kwargs(defendant_name="Dr. X"))
