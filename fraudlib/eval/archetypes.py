"""Map verified positives to fraud archetypes for per-archetype model training.

The verified positive set is:
  - **DOJ defendants** from the canonical labels corpus
    (``data/extracted/gold_set.jsonl``, high-confidence NPI
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

# 10 scheme-level heads for multi-label training. walk_forward_v4 trains one
# XGB per scheme; feature_frames.stamp_labels_and_filter imports SCHEMES + scheme_lookup.
# DISTINCT taxonomy from the 6 ARCHETYPES above (which archetype_lookup uses for eval).
# Restored 2026-05-28: the gold_set migration dropped it but the ranker still needs it.
SCHEMES = (
    "PHANTOM_BILLING",
    "MED_UNNECESSARY",
    "KICKBACK",
    "OPIOID",
    "HHA_HOSPICE",
    "DME",
    "UPCODING",
    "NPI_THEFT",
    "TELEHEALTH",
    "LAB_FRAUD",
)

# Mapping from raw DOJ fraud_type → scheme head(s) for the multi-label scheme
# training in scripts/experiments/walk_forward_v4.py (one XGB per scheme).
# This is a DIFFERENT taxonomy from the 6 ARCHETYPES above (single-label, used by
# archetype_lookup) — the scheme heads are PHANTOM_BILLING / MED_UNNECESSARY /
# KICKBACK / OPIOID / UPCODING / HHA_HOSPICE (+ dropped DME/TELEHEALTH/LAB/NPI_THEFT).
# Restored 2026-05-28: the gold_set migration dropped this dict but
# walk_forward_v4 still imports it (broke the sweep at import time).
_FRAUD_TYPE_TO_SCHEMES: dict[str, list[str]] = {
    "phantom_billing":          ["PHANTOM_BILLING"],
    "medically_unnecessary":    ["MED_UNNECESSARY"],
    "kickback":                 ["KICKBACK"],
    "kickbacks":                ["KICKBACK"],  # typo in data
    "stark_violation":          ["KICKBACK"],
    "compounding_fraud":        ["KICKBACK"],
    "opioid_diversion":         ["OPIOID"],
    "controlled_substance":     ["OPIOID"],
    "addiction_treatment_fraud": ["OPIOID"],
    "drug_diversion":           ["OPIOID"],
    "hha_poc_falsification":    ["HHA_HOSPICE"],
    "hospice_eligibility":      ["HHA_HOSPICE"],
    "dme_scheme":               ["DME"],
    "upcoding":                 ["UPCODING"],
    "unbundling":               ["UPCODING"],
    "npi_theft":                ["NPI_THEFT"],
    "telemedicine_fraud":       ["TELEHEALTH"],
    "genetic_testing_fraud":    ["TELEHEALTH"],
    "lab_requisition_fraud":    ["LAB_FRAUD"],
    "falsification_of_records": ["PHANTOM_BILLING"],
    "other":                    ["PHANTOM_BILLING"],
}


def _fraud_types_to_schemes(fraud_types: Iterable[str]) -> set[str]:
    """Map a case's fraud_types list to all applicable scheme labels.

    Used by walk_forward_v4 / run_ablation for multi-label scheme targets.
    Restored 2026-05-28 alongside _FRAUD_TYPE_TO_SCHEMES.
    """
    schemes: set[str] = set()
    for ft in (fraud_types or []):
        mapped = _FRAUD_TYPE_TO_SCHEMES.get(ft.lower().strip())
        if mapped:
            schemes.update(mapped)
        else:
            schemes.add("PHANTOM_BILLING")  # safe fallback
    return schemes


def scheme_lookup(year: int, min_confidence: str = "high") -> dict[str, set[str]]:
    """Return ``{npi_padded: set_of_schemes}`` for verified positives in ``year``.

    Multi-label: each NPI maps to ALL scheme heads across ALL of its DOJ cases.
    Used by walk_forward_v4 + feature_frames.stamp_labels_and_filter to build
    the per-scheme training targets. Restored 2026-05-28 alongside SCHEMES.
    """
    out: dict[str, set[str]] = {}
    doj_meta = load_positives_scoped(year, window=0, min_confidence=min_confidence)
    for npi, meta in doj_meta.items():
        npi_padded = str(npi).zfill(10)
        schemes = _fraud_types_to_schemes(meta.get("fraud_types", []))
        if npi_padded in out:
            out[npi_padded] |= schemes
        else:
            out[npi_padded] = schemes
    return out


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

    Verified positives = **gold_set.jsonl** records where ``all_active_years``
    intersects ``[year]`` (via ``load_positives_scoped`` with ``window=0``).

    NPI dedup: when same NPI appears in multiple gold_set cases, the case
    with the most fraud_types wins (richer signal for archetype assignment).

    ``lookahead_years`` is retained for caller compatibility and is now a no-op.
    """
    out: dict[str, str] = {}

    # window=0 means STRICT membership: NPI's all_active_years must contain `year`.
    doj_meta = load_positives_scoped(year, window=0, min_confidence="high")

    # Deterministic NPI dedup: if same NPI in multiple cases, prefer the case
    # with more fraud_types (richer archetype signal). Replaces last-writer-wins.
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
        # Pick archetype from case with most fraud_types
        best = max(candidates, key=lambda x: x[0])
        out[npi_padded] = best[1]

    return out


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
