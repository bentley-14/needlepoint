"""6-archetype multi-label fraud lookup (V1, DOJ-only).

Mirrors the consolidated 6-bucket vocabulary from commit 50a4881 but with
multi-label semantics (an NPI can be positive for multiple archetypes,
matching today's 10-scheme architecture).

The 6 archetypes fold the 10 schemes as follows:
    OPIOID_MILL          ← OPIOID
    KICKBACK             ← KICKBACK
    UPCODING             ← UPCODING
    MEDICALLY_UNNECESSARY ← MED_UNNECESSARY
    BILLING_FRAUD        ← PHANTOM_BILLING, HHA_HOSPICE, DME, NPI_THEFT,
                            TELEHEALTH, LAB_FRAUD
    OTHER_FRAUD          ← fallback only (unmapped fraud_type)
"""
from __future__ import annotations

from typing import Iterable

from fraudlib.labels.ground_truth import load_positives_scoped

ARCHETYPES_V1 = (
    "OPIOID_MILL",
    "BILLING_FRAUD",
    "KICKBACK",
    "UPCODING",
    "MEDICALLY_UNNECESSARY",
    "OTHER_FRAUD",
)

_FRAUD_TYPE_TO_ARCHETYPES_V1: dict[str, list[str]] = {
    # Opioid
    "controlled_substance":     ["OPIOID_MILL"],
    "opioid_diversion":         ["OPIOID_MILL"],
    "addiction_treatment_fraud": ["OPIOID_MILL"],
    "drug_diversion":           ["OPIOID_MILL"],
    # Kickback
    "kickback":                 ["KICKBACK"],
    "kickbacks":                ["KICKBACK"],
    "stark_violation":          ["KICKBACK"],
    "compounding_fraud":        ["KICKBACK"],
    # Upcoding
    "upcoding":                 ["UPCODING"],
    "unbundling":               ["UPCODING"],
    # Medically unnecessary
    "medically_unnecessary":    ["MEDICALLY_UNNECESSARY"],
    # Billing fraud (the 6 sub-types from current scheme set)
    "phantom_billing":          ["BILLING_FRAUD"],
    "npi_theft":                ["BILLING_FRAUD"],
    "dme_scheme":               ["BILLING_FRAUD"],
    "hha_poc_falsification":    ["BILLING_FRAUD"],
    "hospice_eligibility":      ["BILLING_FRAUD"],
    "telemedicine_fraud":       ["BILLING_FRAUD"],
    "genetic_testing_fraud":    ["BILLING_FRAUD"],
    "lab_requisition_fraud":    ["BILLING_FRAUD"],
    "falsification_of_records": ["BILLING_FRAUD"],
    "other":                    ["OTHER_FRAUD"],
}


def _fraud_types_to_archetypes_v1(fraud_types: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for ft in (fraud_types or []):
        ft_clean = ft.lower().strip()
        mapped = _FRAUD_TYPE_TO_ARCHETYPES_V1.get(ft_clean)
        if mapped:
            out.update(mapped)
        else:
            out.add("OTHER_FRAUD")
    return out


def archetypes_v1_lookup(year: int, min_confidence: str = "high") -> dict[str, set[str]]:
    """Return ``{npi_padded: set_of_archetypes}`` for verified positives in ``year``.

    DOJ-only. Multi-label: an NPI gets every archetype that any of its
    DOJ cases' fraud_types map to.
    """
    out: dict[str, set[str]] = {}
    doj_meta = load_positives_scoped(year, min_confidence=min_confidence)
    for npi, meta in doj_meta.items():
        npi_padded = str(npi).zfill(10)
        archs = _fraud_types_to_archetypes_v1(meta.get("fraud_types", []))
        if npi_padded in out:
            out[npi_padded] |= archs
        else:
            out[npi_padded] = archs
    return out
