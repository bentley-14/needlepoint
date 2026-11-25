"""Vocabulary drift guard — block NEWLY-ADDED deprecated terms in current-surface files.

`scripts/REGISTRY.yaml`'s ``vocabulary:`` section is the source of truth for
canonical vs DEPRECATED terms/artifacts (the non-script analogue of the
``entries`` block): e.g. gold_set (not "v9 labels"), floor_0.50 (not "rules_v3
agent filter"), the Corpus Curator (not
doj_api_ingest).

Why this guard exists: we kept re-introducing stale terms (v9 → gold_set,
rules_v3-as-filter → floor_0.50, …) because the live code moved on but the
*descriptions* (comments, agent prompts, docs, console cards) lagged, and there
was no enforced source of truth. This guard scans the STAGED DIFF's **ADDED
lines only**, so it never chokes on the many existing legacy mentions — it just
stops *new* drift (the exact mistake that recurs). History / why-deprecated docs
are exempt.

Stdlib-only (parses the vocabulary block by regex, like the registry guard #6),
so it stays fast and import-free in the pre-commit hook.

Exit: 0 = clean · 1 = a staged ADDED line introduces a deprecated term.
Bypass: ``ALLOW_VOCAB_DRIFT=1 git commit ...``
Run:    ``python scripts/verify_vocabulary.py``
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REGISTRY = REPO / "scripts" / "REGISTRY.yaml"

# Current-surface files we lint (where descriptions / prose / canon live).
SURFACE_SUFFIXES = (".md", ".jsx", ".js", ".py", ".yaml", ".yml", ".json")

# Paths exempt: the canon definitions themselves + history/why-deprecated docs +
# captured agent transcripts (historical, immutable) + generated state.
SKIP_SUBSTR = (
    "scripts/REGISTRY.yaml",            # the canon itself (lists the deprecated terms)
    "scripts/verify_vocabulary.py",     # this guard (mentions the terms)
    "scripts/git-hooks/",               # the hook (documents the terms)
    "docs/GOLD_SET.md",
    ".raw.jsonl",
    ".raw.attempt",
    "/state/",
)

_VOCAB_LINE = re.compile(
    r'^\s*"([^"]+)":\s*\{use:\s*"([^"]*)",\s*deprecated:\s*\[(.*)\]\}\s*$'
)


def _load_vocab() -> list[tuple[str, str]]:
    """Return [(deprecated_phrase_lower, canonical_use), ...] from REGISTRY ``vocabulary:``."""
    out: list[tuple[str, str]] = []
    in_vocab = False
    for line in REGISTRY.read_text().splitlines():
        if re.match(r"^vocabulary:\s*$", line):
            in_vocab = True
            continue
        if in_vocab and re.match(r"^[A-Za-z_]", line):  # next top-level key ends the block
            break
        if not in_vocab:
            continue
        m = _VOCAB_LINE.match(line)
        if not m:
            continue
        use = m.group(2)
        for term in re.findall(r'"([^"]+)"', m.group(3)):
            if term.strip():
                out.append((term.lower(), use))
    return out


def _staged_additions() -> list[tuple[str, str]]:
    """Return [(path, added_line), ...] for ADDED lines in surface (non-skipped) files."""
    try:
        diff = subprocess.run(
            ["git", "diff", "--cached", "--unified=0", "--diff-filter=ACM"],
            capture_output=True, text=True, cwd=str(REPO),
        ).stdout
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    cur: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if cur is None or not cur.endswith(SURFACE_SUFFIXES):
            continue
        if any(s in cur for s in SKIP_SUBSTR):
            continue
        out.append((cur, line[1:]))
    return out


def main() -> int:
    vocab = _load_vocab()
    if not vocab:
        return 0
    hits: list[str] = []
    for path, text in _staged_additions():
        low = text.lower()
        for term, use in vocab:
            if term in low:
                hits.append(
                    f"  {path}: deprecated '{term}'  ->  use: {use}\n"
                    f"      + {text.strip()[:120]}"
                )
    if hits:
        print("Vocabulary drift — staged ADDED line(s) introduce DEPRECATED term(s):",
              file=sys.stderr)
        print("\n".join(hits), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
