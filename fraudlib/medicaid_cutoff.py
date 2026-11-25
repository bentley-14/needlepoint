"""Point-in-time cutoff for Medicaid/T-MSIS reads — leak-safe calibration.

The Part-B calibration export (scripts/calibration/build_calibration_workspace.py)
clones only pre-cutoff panels, so Part-B tools are fail-closed by construction.
The all-payer Medicaid tools instead read the single full T-MSIS parquet + the
per-year medicaid_features/entity frames, so they can see POST-cutoff billing
(e.g. the enforcement-year collapse) mid-run — a calibration answer leak.

This module gives every Medicaid reader one shared gate driven by the env var
``FRAUD_INV_MEDICAID_CUTOFF`` (ISO date, e.g. "2024-01-01"), which the
calibration orchestrator sets for the whole run so BOTH the Step-0 pack build
and any mid-run agent tool call are gated identically. Unset in production =
no gating (the common case), so this is inert outside calibration.

T-MSIS ``CLAIM_FROM_MONTH`` is ``YYYY-MM`` (verified), so a lexicographic
``< 'YYYY-MM'`` comparison is correct and needs no date parsing.
"""
from __future__ import annotations

import os

ENV_VAR = "FRAUD_INV_MEDICAID_CUTOFF"


def cutoff_iso() -> str | None:
    """The raw ISO cutoff date from the env, or None when unset/blank."""
    v = os.environ.get(ENV_VAR, "").strip()
    return v or None


def cutoff_ym() -> str | None:
    """Cutoff as 'YYYY-MM' for lexicographic CLAIM_FROM_MONTH comparison."""
    c = cutoff_iso()
    return c[:7] if c and len(c) >= 7 else None


def cutoff_year() -> int | None:
    c = cutoff_iso()
    try:
        return int(c[:4]) if c else None
    except (TypeError, ValueError):
        return None


def claim_clause(col: str = "CLAIM_FROM_MONTH", *, prefix: str = "AND") -> str:
    """SQL fragment restricting a T-MSIS query to strictly-before-cutoff months.

    Returns '' when no cutoff is set (production). ``prefix`` lets callers drop
    it into either an existing WHERE (``AND``) or a fresh one (``WHERE``).
    """
    ym = cutoff_ym()
    return f" {prefix} {col} < '{ym}'" if ym else ""


def feature_year_ok(year: int) -> bool:
    """True if a per-year frame (medicaid_features_{y}/entity_graph_{y}) is
    admissible: a year-Y frame describes billing through Dec Y, so it is
    admissible only when Y < cutoff_year."""
    cy = cutoff_year()
    return cy is None or year < cy


def clamp_feature_year(year: int) -> int:
    """Clamp a requested frame year down to the newest admissible one
    (cutoff_year - 1) so a tool invoked with --year 2024 under a 2024 cutoff
    reads the 2023 frame instead of leaking the enforcement-year frame."""
    cy = cutoff_year()
    return min(year, cy - 1) if cy is not None else year


def banner() -> str | None:
    """Human-readable notice for tool output so a gated run is self-evident."""
    c = cutoff_iso()
    return (f"POINT-IN-TIME CALIBRATION: Medicaid reads gated to before {c} "
            f"(CLAIM_FROM_MONTH < {cutoff_ym()}, frames <= {cutoff_year() - 1})"
            ) if c else None
