"""Tests for the historical gold_set backfill (cache_leads + run_backfill).

The curator (LLM) and the live gate tools (NPPES, panels) are stubbed — these
cover the deterministic plumbing the backfill adds: building leads from the full
warm cache, chunking them through the curator, aggregating proposals, and the
resume path. The gate's routing itself is covered by test_gate.py.
"""
from __future__ import annotations

import json

import pandas as pd

import pipeline.scrapers.doj_lead_adapter as adapter
from pipeline.scrapers.doj_lead_adapter import _epoch_to_iso, cache_leads
from pipeline.scrapers.gold_set_builder import GoldSetBuilder, write_diff_csv, write_review_csv


# ---------------------------------------------------------------------------
# cache_leads — full-corpus producer over the warm cache
# ---------------------------------------------------------------------------


def _write_cache(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records))


def test_epoch_to_iso():
    assert _epoch_to_iso("1231416000") == "2009-01-08"
    assert _epoch_to_iso(None) is None
    assert _epoch_to_iso("") is None
    assert _epoch_to_iso("not-a-number") is None


def test_cache_leads_gates_and_maps_fields(tmp_path, monkeypatch):
    cache = tmp_path / "raw.jsonl"
    _write_cache(cache, [
        {"url": "https://j.gov/a", "body": "<p>fraud body A</p>", "title": "Doctor charged",
         "date": "1231416000", "uuid": "u-a",
         "component": [{"uuid": "x", "name": "Criminal Division"}]},   # raw list-of-dicts shape
        {"url": "https://j.gov/b", "body": "<p>not relevant</p>", "title": "Tax case", "date": "1231416000"},
        {"url": "https://j.gov/c", "body": "", "title": "no body"},   # skipped (no text)
    ])
    # Gate: only the first body passes.
    monkeypatch.setattr(adapter, "passes_body_gate", lambda body: "fraud" in (body or "").lower())
    monkeypatch.setattr(adapter, "matched_title_terms", lambda title: ["charged"] if "charged" in (title or "") else [])

    leads = cache_leads(cache)
    assert len(leads) == 1
    lead = leads[0]
    assert lead.primary_source_url == "https://j.gov/a"
    assert lead.raw_text == "fraud body A"          # HTML stripped
    assert lead.published_date == "2009-01-08"      # epoch → ISO
    assert lead.provenance == "doj_direct"
    assert lead.discovery_meta["from_cache_backfill"] is True
    assert lead.discovery_meta["body_chars"] == len("fraud body A")
    # component flattened to a name string (matches the incremental path), not the raw list.
    assert lead.discovery_meta["component"] == "Criminal Division"


def test_cache_leads_ungated_keeps_all_with_bodies(tmp_path, monkeypatch):
    cache = tmp_path / "raw.jsonl"
    _write_cache(cache, [
        {"url": "https://j.gov/a", "body": "x", "title": "t", "date": "1231416000"},
        {"url": "https://j.gov/b", "body": "y", "title": "t", "date": "1231416000"},
        {"url": "https://j.gov/c", "body": "", "title": "t"},   # still skipped (no text)
    ])
    leads = cache_leads(cache, gated_only=False)
    assert {l.primary_source_url for l in leads} == {"https://j.gov/a", "https://j.gov/b"}


# ---------------------------------------------------------------------------
# run_backfill — chunked curator invocation → aggregate → gate → (dry) merge
# ---------------------------------------------------------------------------


class _FakeProposal:
    def __init__(self, d):
        self._d = d

    def model_dump(self):
        return dict(self._d)


class _FakeCuratorOut:
    def __init__(self, cases):
        self.proposed_cases = [_FakeProposal(c) for c in cases]


def _proposal(npi, years=(2021, 2022)):
    return {"defendant_name": "Doc", "defendant_npi_candidates": [npi],
            "all_active_years": list(years), "outcome_date": None,
            "active_years_source": "source_stated", "source_url": f"https://e.com/{npi}"}


def _stub_gate_tools(monkeypatch, gold_rows=()):
    """Stub the gate's live deps so transform() routes deterministically."""
    monkeypatch.setattr(GoldSetBuilder, "_load_gold_rows", staticmethod(lambda: list(gold_rows)))
    monkeypatch.setattr(GoldSetBuilder, "_verify_npi", staticmethod(lambda npi, first: {
        "exists": bool(npi), "entity_type": "NPI-1", "name_match": "exact",
        "enum_year": None, "nppes_specialty": None, "nppes_states": []}))
    import scripts.check_billing_surface as cbs
    monkeypatch.setattr(cbs, "load_surfaces", lambda base, years: ({}, set()))
    monkeypatch.setattr(cbs, "verdict_for", lambda npi, yrs, surf, np_: ("SURFACE_FOUND", {}, []))


def test_run_backfill_chunks_and_aggregates(tmp_path, monkeypatch):
    _stub_gate_tools(monkeypatch)
    # 5 leads, chunk_size 2 → 3 chunks (2,2,1). Each lead → one proposal with a
    # distinct NPI, so the gate ACCEPTs all 5 as new defendants.
    leads = [object()] * 5
    seen_chunk_sizes = []

    def fake_run_curator(self, chunk, *, run_dir):
        seen_chunk_sizes.append(len(chunk))
        base = len(seen_chunk_sizes) * 10
        return _FakeCuratorOut([_proposal(npi=str(1000000000 + base + i)) for i in range(len(chunk))])

    monkeypatch.setattr(GoldSetBuilder, "_run_curator", fake_run_curator)

    report, digest = GoldSetBuilder().run_backfill(
        dry_run=True, chunk_size=2, leads=leads, run_dir=tmp_path)

    assert seen_chunk_sizes == [2, 2, 1]                 # chunked correctly
    bf = report["backfill"]
    assert (bf["leads"], bf["offset"], bf["total"], bf["chunks"], bf["clean_room"]) == (5, 0, 5, 3, False)
    assert bf["next_offset"] == 5 and bf["done"] is True   # whole (injected) corpus processed
    assert report["counts"]["proposals"] == 5            # all chunks aggregated
    assert report["counts"]["accept"] == 5
    assert report["dry_run"] is True                     # nothing written
    # Each chunk persisted its proposals for resume.
    assert (tmp_path / "chunk_0" / "chunk_proposals.jsonl").exists()
    assert (tmp_path / "chunk_2" / "chunk_proposals.jsonl").exists()


def test_run_backfill_resume_skips_completed_chunks(tmp_path, monkeypatch):
    _stub_gate_tools(monkeypatch)
    # Pre-seed chunk_0 as already done; only chunk_1 should invoke the curator.
    (tmp_path / "chunk_0").mkdir(parents=True)
    (tmp_path / "chunk_0" / "chunk_proposals.jsonl").write_text(json.dumps(_proposal("1000000001")))

    invoked = []

    def fake_run_curator(self, chunk, *, run_dir):
        invoked.append(run_dir.name)
        return _FakeCuratorOut([_proposal("2000000002")])

    monkeypatch.setattr(GoldSetBuilder, "_run_curator", fake_run_curator)

    report, _ = GoldSetBuilder().run_backfill(
        dry_run=True, chunk_size=1, leads=[object(), object()],
        run_dir=tmp_path, resume=True)

    assert invoked == ["chunk_1"]                        # chunk_0 resumed from cache
    assert report["counts"]["proposals"] == 2            # both still aggregated


def test_run_backfill_limit_caps_leads(tmp_path, monkeypatch):
    _stub_gate_tools(monkeypatch)
    monkeypatch.setattr(GoldSetBuilder, "_run_curator",
                        lambda self, chunk, *, run_dir: _FakeCuratorOut([_proposal("1000000001")]))
    report, _ = GoldSetBuilder().run_backfill(
        dry_run=True, chunk_size=10, leads=[object()] * 50, limit=3, run_dir=tmp_path)
    assert report["backfill"]["leads"] == 3
    assert report["backfill"]["total"] == 50


def test_run_backfill_parallel_workers_aggregate_all(tmp_path, monkeypatch):
    _stub_gate_tools(monkeypatch)
    # 12 leads, chunk_size 1, 4 workers → 12 chunks run concurrently; all must aggregate.
    monkeypatch.setattr(
        GoldSetBuilder, "_run_curator",
        lambda self, chunk, *, run_dir: _FakeCuratorOut([_proposal(npi=run_dir.name.replace("chunk_", "100000000"))]))
    report, _ = GoldSetBuilder().run_backfill(
        dry_run=True, chunk_size=1, workers=4, leads=[object()] * 12, run_dir=tmp_path)
    assert report["counts"]["proposals"] == 12
    assert report["backfill"]["workers"] == 4


# ---------------------------------------------------------------------------
# clean-room mode — fresh build into a separate file, canonical untouched
# ---------------------------------------------------------------------------


def test_clean_room_dedupes_by_npi_and_writes_separate_file(tmp_path, monkeypatch):
    # canonical has one NPI; clean-room must NOT dedupe against it (treats empty).
    canonical = [{"npi": "5000000005", "all_active_years": [2015], "case_id": "DOJ-x"}]
    _stub_gate_tools(monkeypatch, gold_rows=canonical)
    out = tmp_path / "gold_set_backfill.jsonl"

    # Same NPI proposed twice with different years (roundup + individual release) → one row, unioned.
    chunks_cases = [[_proposal("9000000001", years=(2018, 2019))],
                    [_proposal("9000000001", years=(2019, 2020))],
                    [_proposal("9000000002", years=(2021,))]]
    seq = iter(chunks_cases)
    monkeypatch.setattr(GoldSetBuilder, "_run_curator",
                        lambda self, chunk, *, run_dir: _FakeCuratorOut(next(seq)))

    report, digest = GoldSetBuilder().run_backfill(
        dry_run=False, chunk_size=1, clean_room=True, out_path=out,
        leads=[object()] * 3, run_dir=tmp_path)

    rows = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
    assert {r["npi"] for r in rows} == {"9000000001", "9000000002"}        # deduped by NPI
    row1 = next(r for r in rows if r["npi"] == "9000000001")
    assert row1["all_active_years"] == [2018, 2019, 2020]                  # years unioned
    assert report["clean_room"] is True and report["counts"]["unique_npis"] == 2
    # diff vs canonical: the canonical NPI was not re-derived; both built NPIs are net-new.
    assert report["diff_vs_canonical"]["missing"] == 1
    assert report["diff_vs_canonical"]["new"] == 2
    assert "RELIABILITY AUDIT" in digest
    # The diff is written as its own audit CSV (the email attachment), with a row per
    # diff item tagged by category — full detail, not just the summary counts.
    diff_rows = (tmp_path / "diff_vs_canonical.csv").read_text().splitlines()
    assert diff_rows[0].startswith("category,defendant_name,npi")
    cats = [r.split(",")[0] for r in diff_rows[1:]]
    assert cats.count("net_new") == 2 and cats.count("not_re_derived") == 1
    assert "5000000005" in (tmp_path / "diff_vs_canonical.csv").read_text()   # the not-re-derived canonical NPI


def test_write_diff_csv_categories_and_year_formatting(tmp_path):
    detail = [
        {"category": "year_disagreement", "npi": "9000000001", "defendant_name": "Doc A",
         "canonical_years": [2014, 2015], "rebuilt_years": [2014, 2015, 2016], "source_url": "u"},
        {"category": "not_re_derived", "npi": "9000000002", "defendant_name": "Doc B",
         "canonical_years": [2012], "rebuilt_years": "", "source_url": "v"},
    ]
    path = write_diff_csv(detail, tmp_path / "diff.csv")
    text = path.read_text()
    assert text.splitlines()[0].startswith("category,defendant_name,npi")
    assert "year_disagreement" in text and "2014, 2015, 2016" in text   # list years rendered
    assert write_diff_csv([], tmp_path / "empty.csv").read_text().strip().startswith("category,")


def test_clean_room_does_not_touch_canonical(tmp_path, monkeypatch):
    _stub_gate_tools(monkeypatch)
    # If clean-room ever called the canonical drift-guarded writer, this would blow up.
    monkeypatch.setattr(GoldSetBuilder, "_drift_guarded_append",
                        staticmethod(lambda *a, **k: (_ for _ in ()).throw(AssertionError("touched canonical!"))))
    monkeypatch.setattr(GoldSetBuilder, "_run_curator",
                        lambda self, chunk, *, run_dir: _FakeCuratorOut([_proposal("9000000001")]))
    report, _ = GoldSetBuilder().run_backfill(
        dry_run=False, chunk_size=1, clean_room=True, out_path=tmp_path / "bf.jsonl",
        leads=[object()], run_dir=tmp_path)
    assert report["written"] is True


def test_write_review_csv_has_header_and_action(tmp_path):
    items = [{"kind": "gate_hold", "defendant_name": "Doc", "npi": "9000000001",
              "all_active_years": [2019, 2020], "reason": "name match is 'fuzzy' (not exact/prefix)"}]
    path = write_review_csv(items, tmp_path / "r.csv")
    text = path.read_text()
    assert text.splitlines()[0].startswith("kind,defendant_name,npi")
    assert "9000000001" in text and "2019, 2020" in text
    assert "find/confirm the right NPI" in text          # derived action column
    # empty items → header-only file still written (workflow attaches unconditionally)
    empty = write_review_csv([], tmp_path / "empty.csv")
    assert empty.read_text().strip().startswith("kind,")


def test_curate_chunks_stops_at_deadline_and_reports_prefix(tmp_path, monkeypatch):
    # A deadline already in the past → no waves run → completed prefix is 0.
    import time
    b = GoldSetBuilder()
    monkeypatch.setattr(GoldSetBuilder, "_run_curator",
                        lambda self, chunk, *, run_dir: _FakeCuratorOut([]))
    chunks = [[object()], [object()], [object()]]
    cases, completed, curated = b._curate_chunks(
        chunks, run_dir=tmp_path, workers=1, resume=False, deadline=time.monotonic() - 1)
    assert completed == 0 and cases == [] and curated == []   # checkpointed before any work


def test_run_backfill_deadline_checkpoint_sets_next_offset(tmp_path, monkeypatch):
    _stub_gate_tools(monkeypatch)
    monkeypatch.setattr(GoldSetBuilder, "_run_curator",
                        lambda self, chunk, *, run_dir: _FakeCuratorOut([_proposal("1000000001")]))
    # Negative budget → deadline in the past → 0 chunks processed → resumes at same offset.
    report, _ = GoldSetBuilder().run_backfill(
        dry_run=True, chunk_size=1, deadline_minutes=-1, offset=4,
        leads=[object()] * 10, run_dir=tmp_path)
    bf = report["backfill"]
    assert bf["completed_chunks"] == 0 and bf["next_offset"] == 4 and bf["done"] is False


def test_clean_room_accumulates_across_waves(tmp_path, monkeypatch):
    _stub_gate_tools(monkeypatch)
    out = tmp_path / "gold_set_backfill.jsonl"
    # Wave 1 finds NPI ...01; wave 2 finds ...02 → file must contain BOTH (accumulate).
    monkeypatch.setattr(GoldSetBuilder, "_run_curator",
                        lambda self, chunk, *, run_dir: _FakeCuratorOut([_proposal("9000000001")]))
    GoldSetBuilder().run_backfill(dry_run=False, chunk_size=1, clean_room=True, out_path=out,
                                  leads=[object()], run_dir=tmp_path / "w1")
    monkeypatch.setattr(GoldSetBuilder, "_run_curator",
                        lambda self, chunk, *, run_dir: _FakeCuratorOut([_proposal("9000000002")]))
    report, _ = GoldSetBuilder().run_backfill(dry_run=False, chunk_size=1, clean_room=True, out_path=out,
                                              leads=[object()], run_dir=tmp_path / "w2")
    npis = {json.loads(l)["npi"] for l in out.read_text().splitlines() if l.strip()}
    assert npis == {"9000000001", "9000000002"}          # wave 2 grew the file, didn't clobber it
    assert report["counts"]["carried_in"] == 1 and report["counts"]["new_this_wave"] == 1


def test_run_backfill_isolates_a_failing_chunk(tmp_path, monkeypatch):
    _stub_gate_tools(monkeypatch)
    # chunk_1's curator raises; the sweep must still complete with the other two,
    # and the failed chunk must NOT be cached (so --resume retries only it).
    def fake_run_curator(self, chunk, *, run_dir):
        if run_dir.name == "chunk_1":
            raise RuntimeError("simulated curator timeout")
        return _FakeCuratorOut([_proposal(npi="100000000" + run_dir.name[-1])])

    monkeypatch.setattr(GoldSetBuilder, "_run_curator", fake_run_curator)
    report, _ = GoldSetBuilder().run_backfill(
        dry_run=True, chunk_size=1, leads=[object()] * 3, run_dir=tmp_path)

    assert report["counts"]["proposals"] == 2                       # 2 survived, 1 failed
    assert not (tmp_path / "chunk_1" / "chunk_proposals.jsonl").exists()   # failed chunk not cached
    assert (tmp_path / "chunk_0" / "chunk_proposals.jsonl").exists()       # successful ones are


def test_run_backfill_offset_selects_a_nonoverlapping_window(tmp_path, monkeypatch):
    _stub_gate_tools(monkeypatch)
    # Distinct lead ids so we can prove the offset window is the one processed.
    leads = list(range(20))
    processed = []

    def fake_run_curator(self, chunk, *, run_dir):
        processed.extend(chunk)
        return _FakeCuratorOut([])   # proposals don't matter here

    monkeypatch.setattr(GoldSetBuilder, "_run_curator", fake_run_curator)
    report, _ = GoldSetBuilder().run_backfill(
        dry_run=True, chunk_size=5, leads=leads, offset=10, limit=5, run_dir=tmp_path)

    assert processed == [10, 11, 12, 13, 14]             # the offset window, not the front
    bf = report["backfill"]
    assert (bf["leads"], bf["offset"], bf["total"], bf["chunks"]) == (5, 10, 20, 1)
    assert bf["next_offset"] == 15 and bf["done"] is False   # 5 of 20 leads done; more remain
