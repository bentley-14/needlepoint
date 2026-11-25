"""Unit tests for fraudlib/ranker/r2_working_set.py.

All rclone calls and filesystem side-effects are mocked — no real network,
no real pulls. Tests verify:
  1. manifest year-union math
  2. stage skips pre-existing files + records only fetched files
  3. purge deletes only staged files (leaves pre-existing untouched)
  4. orphan sweep removes dead-pid manifests

Run: pytest tests/test_r2_working_set.py -v
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fraudlib.ranker.r2_working_set as ws


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_manifest_path(cache_dir: Path, run_id: str) -> Path:
    return cache_dir / f".np_staged_{run_id}.json"


# ── 1. manifest year-union math ───────────────────────────────────────────────

def test_manifest_single_year_2022(tmp_path, monkeypatch):
    """Single score year 2022 → panels for 2013..2022 (10 items)."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))
    items = ws.manifest(score_years=[2022], attach_extras=False)
    years_in = {int(item.r2_key.split("features_full_")[1].replace(".parquet", ""))
                for item in items}
    assert years_in == set(range(2013, 2023)), f"Expected 2013..2022, got {sorted(years_in)}"
    assert len(items) == 10


def test_manifest_sweep_2015_to_2023(tmp_path, monkeypatch):
    """Sweep 2015-2023 → union = 2013..2023 (11 items, not per-year-only)."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))
    items = ws.manifest(score_years=list(range(2015, 2024)), attach_extras=False)
    years_in = {int(item.r2_key.split("features_full_")[1].replace(".parquet", ""))
                for item in items}
    assert years_in == set(range(2013, 2024)), f"Expected 2013..2023, got {sorted(years_in)}"
    assert len(items) == 11


def test_manifest_attach_extras_adds_satellites(tmp_path, monkeypatch):
    """attach_extras=True adds hcpcs_rich + 3 satellite families per year."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))
    base_items = ws.manifest(score_years=[2022], attach_extras=False)
    extra_items = ws.manifest(score_years=[2022], attach_extras=True)
    # extras must be strictly more than base
    assert len(extra_items) > len(base_items)
    # Each year should have hcpcs_rich + dmepos + openpayments + claim_level added
    extra_r2_keys = {i.r2_key for i in extra_items} - {i.r2_key for i in base_items}
    satellite_prefixes = ("hcpcs_rich_", "dmepos_", "openpayments_", "claim_level_")
    for prefix in satellite_prefixes:
        assert any(prefix in k for k in extra_r2_keys), (
            f"Expected satellite prefix '{prefix}' in extra keys"
        )


def test_manifest_empty_score_years(tmp_path, monkeypatch):
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))
    assert ws.manifest(score_years=[], attach_extras=False) == []


def test_manifest_train_start_respected(tmp_path, monkeypatch):
    """Custom train_start shifts the lower bound."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))
    items = ws.manifest(score_years=[2020], attach_extras=False, train_start=2018)
    years_in = {int(item.r2_key.split("features_full_")[1].replace(".parquet", ""))
                for item in items}
    assert years_in == {2018, 2019, 2020}


def test_manifest_local_paths_use_cache_dir(tmp_path, monkeypatch):
    """local_path for features_full is under the NP_CACHE_DIR."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))
    items = ws.manifest(score_years=[2015], attach_extras=False)
    for item in items:
        assert str(item.local_path).startswith(str(tmp_path))


# ── 2. stage ─────────────────────────────────────────────────────────────────

def test_stage_skips_preexisting_files(tmp_path, monkeypatch):
    """Files that already exist locally are not passed to r2_copy."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("NP_KEEP_CACHE", "0")

    # Pre-create one of the two items
    existing_file = tmp_path / "features_full_2013.parquet"
    existing_file.write_bytes(b"existing")
    missing_file = tmp_path / "features_full_2014.parquet"

    items = [
        ws.WorkItem(r2_key="r2:needlepoint-data/cache/v1/feature_frames/features_full_2013.parquet",
                    local_path=existing_file),
        ws.WorkItem(r2_key="r2:needlepoint-data/cache/v1/feature_frames/features_full_2014.parquet",
                    local_path=missing_file),
    ]

    copied = []

    def fake_r2_copy(r2_key, local_dest):
        Path(local_dest).parent.mkdir(parents=True, exist_ok=True)
        Path(local_dest).write_bytes(b"from_r2")
        copied.append(r2_key)

    with patch("fraudlib.ranker.r2_working_set.r2_copy", side_effect=fake_r2_copy):
        run_id, staged = ws.stage(items, run_id="test-run-1")

    # Only the missing file should have been pulled
    assert len(copied) == 1
    assert "features_full_2014" in copied[0]
    assert len(staged) == 1
    assert staged[0] == missing_file


def test_stage_records_only_fetched_files(tmp_path, monkeypatch):
    """The staged manifest JSON records only newly fetched paths."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))

    existing = tmp_path / "already_there.parquet"
    existing.write_bytes(b"x")
    missing = tmp_path / "needs_pull.parquet"

    items = [
        ws.WorkItem(r2_key="r2:bucket/already_there.parquet", local_path=existing),
        ws.WorkItem(r2_key="r2:bucket/needs_pull.parquet", local_path=missing),
    ]

    def fake_r2_copy(r2_key, local_dest):
        Path(local_dest).write_bytes(b"pulled")

    with patch("fraudlib.ranker.r2_working_set.r2_copy", side_effect=fake_r2_copy):
        run_id, _ = ws.stage(items, run_id="test-run-2")

    mf_path = tmp_path / f".np_staged_test-run-2.json"
    assert mf_path.exists()
    data = json.loads(mf_path.read_text())
    assert data["run_id"] == "test-run-2"
    assert len(data["staged"]) == 1
    assert "needs_pull" in data["staged"][0]


def test_stage_all_preexisting_no_r2_calls(tmp_path, monkeypatch):
    """When all files exist, r2_copy is never called."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))

    f = tmp_path / "f.parquet"
    f.write_bytes(b"x")
    items = [ws.WorkItem(r2_key="r2:bucket/f.parquet", local_path=f)]

    mock_copy = MagicMock()
    with patch("fraudlib.ranker.r2_working_set.r2_copy", mock_copy):
        run_id, staged = ws.stage(items, run_id="test-run-3")

    mock_copy.assert_not_called()
    assert staged == []
    data = json.loads((tmp_path / ".np_staged_test-run-3.json").read_text())
    assert data["staged"] == []


# ── 3. purge ──────────────────────────────────────────────────────────────────

def test_purge_deletes_only_staged_files(tmp_path, monkeypatch):
    """purge() removes staged files but leaves a pre-existing file untouched."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("NP_KEEP_CACHE", raising=False)

    staged_file = tmp_path / "staged.parquet"
    staged_file.write_bytes(b"staged")
    preexisting_file = tmp_path / "preexisting.parquet"
    preexisting_file.write_bytes(b"preexisting")

    # Write a manifest that records only the staged file
    run_id = "purge-test-run"
    mf = tmp_path / f".np_staged_{run_id}.json"
    mf.write_text(json.dumps({
        "run_id": run_id,
        "pid": os.getpid(),
        "staged": [str(staged_file)],
    }))

    ws.purge(run_id)

    assert not staged_file.exists(), "staged file should have been deleted"
    assert preexisting_file.exists(), "pre-existing file must NOT be deleted"
    assert not mf.exists(), "manifest file should be removed after purge"


def test_purge_respects_np_keep_cache(tmp_path, monkeypatch):
    """NP_KEEP_CACHE=1 causes purge to skip deletion entirely."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("NP_KEEP_CACHE", "1")

    f = tmp_path / "should_stay.parquet"
    f.write_bytes(b"x")

    run_id = "keep-cache-run"
    mf = tmp_path / f".np_staged_{run_id}.json"
    mf.write_text(json.dumps({"run_id": run_id, "pid": os.getpid(), "staged": [str(f)]}))

    ws.purge(run_id)

    assert f.exists(), "file should remain when NP_KEEP_CACHE=1"
    assert mf.exists(), "manifest should remain when NP_KEEP_CACHE=1"


def test_purge_nonexistent_run_id_is_noop(tmp_path, monkeypatch):
    """purge() with a run_id that has no manifest is a no-op (no error)."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("NP_KEEP_CACHE", raising=False)
    ws.purge("run-that-never-happened")  # must not raise


# ── 4. orphan sweep ───────────────────────────────────────────────────────────

def test_sweep_orphans_removes_dead_pid_manifest(tmp_path, monkeypatch):
    """sweep_orphans() purges manifests whose originating pid is dead."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("NP_KEEP_CACHE", raising=False)

    orphan_file = tmp_path / "orphan.parquet"
    orphan_file.write_bytes(b"orphan")

    # Use a pid that is definitely dead (pid 1 exists but isn't ours; use a huge int)
    dead_pid = 2**30  # unreachable pid
    run_id = "dead-run-99"
    mf = tmp_path / f".np_staged_{run_id}.json"
    mf.write_text(json.dumps({
        "run_id": run_id,
        "pid": dead_pid,
        "staged": [str(orphan_file)],
    }))

    # Patch _pid_alive so dead_pid returns False
    with patch.object(ws, "_pid_alive", return_value=False):
        ws.sweep_orphans()

    assert not orphan_file.exists(), "orphan file should be purged"
    assert not mf.exists(), "orphan manifest should be removed"


def test_sweep_orphans_leaves_live_pid_manifest(tmp_path, monkeypatch):
    """sweep_orphans() does NOT purge manifests whose originating pid is alive."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("NP_KEEP_CACHE", raising=False)

    live_file = tmp_path / "live.parquet"
    live_file.write_bytes(b"live")

    run_id = "live-run-1"
    mf = tmp_path / f".np_staged_{run_id}.json"
    mf.write_text(json.dumps({
        "run_id": run_id,
        "pid": os.getpid(),  # current process — definitely alive
        "staged": [str(live_file)],
    }))

    ws.sweep_orphans()

    assert live_file.exists(), "live file must not be swept"
    assert mf.exists(), "live manifest must not be swept"


def test_sweep_orphans_noop_when_keep_cache(tmp_path, monkeypatch):
    """sweep_orphans() is a no-op when NP_KEEP_CACHE=1."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("NP_KEEP_CACHE", "1")

    f = tmp_path / "keep.parquet"
    f.write_bytes(b"x")
    run_id = "keep-run"
    mf = tmp_path / f".np_staged_{run_id}.json"
    mf.write_text(json.dumps({"run_id": run_id, "pid": 99999999, "staged": [str(f)]}))

    with patch.object(ws, "_pid_alive", return_value=False):
        ws.sweep_orphans()

    assert f.exists(), "NP_KEEP_CACHE=1 must prevent sweep_orphans from deleting"
    assert mf.exists()


def test_sweep_orphans_empty_cache_dir_is_noop(tmp_path, monkeypatch):
    """sweep_orphans() on a non-existent cache dir must not raise."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path / "does_not_exist"))
    monkeypatch.delenv("NP_KEEP_CACHE", raising=False)
    ws.sweep_orphans()  # must not raise


def test_sweep_orphans_malformed_manifest_is_skipped(tmp_path, monkeypatch):
    """sweep_orphans() skips manifests with malformed JSON without crashing."""
    monkeypatch.setenv("NP_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("NP_KEEP_CACHE", raising=False)

    bad_mf = tmp_path / ".np_staged_bad.json"
    bad_mf.write_text("{not valid json")

    ws.sweep_orphans()  # must not raise
