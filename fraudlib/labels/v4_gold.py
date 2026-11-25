"""Year-scoped DOJ-only positive loader for the gold_set corpus.

As of 2026-05-22, **gold_set.jsonl is the sole canonical training ground
truth.** NPI-centric (one row per NPI), outcome-capped (active years
restricted to ≤outcome-date AND years with actual Medicare billing
presence), 1,333 NPIs.

Schema (key fields used by this loader):
  - ``npi`` (zero-padded NPI string; legacy files use ``v4_npi`` — read with fallback)
  - ``all_active_years`` (list[int])
  - ``case_id`` (str)
  - ``defendant_name`` (str)
  - ``defendant_specialty`` (str)
  - ``fraud_types`` (list[str])

This loader reads DOJ-only positives.

Split assignment
----------------
Splits (train/test/val) are derived deterministically from
``md5(case_id) % 100`` → 60/30/10. The walk-forward training pipeline uses
TEMPORAL splits (train_years vs val_year vs score_year), not the case-level
split field — so for that path this code never executes. Case-level splits
are only used by some legacy rule-coverage analyses.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

# Default path resolution: search candidate locations across worktrees.
_WORKTREE_ROOT = Path(__file__).resolve().parents[2]   # .../<worktree>/
_REPO_ROOT = _WORKTREE_ROOT.parent.parent              # .../Healthcare Fraud Detection/

_V4_CANDIDATES = [
    _WORKTREE_ROOT / "data/extracted/gold_set.jsonl",   # canonical as of 2026-05-28
    _REPO_ROOT / "data/extracted/gold_set.jsonl",
    _WORKTREE_ROOT / "data/source/gold_set.jsonl",       # legacy location (pre-migration)
    _WORKTREE_ROOT.parent / "case-corpus-rebuild/data/source/gold_set.jsonl",
    _REPO_ROOT / "data/source/gold_set.jsonl",
]
DEFAULT_V4_PATH = next((p for p in _V4_CANDIDATES if p.exists()), _V4_CANDIDATES[0])

Split = Literal["train", "test", "val"]


def _hash_split(case_id: str) -> str:
    """Deterministic 60/30/10 train/test/val split by case_id hash."""
    h = int(hashlib.md5(case_id.encode("utf-8")).hexdigest(), 16) % 100
    if h < 60:
        return "train"
    if h < 90:
        return "test"
    return "val"


def _split_for_case(case_id: str) -> str:
    """Deterministic-hash split for a case_id (no external dependency)."""
    if not case_id or case_id == "?":
        return "train"  # safe default
    return _hash_split(case_id)


def load_v4_positives_scoped(
    year: int,
    split: Split | None = None,
    min_confidence: str = "high",  # noqa: ARG001 - kept for signature parity; ignored
    cases_path: Path | None = None,  # noqa: ARG001 - kept for signature parity; ignored
    v4_path: Path | None = None,
) -> dict[str, dict]:
    """Return ``{npi_padded: {name, specialty, case_id, split, active_years, fraud_types}}``
    for gold rows whose ``all_active_years`` contains ``year`` (exact match).

    Parameters
    ----------
    year
        Filter rows whose ``all_active_years`` contains this year. Exact match
        only — the legacy ``window`` parameter has been removed (2026-05-22)
        because near-year matching introduced label noise (defendants who
        stopped offending years before ``year`` were being labeled positive).
    split
        If set, filter to rows whose case has this split (train/test/val).
        Split is derived deterministically from ``md5(case_id) % 100``
        (60/30/10 train/test/val).
    min_confidence, cases_path
        Accepted for backward compat; both **ignored**. The gold_set.jsonl
        file is the post-audit canonical, so every row is "high" confidence;
        set ``v4_path`` instead to override the source.
    v4_path
        Override path to the gold_set.jsonl file.

    Returns
    -------
    dict[str, dict]
        Maps zero-padded NPI string to metadata. If the same NPI appears in
        multiple rows (multi-case offender) we keep the first hit's
        case_id / name / specialty / split but **union** fraud_types and
        active_years across all hits.
    """
    path = v4_path or DEFAULT_V4_PATH
    if not path.exists():
        raise FileNotFoundError(f"gold_set not found at {path}")

    out: dict[str, dict] = {}
    for line in path.open():
        r = json.loads(line)
        ay = r.get("all_active_years") or []
        if year not in ay:
            continue
        npi = str(r.get("npi") or r.get("v4_npi") or "").zfill(10)
        if not npi or npi == "0" * 10:
            continue
        case_id = r.get("case_id") or "?"
        row_split = r.get("split") or _split_for_case(case_id)  # public labels carry their split
        if split is not None and row_split != split:
            continue
        if npi not in out:
            out[npi] = {
                "name": ((r.get("defendant_name") or "?").split() or ["?"])[-1],
                "specialty": (r.get("defendant_specialty") or "?").split(",")[0].strip(),
                "case_id": case_id,
                "split": row_split,
                "active_years": list(ay),
                "fraud_types": list(r.get("fraud_types") or []),
            }
        else:
            # Multi-case offender: union fraud_types + active_years
            ft_union = set(out[npi]["fraud_types"]) | set(r.get("fraud_types") or [])
            yr_union = set(out[npi]["active_years"]) | set(ay)
            out[npi]["fraud_types"] = sorted(ft_union)
            out[npi]["active_years"] = sorted(yr_union)
    return out


def load_v4_positives_all_years(
    v4_path: Path | None = None,
) -> set[str]:
    """Return the set of all unique zero-padded NPIs in the v4 gold file
    (regardless of active years). Useful for NPI-symmetric holdout."""
    path = v4_path or DEFAULT_V4_PATH
    if not path.exists():
        raise FileNotFoundError(f"v4 gold not found at {path}")
    out: set[str] = set()
    for line in path.open():
        r = json.loads(line)
        npi = str(r.get("npi") or r.get("v4_npi") or "").zfill(10)
        if npi and npi != "0" * 10:
            out.add(npi)
    return out
