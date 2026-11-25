"""Registry integrity — keeps the canonical/deprecated script list HONEST.

`scripts/REGISTRY.yaml` is the source of truth for which scripts exist and whether they
are canonical / library / scrape / deprecated. The pre-commit guard (#6) enforces this on
commit, but the hook is only active after `make install-hooks` — so this test enforces the
same invariants in `make test` + CI, where they can't be silently lost. Parses the YAML by
line (no PyYAML dep).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REGISTRY = REPO / "scripts" / "REGISTRY.yaml"
VALID_STATUS = {"canonical", "library", "scrape", "deprecated"}

_ENTRY_RE = re.compile(r'^\s*"(scripts/[^"]+)":\s*\{status:\s*([a-z_]+)')


def _entries() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in REGISTRY.read_text().splitlines():
        m = _ENTRY_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _actual_scripts() -> set[str]:
    return {
        str(p.relative_to(REPO))
        for p in (REPO / "scripts").rglob("*")
        if p.suffix in {".py", ".sh"} and "git-hooks" not in p.parts
    }


def test_every_script_is_registered():
    """No script in scripts/ is missing from the registry (mirrors guard #6)."""
    missing = _actual_scripts() - set(_entries())
    assert not missing, f"unregistered scripts (guard #6 blocks these): {sorted(missing)}"


def test_no_phantom_registry_entries():
    """Every registered .py/.sh path actually exists (no stale entries)."""
    entries = _entries()
    phantom = [p for p in entries if p.endswith((".py", ".sh")) and not (REPO / p).exists()]
    assert not phantom, f"registry lists nonexistent scripts: {sorted(phantom)}"


def test_all_statuses_valid():
    bad = {p: s for p, s in _entries().items() if s not in VALID_STATUS}
    assert not bad, f"invalid registry status(es): {bad} (allowed: {sorted(VALID_STATUS)})"


def test_meta_total_matches_entry_count():
    m = re.search(r"total_scripts:\s*(\d+)", REGISTRY.read_text())
    assert m, "REGISTRY.yaml missing _meta.total_scripts"
    declared, actual = int(m.group(1)), len(_entries())
    assert declared == actual, f"_meta.total_scripts={declared} but {actual} entries present"
