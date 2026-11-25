"""Monthly NPPES snapshot runner.

Steps:

1. Fetch the latest NPPES bulk file via ``NppesScraper``.
2. Stamp a date-keyed snapshot copy in ``data/registries/snapshots/``.
3. Diff the new snapshot against the previous (pulled to
   ``data/registries/_prev/`` by the workflow). Report rows added /
   removed / changed and the top columns where values drifted.
4. Push both the "latest" alias and the date-stamped snapshot + diff
   report to R2.
5. Write a Markdown report consumed by ``$GITHUB_STEP_SUMMARY``.

Idempotent within a month: re-running on the same snapshot tag
overwrites the same date-stamped snapshot. Re-running across months
produces a fresh file.

Run locally for testing with::

    PYTHONPATH=. .venv/bin/python -m pipeline.scheduled_scripts.snapshot_nppes --no-push

The ``--no-push`` flag skips R2 uploads so the script can be exercised
without R2 credentials.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from pipeline.scheduled_scripts.utils import compute_snapshot_diff, format_markdown_report
from pipeline.scrapers.nppes import NppesScraper

REGISTRIES = Path("data/registries")
SNAPSHOTS_DIR = REGISTRIES / "snapshots"
PREV_DIR = REGISTRIES / "_prev"

R2_LATEST_PREFIX = "r2:needlepoint-data/processed/v1"
R2_SNAPSHOT_PREFIX = "r2:needlepoint-data/processed/v1/snapshots"


def _run(cmd: list[str]) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def _rclone_push(src: Path, dst: str) -> None:
    _run(
        [
            "rclone",
            "copyto",
            str(src),
            dst,
            "--s3-no-check-bucket",
            "--transfers",
            "1",
            "--multi-thread-streams",
            "1",
        ]
    )


def _derive_snapshot_tag(manifest_path: Path) -> str:
    """Extract ``YYYY_MM`` from the manifest's input URL.

    Falls back to today's year+month if the URL doesn't match the
    expected ``NPPES_Data_Dissemination_<Month>_<Year>[_V2].zip`` form.
    """
    m = json.loads(manifest_path.read_text())
    url = m.get("inputs", {}).get("url", "")
    match = re.search(r"Dissemination_([A-Za-z]+)_(\d{4})", url)
    if not match:
        today = _dt.date.today()
        return f"{today.year}_{today.month:02d}"
    month_name, year = match.group(1), match.group(2)
    month_num = _dt.datetime.strptime(month_name, "%B").month
    return f"{year}_{month_num:02d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--no-push",
        action="store_true",
        help="Skip R2 uploads (for local testing without credentials).",
    )
    args = ap.parse_args()

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    print("[snapshot_nppes] fetching latest NPPES monthly...", flush=True)
    result = NppesScraper().fetch()
    new_parquet = Path(result.artifact_path)
    new_manifest = new_parquet.with_suffix(".manifest.json")

    snapshot_tag = _derive_snapshot_tag(new_manifest)
    print(f"[snapshot_nppes] snapshot tag: {snapshot_tag}", flush=True)

    snapshot_parquet = SNAPSHOTS_DIR / f"nppes_bulk_{snapshot_tag}.parquet"
    snapshot_manifest = SNAPSHOTS_DIR / f"nppes_bulk_{snapshot_tag}.manifest.json"
    shutil.copy2(new_parquet, snapshot_parquet)
    shutil.copy2(new_manifest, snapshot_manifest)

    prev_parquet = PREV_DIR / "nppes_bulk.parquet"
    if prev_parquet.exists():
        print("[snapshot_nppes] computing diff vs previous...", flush=True)
        diff = compute_snapshot_diff(prev_parquet, new_parquet, key="npi")
    else:
        print(
            "[snapshot_nppes] no previous snapshot — first run, skipping diff",
            flush=True,
        )
        diff = {"first_run": True}

    diff["snapshot_tag"] = snapshot_tag

    diff_path = SNAPSHOTS_DIR / f"nppes_bulk_{snapshot_tag}.diff.json"
    diff_path.write_text(json.dumps(diff, indent=2, default=str))

    md_path = REGISTRIES / "nppes_diff_report.md"
    md_path.write_text(format_markdown_report(diff, title="NPPES snapshot"))
    print(md_path.read_text(), flush=True)

    # Free the ~7 GB extracted CSV before R2 push — the CI runner has
    # 14 GB of disk and the upload step doesn't need the raw file.
    raw_csv_dir = Path("data/raw/nppes")
    for p in raw_csv_dir.glob("*.csv"):
        print(f"[snapshot_nppes] removing raw {p}", flush=True)
        p.unlink(missing_ok=True)

    if args.no_push:
        print("[snapshot_nppes] --no-push set, skipping R2 uploads", flush=True)
        return 0

    print("[snapshot_nppes] pushing to R2...", flush=True)
    _rclone_push(new_parquet, f"{R2_LATEST_PREFIX}/nppes_bulk.parquet")
    _rclone_push(new_manifest, f"{R2_LATEST_PREFIX}/nppes_bulk.manifest.json")
    _rclone_push(snapshot_parquet, f"{R2_SNAPSHOT_PREFIX}/{snapshot_parquet.name}")
    _rclone_push(snapshot_manifest, f"{R2_SNAPSHOT_PREFIX}/{snapshot_manifest.name}")
    _rclone_push(diff_path, f"{R2_SNAPSHOT_PREFIX}/{diff_path.name}")

    print("[snapshot_nppes] done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
