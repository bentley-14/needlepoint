"""target_sweep.py — productionized anchor-gated sweep (T3c).

Smoke tests with mocked duckdb connections (no real R2/network, no real
walk-forward panel) plus pure-function unit tests for the new CLI surface
(--ranks / --skip-npis-file) and the batched top-codes query (the throughput
fix — one IN-list query per batch, not one per NPI).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.target_sweep import (
    fetch_top_codes_batch,
    find_panel,
    load_skip_npis,
    parse_rank_range,
    process_candidate,
    run_sweep,
)


# ── parse_rank_range ─────────────────────────────────────────────────────────

def test_parse_rank_range_valid():
    assert parse_rank_range("16-120") == (16, 120)
    assert parse_rank_range(" 1-1 ") == (1, 1)


@pytest.mark.parametrize("bad", ["bad", "16", "16-", "-120", "120-16", "0-5"])
def test_parse_rank_range_invalid(bad):
    with pytest.raises(ValueError):
        parse_rank_range(bad)


# ── load_skip_npis ───────────────────────────────────────────────────────────

def test_load_skip_npis_none_is_empty_set():
    assert load_skip_npis(None) == set()


def test_load_skip_npis_reads_file_with_comments_and_blanks(tmp_path):
    p = tmp_path / "skip.txt"
    p.write_text("# already done\n1900000032\n\n1900000017\n")
    assert load_skip_npis(p) == {"1900000032", "1900000017"}


def test_load_skip_npis_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_skip_npis(tmp_path / "nope.txt")


# ── find_panel ───────────────────────────────────────────────────────────────

def test_find_panel_raises_clear_error_when_absent(tmp_path):
    with pytest.raises(FileNotFoundError, match="no scored panel found"):
        find_panel(tmp_path, 2024)


def test_find_panel_finds_the_glob_match(tmp_path):
    d = tmp_path / "results" / "walk_forward_v4_top5_y2024_extras-True"
    d.mkdir(parents=True)
    p = d / "scored_full_2024.parquet"
    p.write_text("")  # existence is all find_panel checks
    assert find_panel(tmp_path, 2024) == p


# ── fetch_top_codes_batch — the T3c throughput fix ──────────────────────────

class _FakeR2Con:
    """Records every call (so tests can assert ONE query per batch, not one
    per NPI) and returns canned (npi, code, pay) rows regardless of the
    exact SQL text."""

    def __init__(self, rows: list[tuple]):
        self._rows = rows
        self.calls: list[str] = []

    def execute(self, sql, *a, **k):
        self.calls.append(sql)
        return self

    def fetchall(self):
        return self._rows


def test_fetch_top_codes_batch_issues_one_query_for_the_whole_batch():
    rows = [
        ("1111111111", "99214", 500.0), ("1111111111", "87798", 9000.0),
        ("1111111111", "11721", 20.0),
        ("2222222222", "99213", 300.0),
    ]
    con = _FakeR2Con(rows)
    out = fetch_top_codes_batch(con, ["1111111111", "2222222222"], 2024, top_n=2)
    assert len(con.calls) == 1                 # ONE round-trip for both NPIs
    assert "IN (" in con.calls[0]
    assert "1111111111" in con.calls[0] and "2222222222" in con.calls[0]
    # top-2 by payment, sorted descending
    assert out["1111111111"] == [{"code": "87798", "pay": 9000.0}, {"code": "99214", "pay": 500.0}]
    assert out["2222222222"] == [{"code": "99213", "pay": 300.0}]


def test_fetch_top_codes_batch_empty_npi_list_makes_no_query():
    con = _FakeR2Con([])
    assert fetch_top_codes_batch(con, [], 2024) == {}
    assert con.calls == []


def test_fetch_top_codes_batch_filters_malformed_npis():
    con = _FakeR2Con([("1111111111", "99214", 100.0)])
    out = fetch_top_codes_batch(con, ["1111111111", "not-an-npi", "12345"], 2024)
    assert len(con.calls) == 1
    assert "not-an-npi" not in con.calls[0] and "12345" not in con.calls[0]
    assert "1111111111" in out


# ── process_candidate ────────────────────────────────────────────────────────

def test_process_candidate_assembles_record_and_writes_ring_cache(tmp_path):
    cand = {"rank": 5, "npi": "1111111111", "name": "TESTSON TED", "sp": "Podiatry", "st": "MO"}
    top_codes = [{"code": "87798", "pay": 9000.0}]

    def fake_enrich(npi):
        assert npi == "1111111111"
        return {"collectible_entity": {"name": "Test Clinic LLC", "kind": "org"},
                "novelty": {"verdict": "NOVEL — no public case"},
                "ring_flag": True, "n_anomalous": 3}

    def fake_anchor(code):
        assert code == "87798"
        return {"lcd_exists": True, "ncd_exists": False}

    rec = process_candidate(cand, top_codes, tmp_path, enrich_fn=fake_enrich, anchor_fn=fake_anchor)
    assert rec["npi"] == "1111111111" and rec["rank"] == 5
    assert rec["entity"] == "Test Clinic LLC" and rec["kind"] == "org"
    assert rec["ring_flag"] is True and rec["n_anom"] == 3
    assert rec["novelty"] == "NOVEL"
    assert rec["anchor"]["lcd_exists"] is True
    cache_file = tmp_path / "1111111111.json"
    assert cache_file.exists()
    assert json.loads(cache_file.read_text())["ring_flag"] is True


def test_process_candidate_gov_known_novelty():
    cand = {"rank": 1, "npi": "1111111111", "name": "X", "sp": "Y", "st": "Z"}

    def fake_enrich(npi):
        return {"collectible_entity": {}, "novelty": {"verdict": "GOV-KNOWN — charged 2024"},
                "ring_flag": False, "n_anomalous": 0}

    rec = process_candidate(cand, [], Path("/tmp"), enrich_fn=fake_enrich, anchor_fn=lambda c: {})
    assert rec["novelty"] == "GOV-KNOWN"


def test_process_candidate_error_is_captured_not_raised(tmp_path):
    cand = {"rank": 1, "npi": "1111111111", "name": "X", "sp": "Y", "st": "Z"}

    def exploding_enrich(npi):
        raise RuntimeError("boom")

    rec = process_candidate(cand, [{"code": "87798", "pay": 1.0}], tmp_path,
                            enrich_fn=exploding_enrich, anchor_fn=lambda c: {})
    assert "_error" in rec and "boom" in rec["_error"]


# ── run_sweep — full funnel, mocked duckdb (the required smoke test) ────────

class _FakeLocalCon:
    """Fake local duckdb connection for candidate selection: records the SQL
    and returns a canned DataFrame regardless of the exact query text."""

    def __init__(self, df: pd.DataFrame):
        self._df = df
        self.queries: list[str] = []

    def execute(self, sql, *a, **k):
        self.queries.append(sql)
        return self

    def fetchdf(self):
        return self._df


def test_run_sweep_end_to_end_with_mocked_duckdb(tmp_path):
    # A dummy panel file — find_panel only checks existence via glob; the
    # fake local connection below never actually reads its content.
    panel_dir = tmp_path / "results" / "walk_forward_v4_top5_y2024_extras-True"
    panel_dir.mkdir(parents=True)
    (panel_dir / "scored_full_2024.parquet").write_text("")

    cands_df = pd.DataFrame([
        {"npi": "1111111111", "nm": "ALPHA ONE", "sp": "Podiatry", "st": "MO", "skin": 0.0, "rnk": 16},
        {"npi": "2222222222", "nm": "BETA TWO", "sp": "Cardiology", "st": "CA", "skin": 0.0, "rnk": 17},
        {"npi": "3333333333", "nm": "GAMMA THREE (skipped)", "sp": "Podiatry", "st": "TX", "skin": 0.0, "rnk": 18},
    ])
    con_local = _FakeLocalCon(cands_df)
    r2_rows = [
        ("1111111111", "87798", 9000.0), ("1111111111", "99214", 500.0),
        ("2222222222", "99213", 300.0),
    ]
    con_r2 = _FakeR2Con(r2_rows)

    calls = {"enrich": [], "anchor": []}

    def fake_enrich(npi):
        calls["enrich"].append(npi)
        return {"collectible_entity": {"name": f"Entity-{npi}", "kind": "org"},
                "novelty": {"verdict": "NOVEL"}, "ring_flag": npi == "1111111111",
                "n_anomalous": 2}

    def fake_anchor(code):
        calls["anchor"].append(code)
        return {"lcd_exists": code == "87798", "ncd_exists": False}

    output_path = tmp_path / "out" / "sweep.jsonl"
    ring_cache_dir = tmp_path / "state" / "ring_cache"

    summary = run_sweep(
        root=tmp_path, year=2024, rank_lo=16, rank_hi=120,
        skip_npis={"3333333333"}, output_path=output_path, ring_cache_dir=ring_cache_dir,
        con_local=con_local, con_r2=con_r2, enrich_fn=fake_enrich, anchor_fn=fake_anchor,
        print_progress=False,
    )

    # 3 candidates selected, 1 skipped -> 2 processed
    assert summary["n_candidates"] == 3
    assert summary["n_skipped"] == 1
    assert summary["n_written"] == 2

    # ONE R2 query for the whole batch (both NPIs), not one per NPI —
    # the actual T3c throughput assertion.
    assert len(con_r2.calls) == 1

    # skipped NPI never reached enrich_ring/anchor_check at all
    assert "3333333333" not in calls["enrich"]

    lines = output_path.read_text().splitlines()
    assert len(lines) == 2
    recs = [json.loads(line) for line in lines]
    by_npi = {r["npi"]: r for r in recs}
    assert by_npi["1111111111"]["top_codes"][0]["code"] == "87798"
    assert by_npi["1111111111"]["ring_flag"] is True
    assert by_npi["2222222222"]["top_codes"][0]["code"] == "99213"

    assert (ring_cache_dir / "1111111111.json").exists()
    assert (ring_cache_dir / "2222222222.json").exists()
    assert not (ring_cache_dir / "3333333333.json").exists()

    # Candidate-selection query carries the requested rank bounds.
    assert "BETWEEN 16 AND 120" in con_local.queries[0]


def test_run_sweep_batches_at_the_configured_batch_size(tmp_path):
    """3 candidates with --batch-size 2 -> 2 R2 queries (batch of 2, then 1),
    not 1 per candidate and not 1 for everything."""
    panel_dir = tmp_path / "results" / "walk_forward_v4_top5_y2024_x"
    panel_dir.mkdir(parents=True)
    (panel_dir / "scored_full_2024.parquet").write_text("")

    cands_df = pd.DataFrame([
        # NPIs must start with 1 or 2 (fetch_top_codes_batch's validity filter)
        {"npi": f"1{i:09d}", "nm": f"P{i}", "sp": "Podiatry", "st": "MO", "skin": 0.0, "rnk": i}
        for i in range(1, 4)
    ])
    con_local = _FakeLocalCon(cands_df)
    con_r2 = _FakeR2Con([])  # no codes needed for this assertion

    run_sweep(
        root=tmp_path, year=2024, rank_lo=1, rank_hi=10, skip_npis=set(),
        output_path=tmp_path / "out.jsonl", ring_cache_dir=tmp_path / "ring_cache",
        batch_size=2, con_local=con_local, con_r2=con_r2,
        enrich_fn=lambda npi: {}, anchor_fn=lambda c: {}, print_progress=False,
    )
    assert len(con_r2.calls) == 2  # ceil(3 / 2)
