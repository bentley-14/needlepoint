"""One-shot historical gold_set backfill (links 2–4 over the FULL DOJ corpus).

Unlike the *weekly* runner (``run_gold_set_builder``), which processes only the
releases link-1 discovery surfaced this week, this runs the curator → gate →
drift-guarded merge over the **entire** warm cache (``cache_leads()`` — every
DOJ release ever fetched), in chunks so no single curator turn is overloaded.

It is the reconciliation pass the maintainer asked for: with the gold_set now solid, sweep
all of history through the *current* curator + deterministic gate to recover
defendants the original pass missed and surface conduct-window extensions on
known NPIs. The gate's Step-0 already-labeled dedupe makes it safe to re-run —
confirmed labels with no new years are SKIPped, so this only ADDS new defendants
and EXTENDS (never shrinks/removes) — see ``pipeline/docs/GOLD_SET_BACKFILL.md``.

Quiet-on-zero + `$GITHUB_OUTPUT` flags mirror the weekly runner so the workflow
reuses the same draft-PR + email steps.

Usage::

    # local smoke (writes nothing): first 8 leads, one chunk
    python -m pipeline.scripts.backfill_gold_set --dry-run --limit 8

    # real backfill (auto-appends new defendants; updates/holds → review queue)
    python -m pipeline.scripts.backfill_gold_set --chunk-size 8 \
        --body-file gold_set_backfill_digest.txt

    # resume a timed-out run (skips chunks already extracted)
    python -m pipeline.scripts.backfill_gold_set --resume state/runs/goldset_backfill_<ts>
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from pipeline.scrapers.gold_set_builder import GoldSetBuilder


def _emit(name: str, value) -> None:
    """Write a GitHub Actions step output (no-op when run outside CI)."""
    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def _accumulate_csv(src: Path, dest: Path) -> None:
    """Copy this wave's review CSV to ``dest``, APPENDING (minus header) if ``dest``
    already exists — so an auto-chaining backfill grows one CSV across waves."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = src.read_text().splitlines()
    if not dest.exists():
        dest.write_text("\n".join(lines) + ("\n" if lines else ""))
    elif len(lines) > 1:                                  # append rows, skip the header
        with dest.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines[1:]) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Classify only; write nothing to gold_set (local/manual testing).")
    ap.add_argument("--chunk-size", type=int, default=8,
                    help="Leads per curator invocation (default 8 — the validated batch size).")
    ap.add_argument("--offset", type=int, default=0,
                    help="Skip the first N leads — for non-overlapping waves (offset 0, then 500, …). "
                         "Ordering is stable, so a given offset always lands on the same leads.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap leads taken AFTER the offset (smoke runs / wave size); omit to go to the end.")
    ap.add_argument("--workers", type=int, default=1,
                    help="Concurrent curator subprocesses. Raise (e.g. 16) to fit a full sweep into one "
                         "overnight run; mind LLM rate limits.")
    ap.add_argument("--deadline-minutes", type=int, default=None,
                    help="Wall-clock budget: stop launching chunks after N minutes and checkpoint "
                         "(for the auto-chaining waves that work around the 6h GitHub job cap). "
                         "Emits next_offset/done so the next wave resumes from where this one stopped.")
    ap.add_argument("--clean-room", action="store_true",
                    help="Build a fresh label set into --out (canonical untouched), treating gold_set as "
                         "empty. Default (off) = incremental merge into canonical.")
    ap.add_argument("--out", type=Path, default=None,
                    help="Clean-room output file (default data/extracted/gold_set_backfill.jsonl).")
    ap.add_argument("--resume", type=Path, default=None,
                    help="Reuse an existing run dir; skip chunks already extracted (after a timeout).")
    ap.add_argument("--body-file", type=Path, default=Path("gold_set_backfill_digest.txt"),
                    help="Where to write the human-readable digest (the email body).")
    ap.add_argument("--csv-file", type=Path, default=None,
                    help="Where to accumulate the review-worklist CSV (HOLD/PARK), an email attachment.")
    ap.add_argument("--diff-csv-file", type=Path, default=None,
                    help="Where to write the reliability-audit diff CSV (rebuilt vs canonical), an email "
                         "attachment. Full snapshot each wave (overwritten, not appended).")
    args = ap.parse_args(argv)

    report, digest = GoldSetBuilder().run_backfill(
        dry_run=args.dry_run,
        chunk_size=args.chunk_size,
        limit=args.limit,
        offset=args.offset,
        workers=args.workers,
        deadline_minutes=args.deadline_minutes,
        clean_room=args.clean_room,
        out_path=args.out,
        run_dir=args.resume,
        resume=args.resume is not None,
    )
    args.body_file.write_text(digest, encoding="utf-8")
    if args.csv_file and report.get("review_csv"):
        _accumulate_csv(Path(report["review_csv"]), args.csv_file)          # worklist: append across waves
    if args.diff_csv_file and report.get("diff_csv"):
        import shutil
        args.diff_csv_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(report["diff_csv"], args.diff_csv_file)                 # audit: full snapshot, overwrite

    counts = report.get("counts", {})
    added = len(report.get("append", []))
    n_review = len(report.get("update", [])) + counts.get("gate_hold", 0) + counts.get("park", 0)
    changed = bool(report.get("written"))          # gold_set actually mutated
    should_notify = bool(added or n_review)         # quiet-on-zero

    _emit("added", added)
    _emit("needs_review", n_review)
    _emit("changed", str(changed).lower())
    _emit("should_notify", str(should_notify).lower())
    bf = report.get("backfill", {})
    _emit("run_dir", bf.get("run_dir", ""))
    _emit("review_csv", report.get("review_csv", ""))
    _emit("out_path", report.get("out_path", ""))      # clean-room file to push to R2
    _emit("next_offset", bf.get("next_offset", ""))    # where the next wave resumes
    _emit("done", str(bf.get("done", True)).lower())   # whole corpus processed?

    print(f"[backfill_gold_set] mode={'clean-room' if bf.get('clean_room') else 'incremental'} "
          f"offset={bf.get('offset')} completed_chunks={bf.get('completed_chunks')}/{bf.get('chunks')} "
          f"next_offset={bf.get('next_offset')} done={bf.get('done')} workers={bf.get('workers')} "
          f"added={added} needs_review={n_review} (dry_run={args.dry_run}) → {args.body_file}", flush=True)
    print("-" * 66 + "\n" + digest, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
