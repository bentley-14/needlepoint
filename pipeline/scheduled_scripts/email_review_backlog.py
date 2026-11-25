"""Email the standing gold_set human-review worklist.

Reads the rolling review queue (``data/extracted/proposals/needs_review.jsonl``),
renders a human-readable digest to ``--body-file``, and emits ``open_items`` /
``should_notify`` to ``$GITHUB_OUTPUT`` so the workflow can email it (quiet when
the queue is empty). This is decoupled from the data pipeline — no curator, no
panels — so it's a cheap, reliable weekly "here's everything needing a human".

Usage:
    python -m pipeline.scheduled_scripts.email_review_backlog --body-file review.txt
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from pipeline.scrapers.gold_set_builder import read_review_queue, format_review_backlog


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--body-file", type=Path, default=Path("review_digest.txt"))
    args = ap.parse_args(argv)

    items = read_review_queue()
    args.body_file.write_text(format_review_backlog(items), encoding="utf-8")

    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a", encoding="utf-8") as f:
            f.write(f"open_items={len(items)}\n")
            f.write(f"should_notify={'true' if items else 'false'}\n")

    print(f"[email_review_backlog] {len(items)} open item(s) → {args.body_file}", flush=True)
    print("-" * 66 + "\n" + args.body_file.read_text(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
