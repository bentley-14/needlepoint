"""Persisted ledger of curated lead-ids — the cross-run ``claude -p`` dedup.

The discovery scraper re-finds the same DOJ releases every cron run: the
committed warm cache (``raw_press_releases.jsonl``) is a *frozen* dedup baseline
and the cron never writes it back. So without a record of what the **curator**
has already consumed, every weekly run would re-spend ``claude -p`` extracting
the same releases (the gate's already-labeled dedup only prevents duplicate
*labels*, and it fires *after* the tokens are spent).

This ledger is that record: one line per lead-id the curator has processed.
``GoldSetBuilder.run_once`` filters it out **before** invoking the curator, so a
release is curated exactly once across runs, and a per-run cap then bounds spend.

Persistence is via **R2**, not git (pulled before the run, pushed after): it's mutable run-state, not a
reviewable label. A lead is added ONLY after its chunk *completes*, so a chunk
killed mid-run (e.g. the org spend limit) leaves its leads absent → retried next
run. That is the no-loss property the throttle depends on.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from pipeline.scrapers.download import PROJECT_ROOT

# Lives under data/source/ (≙ r2:needlepoint-data/ground_truth/v1 — see Makefile
# R2_CORPUS). The cron rclone-copies just this one file to/from R2 each run.
LEDGER_PATH = PROJECT_ROOT / "data" / "source" / "doj_api" / "curated_ledger.jsonl"


def load_ledger(path: Path = LEDGER_PATH) -> set[str]:
    """Set of lead-ids already curated. Empty when the ledger doesn't exist yet
    (greenfield / first run). Tolerates malformed lines rather than crashing the
    weekly run on a partially-written file."""
    path = Path(path)
    if not path.exists():
        return set()
    out: set[str] = set()
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                lid = json.loads(line).get("lead_id")
            except json.JSONDecodeError:
                continue
            if lid:
                out.add(lid)
    return out


def append_ledger(curated: Iterable[tuple[str, str]], path: Path = LEDGER_PATH) -> int:
    """Append ``(lead_id, primary_source_url)`` pairs not already recorded.

    Dedups against the existing ledger so re-runs don't bloat it; append-only
    (we dedup on read, so order/uniqueness across appends is handled there).
    Returns the number of newly-added lead-ids.
    """
    path = Path(path)
    existing = load_ledger(path)
    new = [(lid, url) for lid, url in curated if lid and lid not in existing]
    if not new:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for lid, url in new:
            f.write(json.dumps({"lead_id": lid, "url": url}) + "\n")
    return len(new)
