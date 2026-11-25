"""Tests for the drift-guarded gold_set writer (link 4, real write).

Uses temp files via monkeypatch — never touches the real gold_set / GOLD_SET.md.
Policy under test: APPEND new NPIs only; updates + holds → review file; existing
rows never modified; backup + changelog written.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

import pipeline.scrapers.gold_set_builder as m
from pipeline.scrapers.gold_set_builder import GoldSetBuilder


@pytest.fixture
def goldset_env(tmp_path, monkeypatch):
    gold = tmp_path / "gold_set.jsonl"
    existing = {"npi": "1111111111", "case_id": "EXIST", "defendant_name": "Existing Doc",
                "all_active_years": [2015], "source_url": "https://e.com/exist",
                "_active_years_source": "doj_source_conduct_dates"}  # carries a manual override
    gold.write_text(json.dumps(existing) + "\n")
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "GOLD_SET.md").write_text("# Gold Set\n\n## Changelog\n\n### 2026-05-30 — prior entry\nx\n")
    monkeypatch.setattr(m, "GOLD_SET_PATH", gold)
    monkeypatch.setattr(m, "PROPOSALS_DIR", tmp_path / "proposals")
    monkeypatch.setattr(m, "REVIEW_QUEUE", tmp_path / "proposals" / "needs_review.jsonl")
    monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)
    return tmp_path, gold, existing


def _accept(npi, years, url, name="New Doc"):
    return {"gate_decision": "accept", "npi": npi, "case_id": f"DOJ-{npi}", "defendant_name": name,
            "all_active_years": years, "source_url": url, "fraud_types": ["kickback"],
            "outcome_type": "plea", "outcome_date": "2024-01-01", "outcome_amount_usd": None,
            "covered_years": years, "active_years_source": "source_stated",
            "defendant_specialty": "X", "defendant_state": "MO", "nppes_entity_type": "NPI-1",
            "name_match": "exact", "_outcome_cap": "in_bounds"}


def test_real_write_appends_new_defers_update_and_hold(goldset_env):
    tmp, gold, existing = goldset_env
    df = pd.DataFrame([
        _accept("2222222222", [2020], "https://e.com/new"),                 # new NPI → APPEND
        _accept("1111111111", [2015], "https://e.com/exist", "Existing Doc"),  # known NPI → UPDATE → deferred
        {"gate_decision": "hold", "npi": "3333333333", "defendant_name": "Shaky", "gate_reason": "fuzzy"},
    ])
    report = GoldSetBuilder()._merge_into_gold_set(df, dry_run=False)

    rows = [json.loads(l) for l in gold.read_text().splitlines() if l.strip()]
    npis = {r["npi"] for r in rows}
    # exactly the one new NPI was appended; update was NOT applied
    assert len(rows) == 2 and npis == {"1111111111", "2222222222"}
    assert report["written"] is True
    # existing row untouched (byte-identical to original)
    assert json.dumps(existing) in gold.read_text()


def test_real_write_creates_backup_and_changelog(goldset_env):
    tmp, gold, _ = goldset_env
    df = pd.DataFrame([_accept("2222222222", [2020], "https://e.com/new")])
    report = GoldSetBuilder()._merge_into_gold_set(df, dry_run=False)
    # backup exists and equals the pre-write content (1 row)
    backups = list(tmp.glob("gold_set.jsonl.bak_pre_curator_merge_*"))
    assert len(backups) == 1 and len(backups[0].read_text().splitlines()) == 1
    # changelog got a new dated entry above the prior one
    cl = (tmp / "docs" / "GOLD_SET.md").read_text()
    assert "Automated curator merge (+1 rows)" in cl and "2222222222" in cl
    assert cl.index("Automated curator merge") < cl.index("prior entry")


def test_real_write_review_file_has_deferred_items(goldset_env):
    tmp, gold, _ = goldset_env
    df = pd.DataFrame([
        _accept("1111111111", [2015], "https://e.com/exist", "Existing Doc"),  # update → deferred
        {"gate_decision": "hold", "npi": "3333333333", "defendant_name": "Shaky", "gate_reason": "fuzzy"},
    ])
    report = GoldSetBuilder()._merge_into_gold_set(df, dry_run=False)
    # no appends → gold_set unchanged, but the 2 items are queued for review
    assert report["written"] is False
    queue = tmp / "proposals" / "needs_review.jsonl"   # rolling queue
    assert queue.exists()
    kinds = sorted(json.loads(l)["kind"] for l in queue.read_text().splitlines() if l.strip())
    assert kinds == ["gate_hold", "update"]


def test_dry_run_writes_nothing(goldset_env):
    tmp, gold, _ = goldset_env
    before = gold.read_text()
    df = pd.DataFrame([_accept("2222222222", [2020], "https://e.com/new")])
    GoldSetBuilder()._merge_into_gold_set(df, dry_run=True)
    assert gold.read_text() == before
    assert not list(tmp.glob("gold_set.jsonl.bak_*"))
