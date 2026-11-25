"""Coverage-anchor registry API — is a code family FILEABLE on public data?

Loads data/reference/coverage_anchors.yaml (the Lab Ebony-lesson registry: an
org case is fileable on public data iff its MO violates a PUBLIC coverage-rule
condition of payment). Consumers:

* scripts/entity_target_screen.py — ranks the fileable band first
  (anchor_available column) so anchored MOs outrank anchorless extremity.
* fraud_investigator evidence pack — injects the anchor entry for the target's
  top codes at Step-0, so counsel opens with the falsity theory instead of
  needing a mid-run human checkpoint to supply the LCD.

Match precedence: exact code -> longest code_prefix. Returns None for unknown
codes (unknown != unfileable; it means "registry silent — investigate").
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "data" / "reference" / "coverage_anchors.yaml"


@functools.lru_cache(maxsize=1)
def _load() -> dict[str, dict[str, Any]]:
    with REGISTRY_PATH.open() as f:
        doc = yaml.safe_load(f) or {}
    return doc.get("families", {}) or {}


def families() -> dict[str, dict[str, Any]]:
    """The raw registry (family name -> entry)."""
    return _load()


def anchor_for_code(code: str | None) -> dict[str, Any] | None:
    """Registry entry for a HCPCS/CPT code, or None when the registry is silent.

    Exact-code match wins; otherwise the LONGEST matching code_prefix. The
    returned dict is the family entry plus ``family`` (its name) and
    ``matched_via`` ("code" | "prefix:<p>").
    """
    if not code:
        return None
    c = str(code).strip().upper()
    fams = _load()
    for name, fam in fams.items():
        if c in {str(x).upper() for x in (fam.get("codes") or [])}:
            return {**fam, "family": name, "matched_via": "code"}
    best: tuple[int, str, dict] | None = None
    for name, fam in fams.items():
        for p in (fam.get("code_prefixes") or []):
            p = str(p).upper()
            if c.startswith(p) and (best is None or len(p) > best[0]):
                best = (len(p), name, fam)
    if best:
        _, name, fam = best
        return {**fam, "family": name, "matched_via": f"prefix:{name}"}
    return None


def fileable(code: str | None) -> bool | None:
    """True/False when the registry speaks; None when silent."""
    e = anchor_for_code(code)
    return None if e is None else bool(e.get("fileable_band"))
