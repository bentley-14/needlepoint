"""Cross-run curated-lead ledger + per-run cap (run_once spend throttle)."""
from __future__ import annotations

import pipeline.scrapers.gold_set_builder as M
from pipeline.leads import Lead
from pipeline.scrapers.curated_ledger import append_ledger, load_ledger


def _lead(i: int) -> Lead:
    return Lead(provenance="doj_direct",
                primary_source_url=f"https://www.justice.gov/usao/pr/case-{i}",
                raw_text="body", title=f"case {i}")


def test_ledger_roundtrip_and_dedup(tmp_path):
    p = tmp_path / "curated_ledger.jsonl"
    assert load_ledger(p) == set()                       # greenfield → empty
    assert append_ledger([("a", "u1"), ("b", "u2")], p) == 2
    assert load_ledger(p) == {"a", "b"}
    assert append_ledger([("a", "u1"), ("c", "u3")], p) == 1   # 'a' already present
    assert load_ledger(p) == {"a", "b", "c"}


def _stub_builder(monkeypatch, leads, *, fail_after=None):
    """Patch run_once's collaborators; curate all leads, optionally failing chunks
    at/after index ``fail_after`` (their leads are NOT marked curated)."""
    monkeypatch.setattr(M, "doj_leads", lambda: list(leads))
    monkeypatch.setattr(M, "takedown_leads", lambda: [])   # isolate from the live takedown worklist
    monkeypatch.setattr(M, "format_run_digest", lambda report, proposals: "digest")
    monkeypatch.setattr(M.GoldSetBuilder, "transform", lambda self, df, **k: df)
    monkeypatch.setattr(M.GoldSetBuilder, "_merge_into_gold_set",
                        lambda self, df, *, dry_run, run_dir: {"append": [], "counts": {}})
    captured = {}

    def fake_curate(self, chunks, *, run_dir, workers, resume):
        captured["curated_n"] = sum(len(c) for c in chunks)
        curated = []
        for ci, chunk in enumerate(chunks):
            if fail_after is not None and ci >= fail_after:
                continue                                  # failed chunk → not marked
            curated.extend((ld.lead_id, ld.primary_source_url) for ld in chunk)
        return [], len(chunks), curated

    monkeypatch.setattr(M.GoldSetBuilder, "_curate_chunks", fake_curate)
    return captured


def test_run_once_caps_then_drains_via_ledger(tmp_path, monkeypatch):
    leads = [_lead(i) for i in range(20)]
    cap = _stub_builder(monkeypatch, leads)
    led = tmp_path / "led.jsonl"

    # Run 1: cap=8 → curate 8, ledger gains 8.
    M.GoldSetBuilder().run_once(dry_run=False, max_new=8, chunk_size=8,
                                ledger_path=led, run_dir=tmp_path / "r1")
    assert cap["curated_n"] == 8
    assert len(load_ledger(led)) == 8

    # Run 2: those 8 are filtered out before the curator → next 8 processed.
    M.GoldSetBuilder().run_once(dry_run=False, max_new=8, chunk_size=8,
                                ledger_path=led, run_dir=tmp_path / "r2")
    assert cap["curated_n"] == 8
    assert len(load_ledger(led)) == 16

    # Run 3: only 4 remain.
    M.GoldSetBuilder().run_once(dry_run=False, max_new=8, chunk_size=8,
                                ledger_path=led, run_dir=tmp_path / "r3")
    assert cap["curated_n"] == 4
    assert len(load_ledger(led)) == 20


def test_dry_run_does_not_advance_ledger(tmp_path, monkeypatch):
    _stub_builder(monkeypatch, [_lead(i) for i in range(5)])
    led = tmp_path / "led.jsonl"
    M.GoldSetBuilder().run_once(dry_run=True, max_new=8, chunk_size=8,
                                ledger_path=led, run_dir=tmp_path / "r")
    assert load_ledger(led) == set()                     # testing must not mutate shared state


def test_failed_chunk_leads_are_not_marked(tmp_path, monkeypatch):
    # 16 leads → 2 chunks of 8; the 2nd "fails" (e.g. spend limit) → its 8 stay unmarked.
    cap = _stub_builder(monkeypatch, [_lead(i) for i in range(16)], fail_after=1)
    led = tmp_path / "led.jsonl"
    M.GoldSetBuilder().run_once(dry_run=False, max_new=16, chunk_size=8,
                                ledger_path=led, run_dir=tmp_path / "r")
    assert len(load_ledger(led)) == 8                    # only the completed chunk → retried next run
