"""Weekly gold_set builder run (links 2–4) for the cron.

Runs leads → curator → gate → drift-guarded merge, then writes a human-readable
digest (``format_run_digest``) to ``--body-file`` for the notification email.

Quiet-on-zero: emits ``should_notify`` / ``changed`` flags to ``$GITHUB_OUTPUT``
so the workflow skips the email (and the PR) when nothing was added and nothing
needs review — DOJ publishing is batchy, so most weeks are no-ops.

Usage (cron does the real write; --dry-run for local/manual testing):
    python -m pipeline.scheduled_scripts.run_gold_set_builder --body-file digest.txt
    python -m pipeline.scheduled_scripts.run_gold_set_builder --dry-run --body-file /tmp/d.txt
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Classify only; write nothing to gold_set (local/manual testing).")
    ap.add_argument("--body-file", type=Path, default=Path("gold_set_run_digest.txt"),
                    help="Where to write the human-readable digest (the email body).")
    ap.add_argument("--max-new", type=int, default=int(os.environ.get("GOLDSET_MAX_NEW") or "16"),
                    help="Cap on NEW leads curated this run (spend bound; default $GOLDSET_MAX_NEW or 16). "
                         "Already-curated leads are skipped via the R2 ledger, so runs drain a backlog "
                         "incrementally.")
    args = ap.parse_args(argv)

    report, digest = GoldSetBuilder().run_once(dry_run=args.dry_run, max_new=args.max_new)
    args.body_file.write_text(digest, encoding="utf-8")

    counts = report.get("counts", {})
    added = len(report.get("append", []))
    n_review = len(report.get("update", [])) + counts.get("gate_hold", 0) + counts.get("park", 0)
    changed = bool(report.get("written"))          # gold_set actually mutated
    should_notify = bool(added or n_review)         # quiet-on-zero

    _emit("added", added)
    _emit("needs_review", n_review)
    _emit("changed", str(changed).lower())
    _emit("should_notify", str(should_notify).lower())

    print(f"[run_gold_set_builder] added={added} needs_review={n_review} "
          f"changed={changed} (dry_run={args.dry_run}) → {args.body_file}", flush=True)
    print("-" * 66 + "\n" + digest, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
