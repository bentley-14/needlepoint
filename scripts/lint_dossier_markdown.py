"""Lint generated dossier markdown for accidental-italics + render-fragility issues.

Detects the formatting patterns documented in
``fraud-investigator-v2/src/fraud_investigator/prompts/case_writer.md``:

  1. Bare snake_case identifiers in prose (not wrapped in backticks)
     — these render with broken italics in CommonMark / Streamlit because
     underscores get parsed as italic delimiters and pair across adjacent
     tokens on the same line.
  2. Triple-asterisks (``***text***``) for bold+italic — renders with
     inconsistent spacing across renderers.
  3. Same-line co-occurrence of ≥2 snake_case identifiers
     — the highest-risk italic-bunching pattern.

Usage:
    python scripts/lint_dossier_markdown.py investigations/Larch-2026-05-02/
    python scripts/lint_dossier_markdown.py path/to/01_Case_Report.md
    python scripts/lint_dossier_markdown.py investigations/  # all dossiers
    python scripts/lint_dossier_markdown.py --strict <path>  # exit 1 on any finding
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ── Patterns ────────────────────────────────────────────────────────────────

# snake_case in prose: a lowercase token containing at least one underscore.
# Must NOT be inside a code span (backticks). Allow it inside literal
# JSON-like blocks (case writer may emit a few intentional ones in
# config-spec passages; those are bracketed by `{` `}`).
SNAKE_CASE_RE = re.compile(r'(?<![`\w])([a-z]+_[a-z][a-z_]*)(?![`\w])')

TRIPLE_ASTERISK_RE = re.compile(r'\*\*\*[^*\n]+\*\*\*')

# Unescaped dollar-sign followed by a digit (currency). Streamlit's
# st.markdown — and any renderer with a KaTeX/MathJax extension (Obsidian,
# pandoc-with-math, GitHub-with-math, etc.) — treats $...$ as inline LaTeX
# math. A line containing two unescaped currency amounts (e.g. "$241 per
# service ... $2.87M Medicare paid") gets the text between them rendered
# in italic mathematical font. Escape every currency $ as \$ to defuse.
UNESCAPED_DOLLAR_RE = re.compile(r'(?<!\\)\$(?=[0-9])')


def _strip_code(text: str) -> str:
    """Remove fenced code blocks + inline backtick spans before scanning."""
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    text = re.sub(r'`[^`]*`', '', text)
    return text


def lint_file(path: Path, strict: bool = False) -> list[dict]:
    """Return a list of findings for one markdown file."""
    raw = path.read_text(errors='replace')
    nocode = _strip_code(raw)

    findings: list[dict] = []

    # ── 1. snake_case identifiers in prose ──────────────────────────────
    for ln_no, line in enumerate(nocode.splitlines(), 1):
        tokens = SNAKE_CASE_RE.findall(line)
        if not tokens:
            continue
        unique = sorted(set(tokens))
        sev = 'high' if len(unique) >= 2 else 'medium'
        findings.append({
            'line': ln_no,
            'severity': sev,
            'rule': 'snake_case_in_prose',
            'detail': f'{len(tokens)} identifier(s): {unique[:5]}',
            'excerpt': line.strip()[:140],
            'fix': 'Wrap identifiers in backticks',
        })

    # ── 2. Triple-asterisks ─────────────────────────────────────────────
    for ln_no, line in enumerate(nocode.splitlines(), 1):
        for m in TRIPLE_ASTERISK_RE.finditer(line):
            findings.append({
                'line': ln_no,
                'severity': 'medium',
                'rule': 'triple_asterisks',
                'detail': m.group(0)[:80],
                'excerpt': line.strip()[:140],
                'fix': 'Use **bold** alone instead of ***bold-italic***',
            })

    # ── 3. Unescaped dollar-sign currency (MathJax/KaTeX pair-bug) ──────
    # Per-line scan after code-stripping. Severity = high when ≥2 unescaped
    # $ occurrences appear on the same line (this is the pattern that gets
    # paired into a single inline-math span and italicizes everything
    # between them — by far the most visible render failure). A single $
    # is medium severity (still wrong, but renders correctly in renderers
    # that don't pair across lines).
    for ln_no, line in enumerate(nocode.splitlines(), 1):
        matches = list(UNESCAPED_DOLLAR_RE.finditer(line))
        if not matches:
            continue
        sev = 'high' if len(matches) >= 2 else 'medium'
        sample_after = [line[m.start():m.start() + 8] for m in matches[:4]]
        findings.append({
            'line': ln_no,
            'severity': sev,
            'rule': 'unescaped_dollar_currency',
            'detail': f'{len(matches)} unescaped $ before digit(s): {sample_after}',
            'excerpt': line.strip()[:140],
            'fix': r'Escape every currency $ as \$  (Streamlit pairs $..$ as MathJax)',
        })

    return findings


def lint_target(target: Path, strict: bool = False) -> tuple[int, int]:
    """Lint a file or recursively-found .md files in a directory.

    Returns (n_files_with_findings, n_total_findings).
    """
    files: list[Path] = []
    if target.is_file():
        files = [target]
    elif target.is_dir():
        files = sorted(target.rglob('*.md'))
    else:
        print(f'ERROR: {target} not found', file=sys.stderr)
        return 1, 1

    if not files:
        print(f'No .md files found under {target}')
        return 0, 0

    n_files_with_findings = 0
    n_total = 0
    n_high = 0
    for f in files:
        findings = lint_file(f, strict=strict)
        if not findings:
            continue
        n_files_with_findings += 1
        n_total += len(findings)
        n_high += sum(1 for x in findings if x['severity'] == 'high')

        print(f'\n=== {f} ===')
        print(f'  {len(findings)} finding(s)')
        # Show first 8 per file
        for x in findings[:8]:
            print(f'  L{x["line"]:>4} [{x["severity"]:>6}] {x["rule"]}: {x["detail"]}')
            print(f'         > {x["excerpt"]}')
            print(f'         fix: {x["fix"]}')
        if len(findings) > 8:
            print(f'  ... ({len(findings) - 8} more)')

    print()
    print(f'Summary: {n_files_with_findings} file(s) with {n_total} finding(s) '
          f'({n_high} high severity)')
    return n_files_with_findings, n_total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('target', type=Path,
                    help='Markdown file or directory containing .md files')
    ap.add_argument('--strict', action='store_true',
                    help='Exit 1 if any findings are present (CI gate mode)')
    args = ap.parse_args(argv)

    n_files, n_total = lint_target(args.target, strict=args.strict)
    if args.strict and n_total > 0:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
