"""Thin wrapper around the public NPPES API.

https://npiregistry.cms.hhs.gov/api/ — free, no auth, rate limit is
"reasonable" (they don't publish a number; we sleep 0.1s between calls
to be safe).

Used by corpus-curation flows to turn scraped (name, state) defendants
into NPI candidates that downstream code can panel-confirm against CMS
Part B billing.

Beyond name→NPI resolution, :func:`lookup` fetches a single NPI's full
registry record and returns a richer :class:`NppesProvider` dataclass
that carries the provider-registry signals used as fraud features:

  * ``enumeration_date`` — the date the NPI was issued. A post-billing-year
    enumeration date is a phantom-billing red flag.
  * ``credential`` — free-text credential string ("MD", "DO", "LPN", ...).
  * ``sole_proprietor`` — "YES" / "NO" / None. Sole-proprietor plus
    extreme billing volume is a shell-practice pattern.
  * ``primary_taxonomy_code`` + ``taxonomy_count`` — specialty-arbitrage
    signal when the count grows across years.
  * ``is_organization`` — NPI-2 (org) vs NPI-1 (individual).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable

import requests

API = "https://npiregistry.cms.hhs.gov/api/"
VERSION = "2.1"
SLEEP = 0.1


@dataclass
class NppesHit:
    npi: str
    first_name: str
    last_name: str
    state: str | None
    specialty_primary: str | None
    enumeration_type: str  # "NPI-1" (individual) or "NPI-2" (org)


@dataclass
class NppesProvider:
    """Rich NPPES record for a single NPI — the feature-engineering-facing
    counterpart to :class:`NppesHit`.

    Field mapping against the NPPES v2.1 JSON payload at
    ``results[0]``:

      * ``npi``                    ← ``number``
      * ``enumeration_type``       ← ``enumeration_type`` ("NPI-1"/"NPI-2")
      * ``enumeration_date``       ← ``basic.enumeration_date`` (ISO, "YYYY-MM-DD")
      * ``credential``             ← ``basic.credential``
      * ``sole_proprietor``        ← ``basic.sole_proprietor`` ("YES"/"NO"/None)
      * ``is_organization``        ← derived from ``enumeration_type == "NPI-2"``
      * ``primary_taxonomy_code``  ← ``taxonomies[*].code`` where ``primary=True``
      * ``primary_taxonomy_desc``  ← ``taxonomies[*].desc`` where ``primary=True``
      * ``taxonomy_count``         ← ``len(taxonomies)``
      * ``last_updated``           ← ``basic.last_updated``
      * ``addresses``              ← ``addresses`` (raw dicts, truncated to 5)
    """
    npi: str
    enumeration_type: str
    enumeration_date: str | None
    credential: str | None
    sole_proprietor: str | None
    is_organization: bool
    primary_taxonomy_code: str | None
    primary_taxonomy_desc: str | None
    taxonomy_count: int
    last_updated: str | None
    addresses: list[dict] = field(default_factory=list)


def search(
    first_name: str,
    last_name: str,
    state: str | None = None,
    limit: int = 20,
) -> list[NppesHit]:
    """Hit the NPPES API for (first, last, state). Returns NPI-1 (individual) hits.

    The NPPES search is case-insensitive and supports wildcards via ``*``.
    We pass ``enumeration_type=NPI-1`` so org NPIs (Type 2) are filtered.
    """
    params = {
        "version": VERSION,
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "enumeration_type": "NPI-1",
        "limit": str(limit),
    }
    if state:
        params["state"] = state.strip().upper()
    try:
        r = requests.get(API, params=params, timeout=15)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        print(f"  nppes error ({last_name}, {first_name}, {state}): {e}")
        return []
    time.sleep(SLEEP)

    hits: list[NppesHit] = []
    for result in payload.get("results", []):
        npi = str(result.get("number", ""))
        if not npi:
            continue
        basic = result.get("basic", {}) or {}
        tax = (result.get("taxonomies") or [{}])
        primary = next((t for t in tax if t.get("primary")), tax[0] if tax else {})
        addresses = result.get("addresses") or []
        location_states = [a.get("state") for a in addresses if a.get("address_purpose") == "LOCATION"]
        hit_state = location_states[0] if location_states else (addresses[0].get("state") if addresses else None)
        hits.append(NppesHit(
            npi=npi,
            first_name=basic.get("first_name", "") or "",
            last_name=basic.get("last_name", "") or "",
            state=hit_state,
            specialty_primary=primary.get("desc"),
            enumeration_type=result.get("enumeration_type", ""),
        ))
    return hits


def search_with_variants(
    first_name: str,
    last_name: str,
    state: str | None = None,
) -> list[NppesHit]:
    """Try (first, last, state), then (first*, last, state), then (first, last) nationwide.

    Real-world defendants often appear in DOJ releases with shortened
    first names (Mike vs Michael, Bob vs Robert) or cross state lines.
    This sweeps a few variants and deduplicates by NPI.
    """
    tried: dict[str, NppesHit] = {}
    for fn, st in [
        (first_name, state),
        (f"{first_name[:3]}*" if len(first_name) >= 3 else first_name, state),
        (first_name, None),
    ]:
        for h in search(fn, last_name, st):
            tried.setdefault(h.npi, h)
        if len(tried) >= 3:
            break
    return list(tried.values())


def lookup(npi: str) -> NppesProvider | None:
    """Fetch the full NPPES registry record for a single NPI.

    Uses the ``number=<npi>`` query parameter — NPPES returns the record
    in ``results[0]`` when the NPI is valid and 0 results otherwise. Any
    HTTP/transport error is swallowed and returns ``None`` (callers doing
    bulk enrichment keep going; a single lookup failure shouldn't abort
    the batch).

    Returns a :class:`NppesProvider` on success, or ``None`` if the NPI
    is unknown or the request fails. See :class:`NppesProvider` for the
    field mapping.
    """
    npi_s = str(npi).strip()
    if not npi_s:
        return None
    params = {
        "version": VERSION,
        "number": npi_s,
    }
    try:
        r = requests.get(API, params=params, timeout=15)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        print(f"  nppes lookup error ({npi_s}): {e}")
        return None
    time.sleep(SLEEP)

    results = payload.get("results") or []
    if not results:
        return None
    result = results[0]

    basic = result.get("basic") or {}
    enumeration_type = result.get("enumeration_type") or ""
    taxonomies = result.get("taxonomies") or []
    primary_tax = next(
        (t for t in taxonomies if t.get("primary")),
        taxonomies[0] if taxonomies else {},
    )
    addresses = result.get("addresses") or []
    # Cap at 5 — most NPIs have at most one practice + one mailing.
    addresses_capped = addresses[:5]

    return NppesProvider(
        npi=str(result.get("number") or npi_s),
        enumeration_type=enumeration_type,
        enumeration_date=basic.get("enumeration_date") or None,
        credential=basic.get("credential") or None,
        sole_proprietor=basic.get("sole_proprietor") or None,
        is_organization=(enumeration_type == "NPI-2"),
        primary_taxonomy_code=(primary_tax.get("code") if primary_tax else None),
        primary_taxonomy_desc=(primary_tax.get("desc") if primary_tax else None),
        taxonomy_count=len(taxonomies),
        last_updated=basic.get("last_updated") or None,
        addresses=addresses_capped,
    )
