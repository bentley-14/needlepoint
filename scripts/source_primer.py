"""Print a per-source domain primer (`data/docs/<source>.md`) for the investigator.

Each data source ships a domain primer written for understanding it: plain-English
description, acronym decode, unit of observation, publication cadence + suppression
rules, **which columns light up for which fraud scheme**, joins to other sources,
year coverage + drift, and known limitations. Consult the relevant primer BEFORE
hand-writing queries against an unfamiliar source — it is the schema + business-logic
context the per-source CLI tools (`dmepos_detailed_lookup`, `partd_drug_lookup`, …)
encode operationally.

Jail-safe by design: this reads a repo-local file via a CLI script, so it works even
when the `Read` tool is jailed to the case state dir (calibration mode). Primers are
static domain knowledge with no provider-specific or post-cutoff facts.

Usage:
    python scripts/source_primer.py --list
    python scripts/source_primer.py --source cms_dmepos_detailed
    python scripts/source_primer.py --source physician_compare --section 4   # one "## N." section
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "data/docs"


def _available() -> list[str]:
    return sorted(p.stem for p in DOCS.glob("*.md"))


def _extract_section(text: str, section: str) -> str | None:
    # Match a "## N." or "## N " heading and capture until the next "## " of the same level.
    pat = re.compile(rf"^##\s+{re.escape(section)}[.\s].*?(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
    m = pat.search(text)
    return m.group(0).rstrip() if m else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", help="source name (the data/docs/<name>.md stem)")
    ap.add_argument("--section", help="print only top-level section N (e.g. 4 for 'Fraud signals')")
    ap.add_argument("--list", action="store_true", help="list available primers")
    args = ap.parse_args(argv)

    avail = _available()
    if args.list or not args.source:
        print("Available source primers (data/docs/<name>.md):")
        for name in avail:
            print(f"  {name}")
        print("\nUse: python scripts/source_primer.py --source <name>")
        return 0 if args.list else 2

    path = DOCS / f"{args.source}.md"
    if not path.exists():
        print(f"ERROR: no primer for {args.source!r}. Available: {', '.join(avail)}", file=sys.stderr)
        return 1

    text = path.read_text()
    if args.section:
        sec = _extract_section(text, args.section)
        if sec is None:
            print(f"ERROR: no top-level section {args.section!r} in {args.source}.md", file=sys.stderr)
            return 1
        print(sec)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
