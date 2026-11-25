"""Tests for the weekly-cron runner + the run_once orchestration (links 2–4)."""
from __future__ import annotations

import pipeline.scrapers.gold_set_builder as m
from pipeline.scrapers.gold_set_builder import GoldSetBuilder
from pipeline.scheduled_scripts import run_gold_set_builder as runner


def test_run_once_quiet_when_no_leads(monkeypatch):
    # idle week: no leads from EITHER source → curator skipped → quiet digest (no network)
    monkeypatch.setattr(m, "doj_leads", lambda: [])
    monkeypatch.setattr(m, "takedown_leads", lambda: [])
    report, digest = GoldSetBuilder().run_once(dry_run=True)
    assert report["append"] == [] and report["update"] == []
    assert "0 label(s) added, 0 need review" in digest


def test_runner_writes_body_and_emits_flags(tmp_path, monkeypatch):
    canned = (
        {"counts": {"accept": 3, "gate_hold": 2, "park": 0, "skip": 1},
         "append": [{"npi": "1", "defendant_name": "A"}],
         "update": [{"npi": "2", "defendant_name": "B"}],
         "written": True},
        "DIGEST BODY",
    )
    monkeypatch.setattr(GoldSetBuilder, "run_once", lambda self, **k: canned)
    gho = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gho))
    body = tmp_path / "digest.txt"

    rc = runner.main(["--body-file", str(body)])
    assert rc == 0
    assert body.read_text() == "DIGEST BODY"

    out = gho.read_text()
    assert "added=1" in out                         # 1 append
    assert "needs_review=3" in out                  # 1 update + 2 gate_hold
    assert "changed=true" in out                    # written
    assert "should_notify=true" in out


def test_runner_quiet_run_does_not_notify(tmp_path, monkeypatch):
    canned = ({"counts": {"accept": 0, "gate_hold": 0, "park": 0, "skip": 5},
               "append": [], "update": [], "written": False}, "nothing")
    monkeypatch.setattr(GoldSetBuilder, "run_once", lambda self, **k: canned)
    gho = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gho))
    runner.main(["--body-file", str(tmp_path / "d.txt")])
    out = gho.read_text()
    assert "should_notify=false" in out and "changed=false" in out
