"""R2-backed ephemeral working set for the walk-forward runner.

Stage parquet panels from R2 at the start of a walk, train, then purge on
completion — so local disk holds only transient scratch during a run.


Knobs (env vars):
    NP_R2_STAGING   "1" → active; default "0" = read local as today (zero change)
    NP_KEEP_CACHE   "1" → skip purge (dev mode: avoid re-pulling ~18 GB each cycle)
    NP_CACHE_DIR    override scratch location (default: data/cache/feature_frames)

Usage:
    from fraudlib.ranker.r2_working_set import manifest, stage, purge, sweep_orphans

    items = manifest(score_years=[2022], attach_extras=True)
    run_id, staged = stage(items)
    try:
        ...  # train
    finally:
        purge(run_id)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import NamedTuple

from fraudlib.r2 import r2_copy

# ── R2 prefix constants ──
R2_FEATURE_FRAMES = "r2:needlepoint-data/cache/v1/feature_frames"   # features_full_<y>, hcpcs_rich_<y>, dmepos_rich_<y>

# Satellite parquet R2 prefixes (canonical cache/v1/misc/, confirmed present). NOTE: a normal
# walk-forward run reads the PRE-MERGED features_full (use_full_cache=True) which already contains
# these, so staging them is belt-and-suspenders — only needed for a from-base (non-full) rebuild.
_R2_SATELLITE_PREFIXES = {
    "hcpcs_rich":     R2_FEATURE_FRAMES,                                  # cache/v1/feature_frames
    "dmepos":         "r2:needlepoint-data/cache/v1/misc/dmepos_features",
    "openpayments":   "r2:needlepoint-data/cache/v1/misc/openpayments_features",
    "claim_level":    "r2:needlepoint-data/cache/v1/misc/claim_level_features",
}

# R2 + local file stem per satellite (differs from the key for op / claim).
_SATELLITE_FILE_STEM = {
    "hcpcs_rich": "hcpcs_rich", "dmepos": "dmepos", "openpayments": "op", "claim_level": "claim",
}

# Local satellite subdirectories (relative to data/cache/)
_LOCAL_SATELLITE_SUBDIRS = {
    "hcpcs_rich":   "feature_frames",
    "dmepos":       "dmepos_features",
    "openpayments": "openpayments_features",
    "claim_level":  "claim_level_features",
}


def _cache_dir() -> Path:
    return Path(os.environ.get("NP_CACHE_DIR", "data/cache/feature_frames"))


# ── Public types ─────────────────────────────────────────────────────────────

class WorkItem(NamedTuple):
    r2_key: str
    local_path: Path


# ── manifest ─────────────────────────────────────────────────────────────────

def manifest(
    score_years: list[int],
    attach_extras: bool,
    train_start: int = 2013,
) -> list[WorkItem]:
    """Return the list of (r2_key, local_path) pairs for a walk-forward run.

    Builds the UNION of all panels needed across every score year:
      features_full_{y} for y in train_start..max(score_years)
    (because every panel from train_start is reused by subsequent folds).

    If attach_extras=True, also includes the satellite parquets for the same
    year range (hcpcs_rich, dmepos, openpayments, claim_level).
    """
    if not score_years:
        return []
    end_year = max(score_years)
    years = list(range(train_start, end_year + 1))
    cache = _cache_dir()
    items: list[WorkItem] = []

    for y in years:
        fname = f"features_full_{y}.parquet"
        r2_key = f"{R2_FEATURE_FRAMES}/{fname}"
        local_path = cache / fname
        items.append(WorkItem(r2_key=r2_key, local_path=local_path))

    if attach_extras:
        for sat_name, r2_prefix in _R2_SATELLITE_PREFIXES.items():
            subdir = _LOCAL_SATELLITE_SUBDIRS[sat_name]
            local_base = Path("data/cache") / subdir
            stem = _SATELLITE_FILE_STEM[sat_name]
            for y in years:
                fname = f"{stem}_{y}.parquet"
                r2_key = f"{r2_prefix}/{fname}"
                local_path = local_base / fname
                items.append(WorkItem(r2_key=r2_key, local_path=local_path))

    return items


# ── stage ────────────────────────────────────────────────────────────────────

def stage(
    items: list[WorkItem],
    run_id: str | None = None,
) -> tuple[str, list[Path]]:
    """Download any missing panels from R2; record which files this call fetched.

    Pre-existing local files are left alone and NOT counted as staged-by-us
    (so purge() won't delete them).

    Writes the list of files this call fetched to:
        <cache_dir>/.np_staged_<run_id>.json

    Args:
        items:   output of manifest()
        run_id:  stable identifier for this run; defaults to "<pid>-<ts>"

    Returns:
        (run_id, list_of_staged_paths)  — staged_paths is only the newly pulled files
    """
    if run_id is None:
        run_id = f"{os.getpid()}-{int(time.time())}"

    staged: list[Path] = []
    for item in items:
        if item.local_path.exists():
            continue  # already local — skip, do NOT re-pull, NOT our responsibility
        r2_copy(item.r2_key, item.local_path)
        staged.append(item.local_path)

    manifest_path = _cache_dir() / f".np_staged_{run_id}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "pid": os.getpid(),
                "staged": [str(p) for p in staged],
            },
            indent=2,
        )
    )
    return run_id, staged


# ── purge ────────────────────────────────────────────────────────────────────

def purge(run_id: str) -> None:
    """Delete ONLY the files this run staged; then remove the staged manifest.

    Respects NP_KEEP_CACHE=1 (skip purge entirely).
    Never touches pre-existing files, repo files, or results/.
    """
    if os.environ.get("NP_KEEP_CACHE") == "1":
        return

    manifest_path = _cache_dir() / f".np_staged_{run_id}.json"
    if not manifest_path.exists():
        return

    data = json.loads(manifest_path.read_text())
    for p_str in data.get("staged", []):
        p = Path(p_str)
        if p.exists():
            p.unlink()
    manifest_path.unlink()


# ── sweep_orphans ─────────────────────────────────────────────────────────────

def sweep_orphans() -> None:
    """Purge staged manifests whose originating process is no longer alive.

    Call once at startup when NP_R2_STAGING=1 to clean up after crashes.
    Respects NP_KEEP_CACHE=1.
    """
    if os.environ.get("NP_KEEP_CACHE") == "1":
        return

    cache = _cache_dir()
    if not cache.exists():
        return

    for mf in cache.glob(".np_staged_*.json"):
        try:
            data = json.loads(mf.read_text())
        except Exception:
            continue
        pid = data.get("pid")
        if pid is None:
            continue
        if not _pid_alive(pid):
            # Orphan — purge its files
            run_id = data.get("run_id", mf.stem.replace(".np_staged_", ""))
            purge(run_id)


def _pid_alive(pid: int) -> bool:
    """Return True if the given pid is still running."""
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't own it — treat as alive (safe side)
        return True
