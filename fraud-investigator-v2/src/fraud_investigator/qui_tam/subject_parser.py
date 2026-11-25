"""Parse the free-form ``summary.json["subject"]`` string into caption fields.

The orchestrator stores the subject as a double-dash-separated string, e.g.::

    "Provider Cedar -- Internal Medicine -- Anytown, IN --
     NPI 1900000075 -- score_rrf=0.0181 -- partb_pay=$95K, 72 benes, ..."

This module extracts the fields needed for the caption block and prose:
``name_caption`` (ALL CAPS), ``name_prose`` (Title Case), ``credentials``,
``specialty``, ``city``, ``state``, ``npi``.

Parsing is defensive — on any failure, return placeholder values and log a
warning. Never raise.
"""
from __future__ import annotations

import logging
import re
from typing import TypedDict

log = logging.getLogger(__name__)


class SubjectFields(TypedDict):
    name_caption: str
    name_prose: str
    credentials: str
    specialty: str
    city: str
    state: str
    npi: str


_NPI_RE = re.compile(r"\bNPI\s+(\d{10})\b", re.IGNORECASE)
_CITY_STATE_RE = re.compile(r"^([A-Za-z][A-Za-z .'\-]+?),\s*([A-Z]{2})\b")


_PLACEHOLDER: SubjectFields = {
    "name_caption": "JOHN DOE, M.D.",
    "name_prose": "John Doe, M.D.",
    "credentials": "M.D.",
    "specialty": "Unknown Specialty",
    "city": "Unknown City",
    "state": "XX",
    "npi": "UNKNOWN",
}


def parse_subject(subject: str | None) -> SubjectFields:
    """Parse a subject string into caption fields. Never raises."""
    if not subject:
        log.warning("subject_parser: empty subject; using placeholders")
        return dict(_PLACEHOLDER)  # type: ignore[return-value]

    try:
        parts = [p.strip() for p in subject.split("--") if p.strip()]
        if not parts:
            raise ValueError("no dash-separated parts")

        # Part 0: name + credentials (e.g., "Provider Cedar")
        name_raw = parts[0]
        credentials = ""
        m = re.search(r",\s*(M\.?D\.?|D\.?O\.?|N\.?P\.?|P\.?A\.?|R\.?N\.?|"
                      r"D\.?P\.?M\.?|D\.?D\.?S\.?|D\.?M\.?D\.?|Pharm\.?D\.?|"
                      r"Ph\.?D\.?)\.?$",
                      name_raw)
        if m:
            credentials = m.group(1).replace(".", "").upper()
            credentials = ".".join(credentials) + "."
            # Re-normalize "MD" -> "M.D." style
            if credentials == "M.D.":
                pass
            name_prose = name_raw.strip()
            name_only = name_raw[: m.start()].rstrip(", ").strip()
        else:
            name_only = name_raw
            name_prose = name_raw

        # ALL CAPS variant for caption
        if credentials:
            name_caption = f"{name_only.upper()}, {credentials}"
        else:
            name_caption = name_only.upper()

        # Part 1: specialty (best-effort)
        specialty = parts[1] if len(parts) > 1 else "Unknown Specialty"

        # Part 2: "City, ST"
        city = "Unknown City"
        state = "XX"
        if len(parts) > 2:
            m2 = _CITY_STATE_RE.match(parts[2])
            if m2:
                city = m2.group(1).strip()
                state = m2.group(2).strip().upper()

        # NPI: search the entire subject (more robust than indexing parts)
        npi = "UNKNOWN"
        m3 = _NPI_RE.search(subject)
        if m3:
            npi = m3.group(1)

        return {
            "name_caption": name_caption,
            "name_prose": name_prose,
            "credentials": credentials or "",
            "specialty": specialty,
            "city": city,
            "state": state,
            "npi": npi,
        }
    except Exception as e:  # never crash
        log.warning("subject_parser: parse failed (%s); using placeholders", e)
        return dict(_PLACEHOLDER)  # type: ignore[return-value]


# ────────────────────────────────────────────────────────────────────────────
# District inference
# ────────────────────────────────────────────────────────────────────────────

# Minimal state → default federal district mapping. For multi-district
# states, picks the district most commonly associated with the listed city.
# This is intentionally narrow — the PDF prominently shows "DRAFT — NOT FOR
# FILING," so the venue need only be plausible, not legally authoritative.
_STATE_TO_DISTRICT: dict[str, str] = {
    "AL": "Northern District of Alabama",
    "AK": "District of Alaska",
    "AZ": "District of Arizona",
    "AR": "Eastern District of Arkansas",
    "CA": "Central District of California",
    "CO": "District of Colorado",
    "CT": "District of Connecticut",
    "DE": "District of Delaware",
    "DC": "District of Columbia",
    "FL": "Southern District of Florida",
    "GA": "Northern District of Georgia",
    "HI": "District of Hawaii",
    "ID": "District of Idaho",
    "IL": "Northern District of Illinois",
    "IN": "Northern District of Indiana",
    "IA": "Southern District of Iowa",
    "KS": "District of Kansas",
    "KY": "Eastern District of Kentucky",
    "LA": "Eastern District of Louisiana",
    "ME": "District of Maine",
    "MD": "District of Maryland",
    "MA": "District of Massachusetts",
    "MI": "Eastern District of Michigan",
    "MN": "District of Minnesota",
    "MS": "Southern District of Mississippi",
    "MO": "Eastern District of Missouri",
    "MT": "District of Montana",
    "NE": "District of Nebraska",
    "NV": "District of Nevada",
    "NH": "District of New Hampshire",
    "NJ": "District of New Jersey",
    "NM": "District of New Mexico",
    "NY": "Southern District of New York",
    "NC": "Eastern District of North Carolina",
    "ND": "District of North Dakota",
    "OH": "Northern District of Ohio",
    "OK": "Western District of Oklahoma",
    "OR": "District of Oregon",
    "PA": "Eastern District of Pennsylvania",
    "RI": "District of Rhode Island",
    "SC": "District of South Carolina",
    "SD": "District of South Dakota",
    "TN": "Middle District of Tennessee",
    "TX": "Southern District of Texas",
    "UT": "District of Utah",
    "VT": "District of Vermont",
    "VA": "Eastern District of Virginia",
    "WA": "Western District of Washington",
    "WV": "Southern District of West Virginia",
    "WI": "Eastern District of Wisconsin",
    "WY": "District of Wyoming",
}


# City-level overrides for multi-district states, where the per-state default is wrong for
# a given city. Keyed by UPPERCASE city. (e.g. KY: Louisville = Western, Lexington = Eastern.)
_CITY_TO_DISTRICT: dict[str, str] = {
    "LOUISVILLE": "Western District of Kentucky",
    "BOWLING GREEN": "Western District of Kentucky",
    "PADUCAH": "Western District of Kentucky",
    "LEXINGTON": "Eastern District of Kentucky",
    "COVINGTON": "Eastern District of Kentucky",
}


def infer_district(state: str, litigation_venue: str | None = None,
                   city: str | None = None) -> str:
    """Pick the district name.

    Preference order:
      1. Explicit ``litigation_venue`` from summary.json if present
      2. City-level override for multi-district states (e.g. Louisville -> W.D. Ky.)
      3. Per-state default in the mapping
      4. Generic placeholder
    """
    if litigation_venue:
        v = litigation_venue.replace("U.S. District Court for the ", "")
        v = v.replace("United States District Court for the ", "")
        return v.strip()
    if city:
        override = _CITY_TO_DISTRICT.get(city.strip().upper())
        if override:
            return override
    return _STATE_TO_DISTRICT.get((state or "").upper(),
                                  "District of [STATE]")


def division_from_city(city: str) -> str:
    """Best-effort division name (most districts use ``[CITY] Division``)."""
    if not city or city == "Unknown City":
        return ""
    return f"{city} Division"
