"""Deterministically auto-fix dossier markdown formatting.

Companion fixer to ``scripts/lint_dossier_markdown.py`` (which detects;
this repairs). Applied by the case writer (agents/case_writer.py) to each
prose deliverable before it is written, so generation can never fail on
formatting; the orchestrator's post-write lint stays as a verify-after-fix
backstop.

Fix rules (applied OUTSIDE fenced code blocks + inline backtick spans only):

  1. Escape unescaped currency dollars: ``$`` before a digit → ``\\$``
     (mirrors lint's UNESCAPED_DOLLAR_RE; existing ``\\$`` untouched).
  2. ``***bold-italic***`` → ``**bold**`` (lint's triple_asterisks rule).
  3. Wrap bare snake_case identifiers in backticks (lint's
     snake_case_in_prose rule) — skipping markdown link targets ``](...)``
     and tokens already adjacent to a backtick.

Pipes in table cells are NOT touched — the lint has no pipe rule.

Usage:
    python scripts/fix_dossier_markdown.py < in.md > out.md
    python scripts/fix_dossier_markdown.py file.md            # fixed → stdout
    python scripts/fix_dossier_markdown.py --in-place a.md b.md
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Share the lint's currency-dollar definition (single source of truth).
# Package import works with repo root on sys.path (PYTHONPATH=.); the
# importlib fallback covers direct CLI runs + path-based loading.
try:
    from scripts.lint_dossier_markdown import UNESCAPED_DOLLAR_RE
except ImportError:  # pragma: no cover - exercised when run as a script
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "lint_dossier_markdown",
        Path(__file__).resolve().parent / "lint_dossier_markdown.py",
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    UNESCAPED_DOLLAR_RE = _mod.UNESCAPED_DOLLAR_RE

# ── Patterns ────────────────────────────────────────────────────────────────

# Capture-group variant of the lint's TRIPLE_ASTERISK_RE so the inner text
# can be rewritten as plain bold.
TRIPLE_ASTERISK_FIX_RE = re.compile(r'\*\*\*([^*\n]+)\*\*\*')

# Superset of the lint's SNAKE_CASE_RE: also matches digit-bearing segments
# (e.g. ``rules_v3``, ``gold_set_2024``), which the lint's letters-only
# pattern misses but which break italics identically. Leading [a-z]
# guarantees the token contains a letter.
SNAKE_CASE_FIX_RE = re.compile(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b')

# Markdown link target immediately following link text: ``](...)``.
# snake_case inside hrefs must stay bare (backticks would corrupt the URL).
_LINK_TARGET_RE = re.compile(r'\]\([^)\n]*\)')

# Inline code span on a single line (lint strips the same shape).
_INLINE_CODE_RE = re.compile(r'`[^`\n]*`')


# ── Fixers (operate on prose-only text, no code spans) ─────────────────────

def _fix_prose(text: str) -> str:
    """Apply all fix rules to a chunk known to contain no code spans."""
    text = TRIPLE_ASTERISK_FIX_RE.sub(r'**\1**', text)
    text = UNESCAPED_DOLLAR_RE.sub(r'\\$', text)

    skip_spans = [m.span() for m in _LINK_TARGET_RE.finditer(text)]

    def _wrap(m: re.Match) -> str:
        i, j = m.span()
        if any(a <= i < b for a, b in skip_spans):
            return m.group(0)  # inside a link target
        # Adjacent backtick → already (partially) code-delimited; leave it.
        if (i > 0 and text[i - 1] == '`') or (j < len(text) and text[j] == '`'):
            return m.group(0)
        return f'`{m.group(0)}`'

    return SNAKE_CASE_FIX_RE.sub(_wrap, text)


def _fix_line(line: str) -> str:
    """Fix one non-fence line, leaving inline backtick spans verbatim."""
    parts: list[str] = []
    pos = 0
    for m in _INLINE_CODE_RE.finditer(line):
        parts.append(_fix_prose(line[pos:m.start()]))
        parts.append(m.group(0))
        pos = m.end()
    parts.append(_fix_prose(line[pos:]))
    return ''.join(parts)


def fix_text(text: str) -> str:
    """Auto-fix a full markdown document. Idempotent: fix(fix(x)) == fix(x)."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith('```'):
            in_fence = not in_fence
            out.append(line)
        elif in_fence:
            out.append(line)
        else:
            out.append(_fix_line(line))
    return ''.join(out)


def fix_file(path: Path, in_place: bool = False) -> str:
    """Fix one file; rewrite it when ``in_place`` else return the fixed text."""
    fixed = fix_text(path.read_text(errors='replace'))
    if in_place:
        path.write_text(fixed)
    return fixed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('files', nargs='*', type=Path,
                    help='Markdown files to fix (omit to filter stdin → stdout)')
    ap.add_argument('--in-place', action='store_true',
                    help='Rewrite the given files instead of printing to stdout')
    args = ap.parse_args(argv)

    if not args.files:
        sys.stdout.write(fix_text(sys.stdin.read()))
        return 0
    for f in args.files:
        if not f.is_file():
            print(f'ERROR: {f} not found', file=sys.stderr)
            return 1
        fixed = fix_file(f, in_place=args.in_place)
        if args.in_place:
            print(f'fixed: {f}', file=sys.stderr)
        else:
            sys.stdout.write(fixed)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
