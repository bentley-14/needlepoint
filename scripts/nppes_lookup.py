"""Live NPPES (National Plan & Provider Enumeration System) lookup.

CMS publishes a free public API at https://npiregistry.cms.hhs.gov/api/ that
returns real-time NPI registry data including:
  - provider identity (name, credentials, taxonomy)
  - all addresses (primary practice, mailing, secondary locations)
  - sole-proprietor flag, entity type (Individual / Organization)
  - taxonomy + license info (multiple specialties supported)
  - "other identifiers" — Medicare CCN affiliations, Medicaid IDs, etc.
  - enumeration date, last update date

This is the SAME source as our local nppes_bulk.parquet, but the live API
captures updates that may have happened since the bulk snapshot was taken.
Useful for verifying defendant's current entity status, finding affiliated
facility CCNs, and confirming taxonomy changes.

The endpoint is NOT Akamai-protected. Plain HTTP GET works.

Usage:
    python scripts/nppes_lookup.py --npi 1900000085
    python scripts/nppes_lookup.py --npi 1900000085 --raw     # full JSON
    python scripts/nppes_lookup.py --org-name "Larch Medical Clinic"
    python scripts/nppes_lookup.py --last-name Larch --state CA

Returns formatted text by default; --raw dumps the full JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
from difflib import SequenceMatcher
from typing import Any

import requests

NPPES_URL = "https://npiregistry.cms.hhs.gov/api/"
API_VERSION = "2.1"
TIMEOUT_SEC = 30

# Fuzzy first-name matching threshold (difflib ratio). 0.72 catches real
# spelling variants/typos — Emily↔Emilee (0.73), Michael↔Micheal (0.86),
# Steven↔Stephen (0.77) — while rejecting clearly-different names like
# Jon↔Ron (0.67) and Kathryn↔Catherine (0.62). It DOES still admit a few
# genuinely-different-but-similar names (John↔Joan, Mary↔Gary ≈ 0.75) — no
# string metric separates those from real variants — so fuzzy matches are
# RETURNED BUT FLAGGED (never treated as exact); the last-name (exact) +
# state + specialty narrowing on the query keeps the practical false-positive
# rate low, and the caller is told to verify identity by specialty/location.
FUZZY_FIRST_NAME_THRESHOLD = 0.72


def _first_name_strength(query: str, candidate: str) -> str | None:
    """Match strength of NPPES first name ``candidate`` vs requested ``query``.

    Returns ``"exact"`` | ``"prefix"`` | ``"fuzzy"`` | ``None`` (no match).
    ``prefix`` (either direction) covers nicknames/abbreviations
    (Rob↔Robert, Will↔William, Emil↔Emilee); ``fuzzy`` covers spelling
    variants/typos above :data:`FUZZY_FIRST_NAME_THRESHOLD`.
    """
    q = (query or "").upper().strip()
    c = (candidate or "").upper().strip()
    if not q or not c:
        return None
    if q == c:
        return "exact"
    if c.startswith(q) or q.startswith(c):
        return "prefix"
    if SequenceMatcher(None, q, c).ratio() >= FUZZY_FIRST_NAME_THRESHOLD:
        return "fuzzy"
    return None


def _query(params: dict[str, Any]) -> dict:
    """Hit the NPPES API. Returns the parsed JSON or empty result_count=0 on error."""
    full = {"version": API_VERSION, **params}
    try:
        r = requests.get(NPPES_URL, params=full, timeout=TIMEOUT_SEC,
                         headers={"User-Agent": "fraud-investigator/1.0"})
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        return {"result_count": 0, "_error": f"HTTP {e.response.status_code}"}
    except requests.RequestException as e:
        return {"result_count": 0, "_error": str(e)}


def _format_provider(p: dict) -> str:
    """Pretty-print one NPPES record."""
    out: list[str] = []
    out.append(f"NPI: {p.get('number')}")
    enum = p.get("enumeration_type")
    out.append(f"Entity: {enum} ({'Individual' if enum == 'NPI-1' else 'Organization' if enum == 'NPI-2' else '?'})")

    basic = p.get("basic", {}) or {}
    name = " ".join(filter(None, [
        basic.get("first_name"),
        basic.get("middle_name"),
        basic.get("last_name") or basic.get("organization_name"),
    ]))
    creds = basic.get("credential") or ""
    out.append(f"Name: {name} {creds}".strip())
    if basic.get("sole_proprietor"):
        out.append(f"Sole proprietor: {basic.get('sole_proprietor')}")
    out.append(f"Enumeration date: {basic.get('enumeration_date')}, last_updated: {basic.get('last_updated')}")
    if basic.get("status"):
        out.append(f"Status: {basic.get('status')}")
    if basic.get("deactivation_date"):
        out.append(f"DEACTIVATED: {basic.get('deactivation_date')} reason={basic.get('deactivation_reason_code')}")

    # Taxonomy
    taxonomies = p.get("taxonomies") or []
    if taxonomies:
        out.append("Taxonomies:")
        for t in taxonomies:
            primary = " (PRIMARY)" if t.get("primary") else ""
            out.append(f"  {t.get('code')} {t.get('desc')}{primary} "
                       f"license={t.get('license') or '-'} state={t.get('state') or '-'}")

    # Addresses
    addrs = p.get("addresses") or []
    if addrs:
        out.append("Addresses:")
        for a in addrs:
            purpose = a.get("address_purpose") or "?"
            line = ", ".join(filter(None, [
                a.get("address_1"), a.get("address_2"),
                a.get("city"), a.get("state"), a.get("postal_code"),
            ]))
            phone = a.get("telephone_number") or ""
            out.append(f"  [{purpose}] {line} {('☏ ' + phone) if phone else ''}")

    # Practice locations
    locs = p.get("practiceLocations") or []
    if locs:
        out.append(f"Practice locations: {len(locs)}")
        for l in locs[:5]:
            line = ", ".join(filter(None, [
                l.get("address_1"), l.get("city"), l.get("state"), l.get("postal_code"),
            ]))
            out.append(f"  {line}")

    # Other identifiers — Medicaid IDs, CCN affiliations
    others = p.get("identifiers") or []
    if others:
        out.append("Other identifiers:")
        for o in others:
            out.append(f"  [{o.get('desc') or o.get('code') or '?'}] {o.get('identifier')} "
                       f"state={o.get('state') or '-'} issuer={o.get('issuer') or '-'}")

    return "\n".join(out)


def cmd_npi(npi: str, raw: bool) -> int:
    res = _query({"number": npi})
    if raw:
        print(json.dumps(res, indent=2))
        return 0
    if res.get("_error"):
        print(f"ERROR: {res['_error']}", file=sys.stderr)
        return 1
    results = res.get("results") or []
    if not results:
        print(f"NPI {npi} not found")
        return 1
    for p in results:
        print(_format_provider(p))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"limit": args.limit}
    if args.last_name: params["last_name"] = args.last_name
    if args.org_name: params["organization_name"] = args.org_name
    if args.state: params["state"] = args.state
    if args.taxonomy: params["taxonomy_description"] = args.taxonomy
    if args.postal_code: params["postal_code"] = args.postal_code
    # `first_name` is matched CLIENT-SIDE (fuzzy), NOT sent to the API: the
    # NPPES server-side first_name filter is exact, so asking "Emily" never
    # returns "Emilee". We fetch by last_name/state/taxonomy/postal and
    # fuzzy-match first_name locally (see _first_name_strength). Exception:
    # if first_name is the ONLY criterion, we must send it or the query is
    # unbounded — fuzzy can't help that degenerate case anyway.
    if args.first_name and not (args.last_name or args.org_name or args.state or args.postal_code):
        params["first_name"] = args.first_name

    res = _query(params)
    if args.raw:
        print(json.dumps(res, indent=2))
        return 0
    if res.get("_error"):
        print(f"ERROR: {res['_error']}", file=sys.stderr)
        return 1
    results = res.get("results") or []
    # NPPES API quirk: when a name query matches >200 records, the API silently
    # DROPS the name filter and returns alphabetical state-only matches (verified
    # 2026-05-29: last_name=Patel returns Ahuja/Amin/...). Post-filter client-side
    # to the requested name so we never emit wrong-name providers, and warn so the
    # caller narrows the query (add --first-name).
    if args.last_name or args.first_name:
        matched = []
        for p in results:
            b = p.get("basic", {})
            if args.last_name and (b.get("last_name") or "").upper() != args.last_name.upper():
                continue
            if args.first_name:
                strength = _first_name_strength(args.first_name, b.get("first_name") or "")
                if strength is None:
                    continue
                p["_first_name_match"] = strength  # exact | prefix | fuzzy
            matched.append(p)
        if results and not matched:
            print(f"WARNING: NPPES returned {res.get('result_count', 0)} results but NONE match the "
                  f"requested name — the API likely dropped an over-broad name filter (>200 matches). "
                  f"Narrow the query: add --first-name (and/or --state, --postal-code).",
                  file=sys.stderr)
        results = matched
    print(f"Found {len(results)} name-matched (NPPES API returned {res.get('result_count', 0)}):\n")
    for p in results:
        if p.get("_first_name_match") == "fuzzy":
            b = p.get("basic", {})
            print(f"⚠ FUZZY first-name match: requested {args.first_name!r} ≈ NPPES "
                  f"{b.get('first_name')!r} — spelling variant, NOT exact. Verify identity via "
                  f"specialty / location / billing surface before trusting this NPI.")
        print(_format_provider(p))
        print("---")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Live NPPES NPI registry lookup")
    ap.add_argument("--npi", type=str, help="Exact NPI lookup (10-digit)")
    ap.add_argument("--first-name", type=str)
    ap.add_argument("--last-name", type=str)
    ap.add_argument("--org-name", type=str)
    ap.add_argument("--state", type=str, help="2-letter state code")
    ap.add_argument("--taxonomy", type=str, help="Taxonomy description (substring match)")
    ap.add_argument("--postal-code", type=str)
    ap.add_argument("--limit", type=int, default=20, help="Max results (NPPES caps at 200)")
    ap.add_argument("--raw", action="store_true", help="Dump full JSON response")
    args = ap.parse_args(argv)

    if args.npi:
        return cmd_npi(args.npi, args.raw)
    if any([args.first_name, args.last_name, args.org_name, args.state, args.taxonomy, args.postal_code]):
        return cmd_search(args)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
