"""Map verified positives to fraud archetypes for per-archetype model training.

The verified positive set is:
  - **DOJ defendants** from ``gold_set.jsonl`` (high-confidence NPI
    confidence); each carries a ``fraud_types`` list

API
---
``archetype_lookup(year)`` → dict[str, str] mapping NPI → archetype.

``ARCHETYPES`` → ordered tuple of archetype names. 5 archetypes:

    OPIOID_MILL          — DOJ-controlled-substance / opioid_diversion /
                           telemedicine_fraud + medically_unnecessary in
                           Anesth/Pain/PMR
    BILLING_FRAUD        — DOJ-phantom_billing / npi_theft / lab /
                           dme_scheme / hospice / hha_poc / etc.
    KICKBACK             — DOJ-kickback / stark
    UPCODING             — DOJ-upcoding
    MEDICALLY_UNNECESSARY — DOJ-medically_unnecessary (non-opioid specialty)
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

from fraudlib.labels.ground_truth import load_positives_scoped

# 6 consolidated archetypes for training (12 was too fragmented —
# ~20 positives per model at 0.04% base rate gave XGBoost nothing to learn).
# Sub-types preserved in the mapping for analysis but merged for training.
ARCHETYPES = (
    "OPIOID_MILL",            # opioid diversion, controlled substance, addiction treatment
    "BILLING_FRAUD",          # phantom billing, NPI theft, HHA falsification, hospice, DME, lab, telemedicine
    "KICKBACK",               # kickback, stark violation, compounding
    "UPCODING",               # upcoding, unbundling
    "MEDICALLY_UNNECESSARY",  # medically unnecessary procedures
    "OTHER_FRAUD",            # anything that doesn't fit the above
)

_OPIOID_HEAVY_SPECIALTIES = frozenset({
    "Anesthesiology", "Pain Management", "Interventional Pain Management",
    "Physical Medicine and Rehabilitation",
    "Certified Registered Nurse Anesthetist (CRNA)",
})


def _doj_fraud_to_archetype(
    fraud_types: Iterable[str], specialty: str | None = None
) -> str:
    """Map DOJ fraud_types list (per case) to a single archetype."""
    fts = {ft.lower().strip() for ft in (fraud_types or []) if ft}
    spec = (specialty or "").strip()

    # OPIOID_MILL: controlled substance, opioid diversion, addiction treatment
    if any(ft in fts for ft in ("controlled_substance", "opioid_diversion")):
        return "OPIOID_MILL"
    if "addiction_treatment_fraud" in fts:
        return "OPIOID_MILL"
    if "medically_unnecessary" in fts and spec in _OPIOID_HEAVY_SPECIALTIES:
        return "OPIOID_MILL"

    # KICKBACK: kickback, stark, compounding
    if "kickback" in fts or "stark_violation" in fts or "compounding_fraud" in fts:
        return "KICKBACK"

    # UPCODING: upcoding, unbundling
    if "upcoding" in fts or "unbundling" in fts:
        return "UPCODING"

    # MEDICALLY_UNNECESSARY (non-opioid specialty)
    if "medically_unnecessary" in fts:
        return "MEDICALLY_UNNECESSARY"

    # BILLING_FRAUD: everything else that involves false billing
    # phantom billing, NPI theft, HHA falsification, hospice, DME,
    # lab fraud, telemedicine fraud, genetic testing
    if any(ft in fts for ft in (
        "phantom_billing", "npi_theft", "hha_poc_falsification",
        "hospice_eligibility", "dme_scheme", "lab_requisition_fraud",
        "telemedicine_fraud", "genetic_testing_fraud",
    )):
        return "BILLING_FRAUD"

    return "BILLING_FRAUD"  # safe fallback


def archetype_lookup(year: int, lookahead_years: int = 5) -> dict[str, str]:
    """Return ``{npi_padded: archetype}`` for verified positives in ``year``.

    DOJ defendants from ``gold_set.jsonl`` (high-confidence, active in
    ``year``). ``lookahead_years`` is retained for caller compatibility
    and is now a no-op.

    NPI dedup: when same NPI appears in multiple DOJ cases, the case
    with the most fraud_types wins (richer signal for archetype assignment).
    """
    out: dict[str, str] = {}

    doj_meta = load_positives_scoped(year, min_confidence="high")

    npi_candidates: dict[str, list[tuple[int, str]]] = {}
    for npi, meta in doj_meta.items():
        npi_padded = str(npi).zfill(10)
        archetype = _doj_fraud_to_archetype(
            meta.get("fraud_types", []), meta.get("specialty"),
        )
        n_types = len(meta.get("fraud_types", []))
        if npi_padded not in npi_candidates:
            npi_candidates[npi_padded] = []
        npi_candidates[npi_padded].append((n_types, archetype))

    for npi_padded, candidates in npi_candidates.items():
        best = max(candidates, key=lambda x: x[0])
        out[npi_padded] = best[1]

    return out


_FULL_PANEL_CACHE = Path("data/cache")


def archetype_lookup_active(
    year: int,
    lookahead_years: int = 5,
    bene_floor: int = 50,
    pay_floor: int = 5000,
) -> dict[str, str]:
    """Alias for :func:`archetype_lookup`. The activity-filter only ever
    affected non-DOJ positives (now removed); for DOJ-only positives it
    is a no-op. ``bene_floor`` / ``pay_floor`` are retained for caller
    compatibility.
    """
    return archetype_lookup(year, lookahead_years=lookahead_years)


def archetype_distribution(year: int, active_only: bool = False) -> pd.Series:
    """Return per-archetype positive count for ``year`` (diagnostic)."""
    lookup = archetype_lookup_active(year) if active_only else archetype_lookup(year)
    return pd.Series(list(lookup.values())).value_counts().reindex(ARCHETYPES, fill_value=0)
