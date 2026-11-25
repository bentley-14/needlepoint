"""Medicaid T-MSIS state x year data-quality reliability gate.

CMS's T-MSIS Data Quality (DQ) Atlas rates each state's Medicaid claims data
per year/measure from "low concern" to "unusable"; absence of Medicaid
billing for a defendant in a bad state-year does NOT prove absence of
Medicaid billing (see ``scripts/medicaid_lookup.py``'s own caveat). This
module is the single place that turns a (state, year) pair into a
``reliable`` / ``high_concern`` / ``unusable`` rating, backed by the
human-curated map at ``data/medicaid_data_quality.yaml``.

See that file's header for the current provenance gap: the map ships with
zero states flagged (default ``reliable`` for everything) because the
per-state list behind the well-sourced "6 unusable + 16 high-concern"
counts is not reproducible from a fetchable public source. Populating
`states:` in the yaml is enough to make this gate live -- no code change
needed.

Usage:
    from fraudlib.medicaid_quality import medicaid_reliability
    rating = medicaid_reliability("NY", 2023)   # -> "reliable" | "high_concern" | "unusable"
"""
from __future__ import annotations

import functools
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
QUALITY_MAP_PATH = REPO_ROOT / "data/medicaid_data_quality.yaml"

VALID_RATINGS = frozenset({"reliable", "high_concern", "unusable"})
_FALLBACK_DEFAULT = "reliable"


@functools.lru_cache(maxsize=1)
def _load_map() -> dict:
    """Parse the yaml map once per process (cached); safe defaults on any failure.

    A missing file or an unparsable yaml degrades to "everything reliable"
    (fail-open) rather than raising -- this gate must never be the thing
    that breaks a feature build.
    """
    if not QUALITY_MAP_PATH.exists():
        return {"default": _FALLBACK_DEFAULT, "states": {}}
    try:
        import yaml  # noqa: PLC0415 -- optional dependency, guarded on purpose
        with open(QUALITY_MAP_PATH) as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {"default": _FALLBACK_DEFAULT, "states": {}}
    default = data.get("default", _FALLBACK_DEFAULT)
    if default not in VALID_RATINGS:
        default = _FALLBACK_DEFAULT
    states = data.get("states") or {}
    return {"default": default, "states": states}


def medicaid_reliability(state: str | None, year: int | str) -> str:
    """Return the T-MSIS data-quality rating for one (state, year).

    ``state`` is a 2-letter postal abbreviation (case-insensitive); a
    missing/None/unknown state or year falls through to the map's
    ``default`` (``reliable`` unless the yaml overrides it) -- absence from
    the map means "not flagged", not "invalid input".
    """
    data = _load_map()
    if not state:
        return data["default"]
    year_block = data["states"].get(str(year), {})
    rating = year_block.get(state.strip().upper(), data["default"])
    return rating if rating in VALID_RATINGS else data["default"]
