"""Tests for pipeline.scrapers.gold_set_builder link-2 wiring (curator integration).

The curator itself (fraud-investigator-v2) is NOT imported — `_invoke_curator`
is patched — so these run in the bare pipeline env, mirroring how the builder
lazily imports the agent only when actually invoking it.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd

from pipeline.leads import Lead
from pipeline.scrapers.gold_set_builder import GoldSetBuilder


def _lead(url="https://www.justice.gov/opa/pr/x", text="A Medicare fraud scheme..."):
    return Lead(provenance="doj_direct", primary_source_url=url, raw_text=text)


def test_empty_leads_short_circuits_without_invoking_curator(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(GoldSetBuilder, "_invoke_curator",
                        staticmethod(lambda *a, **k: called.append((a, k))))
    out = GoldSetBuilder()._run_curator([], run_dir=tmp_path)
    assert out is None
    assert called == []                       # curator never invoked
    assert not (tmp_path / "leads.jsonl").exists()   # nothing written


def test_nonempty_leads_writes_file_and_invokes_curator(tmp_path, monkeypatch):
    seen = {}
    def fake_invoke(leads_path, run_dir):
        seen["leads_path"] = leads_path
        seen["run_dir"] = run_dir
        return "CURATOR_OUTPUT"
    monkeypatch.setattr(GoldSetBuilder, "_invoke_curator", staticmethod(fake_invoke))

    leads = [_lead(url="https://e.com/a"), _lead(url="https://e.com/b")]
    out = GoldSetBuilder()._run_curator(leads, run_dir=tmp_path)

    assert out == "CURATOR_OUTPUT"
    leads_path = tmp_path / "leads.jsonl"
    assert seen["leads_path"] == leads_path and seen["run_dir"] == tmp_path

    # the file is the curator's interface: one JSON Lead per line, raw_text intact
    rows = [json.loads(l) for l in leads_path.read_text().splitlines()]
    assert len(rows) == 2
    assert all(r["raw_text"] and r["provenance"] == "doj_direct" for r in rows)
    assert {r["primary_source_url"] for r in rows} == {"https://e.com/a", "https://e.com/b"}


def test_proposals_to_frame_handles_none_and_cases():
    assert GoldSetBuilder._proposals_to_frame(None).empty

    fake_out = SimpleNamespace(proposed_cases=[
        SimpleNamespace(model_dump=lambda: {"case_id": "DOJ-1", "defendant_name": "A"}),
        SimpleNamespace(model_dump=lambda: {"case_id": "DOJ-2", "defendant_name": "B"}),
    ])
    df = GoldSetBuilder._proposals_to_frame(fake_out)
    assert isinstance(df, pd.DataFrame) and len(df) == 2
    assert list(df["case_id"]) == ["DOJ-1", "DOJ-2"]


def test_new_run_dir_is_under_runs_dir(tmp_path, monkeypatch):
    import pipeline.scrapers.gold_set_builder as m
    monkeypatch.setattr(m, "RUNS_DIR", tmp_path / "runs")
    rd = GoldSetBuilder._new_run_dir()
    assert rd.exists() and rd.parent == tmp_path / "runs" and rd.name.startswith("goldset_")
