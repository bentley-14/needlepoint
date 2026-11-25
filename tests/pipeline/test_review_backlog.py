"""Tests for the rolling review queue + the worklist email digest."""
from __future__ import annotations

import json

import pipeline.scrapers.gold_set_builder as m
from pipeline.scrapers.gold_set_builder import (
    GoldSetBuilder, format_review_backlog, read_review_queue,
)
from pipeline.scheduled_scripts import email_review_backlog as erb


def test_format_backlog_actions_and_quiet_empty():
    assert "nothing needs human review" in format_review_backlog([])
    items = [
        {"kind": "gate_hold", "npi": "1", "defendant_name": "Fuzzy P",
         "reason": "name match is 'fuzzy' (not exact/prefix) ..."},
        {"kind": "gate_hold", "npi": "2", "defendant_name": "Ungrounded P",
         "reason": "active-years grounding 'inferred' ..."},
        {"kind": "update", "npi": "3", "defendant_name": "Known P", "target_case_id": "DOJ-x"},
    ]
    d = format_review_backlog(items)
    assert "3 item(s) need human review" in d
    assert "what each ACTION means" in d  # plain-English legend at the top
    assert "find/confirm the right NPI" in d
    assert "supply the documented conduct window" in d
    assert "confirm whether to apply" in d
    assert "delete its line" in d  # tells the human how to clear it


def test_write_review_file_is_rolling_and_dedups(tmp_path, monkeypatch):
    q = tmp_path / "needs_review.jsonl"
    monkeypatch.setattr(m, "REVIEW_QUEUE", q)
    monkeypatch.setattr(m, "PROPOSALS_DIR", tmp_path)
    a = {"kind": "gate_hold", "npi": "1", "defendant_name": "A", "reason": "x"}
    b = {"kind": "gate_hold", "npi": "2", "defendant_name": "B", "reason": "y"}
    GoldSetBuilder._write_review_file([a], run_dir=None)
    GoldSetBuilder._write_review_file([a, b], run_dir=None)  # re-run: 'a' must not duplicate
    items = read_review_queue()
    assert len(items) == 2 and {i["npi"] for i in items} == {"1", "2"}


def test_email_backlog_runner_emits_flags(tmp_path, monkeypatch):
    q = tmp_path / "needs_review.jsonl"
    q.write_text(json.dumps({"kind": "gate_hold", "npi": "1", "defendant_name": "A", "reason": "x"}) + "\n")
    monkeypatch.setattr(m, "REVIEW_QUEUE", q)
    gho = tmp_path / "gh_out"; monkeypatch.setenv("GITHUB_OUTPUT", str(gho))
    body = tmp_path / "d.txt"
    erb.main(["--body-file", str(body)])
    assert "need human review" in body.read_text()
    out = gho.read_text()
    assert "open_items=1" in out and "should_notify=true" in out
