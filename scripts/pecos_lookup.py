"""PECOS (Provider Enrollment, Chain, and Ownership System) lookup — LIVE API.

CMS exposes the public Medicare Fee-for-Service Provider Enrollment dataset
as a JSON API. We hit it directly for per-NPI and per-specialty/state lookups,
defeating the "NPPES sweep is not equivalent to PECOS" defense at investigation
time (no DOJ subpoena required).

Dataset:
  https://data.cms.gov/provider-characteristics/medicare-provider-supplier-enrollment/medicare-fee-for-service-public-provider-enrollment

API endpoint:
  https://data.cms.gov/data-api/v1/dataset/2457ea29-fc82-48b0-86ec-3b0755de7515/data

Fields per row (one row per ENROLLMENT — a single NPI with multi-state
or multi-specialty enrollment will have multiple rows):

  - NPI                   — 10-digit NPI
  - MULTIPLE_NPI_FLAG     — Y/N
  - PECOS_ASCT_CNTL_ID    — PECOS associate control ID
  - ENRLMT_ID             — enrollment ID (e.g., "I20031204000132"; date-encoded)
  - PROVIDER_TYPE_CD      — code (e.g., "14-02" for GENERAL SURGERY)
  - PROVIDER_TYPE_DESC    — description (e.g., "PRACTITIONER - GENERAL SURGERY")
  - STATE_CD              — 2-letter state where enrolled
  - FIRST_NAME, MDL_NAME, LAST_NAME, ORG_NAME

Filter syntax: ``filter[FIELD]=value`` for exact match; ``keyword=X`` for
substring across all text fields.

NO API key required. No rate limit documented; be reasonable (default 10s
timeout, retry on 5xx).

Usage::

    # Per-NPI enrollment status (the key question: enrolled as what?)
    python scripts/pecos_lookup.py --npi 1900000002

    # All enrollments for an NPI (multi-state, multi-specialty)
    python scripts/pecos_lookup.py --npi 1900000002 --raw

    # Find PECOS-enrolled psychologists in PA — defeats out-of-state
    # telehealth-supervisor defense by enumerating who is actually enrolled
    python scripts/pecos_lookup.py --keyword PSYCHOLOGIST --state PA

    # Reassigned-provider count for an ORG NPI — the workforce-capacity
    # denominator that bounds a staffing-impossibility theory
    python scripts/pecos_lookup.py --reassignments 1900000056

    # Cross-check: is any psychologist enrolled in PA whose last name matches
    # a person Mulberry might be using as a supervisor?
    python scripts/pecos_lookup.py --keyword PSYCHOLOGIST --state PA --last-name SMITH

    # Specialty + last-name within state (find a specific suspected supervisor)
    python scripts/pecos_lookup.py --specialty "CLINICAL PSYCHOLOGIST" --state PA

The third form is the key new capability: "find ALL PECOS-enrolled
psychologists in PA" — which directly answers the supervisor-required
defense (no need for "out-of-state telehealth" since we enumerate).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import requests


PECOS_API_URL = (
    "https://data.cms.gov/data-api/v1/dataset/"
    "2457ea29-fc82-48b0-86ec-3b0755de7515/data"
)
# CMS "Revalidation Clinic Group Practice Reassignment" — the providers who
# reassign Medicare billing to a group, with the group's reassignment count.
# This UUID is the CMS-maintained "latest" alias (DCAT description="latest"),
# so it tracks the current monthly release. To refresh if CMS ever rotates it:
#   python -c "import json,urllib.request as u; \
#     [print(d['distribution'][0]['accessURL']) for d in \
#      json.load(u.urlopen('https://data.cms.gov/data.json'))['dataset'] \
#      if d['title']=='Revalidation Clinic Group Practice Reassignment']"
REASSIGNMENT_API_URL = (
    "https://data.cms.gov/data-api/v1/dataset/"
    "e1f1fa9a-d6b4-417e-948a-c72dead8a41c/data"
)
DATASET_LANDING = (
    "https://data.cms.gov/provider-characteristics/"
    "medicare-provider-supplier-enrollment/"
    "medicare-fee-for-service-public-provider-enrollment"
)
TIMEOUT_SEC = 30
USER_AGENT = "fraud-investigator/1.0 (PECOS lookup; reproducible citation)"


def _api_call(params: list[tuple[str, str]], retries: int = 2,
              url: str = PECOS_API_URL) -> list[dict] | dict:
    """Hit a CMS data-api dataset with retries. Returns parsed JSON or error dict."""
    backoff = 1
    for attempt in range(retries + 1):
        try:
            r = requests.get(
                url,
                params=params,
                timeout=TIMEOUT_SEC,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    return {"_error": "non-JSON response", "_status": 200,
                            "_body": r.text[:500]}
            elif r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            else:
                return {"_error": f"HTTP {r.status_code}", "_body": r.text[:500]}
        except requests.RequestException as e:
            if attempt < retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            return {"_error": f"request failed: {e}"}
    return {"_error": "exhausted retries"}


def lookup_npi(npi: str) -> list[dict]:
    """Return ALL PECOS enrollment rows for one NPI.

    A single NPI can have multiple enrollment rows (one per state or per
    specialty). Returning the full list lets the caller see the entire
    enrollment footprint.
    """
    rows = _api_call([("filter[NPI]", npi)])
    if isinstance(rows, dict) and "_error" in rows:
        return [rows]   # propagate error in list form
    return rows if isinstance(rows, list) else []


def lookup_reassignments(org_npi: str) -> dict[str, Any]:
    """For an ORGANIZATION NPI, return the providers who reassign Medicare
    billing rights to it (CMS Revalidation Clinic Group Practice Reassignment).

    This is the AUTHORITATIVE workforce-capacity number — e.g. "49 reassigned
    providers" — that bounds a staffing-impossibility argument: a count alleging
    that billed clinical-staff time exceeds what the entity could furnish MUST be
    measured against this denominator, not an assumed solo/small practice. (The
    reassignment dataset keys groups by Group Enrollment ID, not NPI, so the org
    NPI is first resolved to its O-prefixed enrollment id via the PECOS enrollment
    dataset, then the reassignment list is filtered on it.)

    Returns: {org_npi, reassigned_provider_count, group_legal_business_name,
    group_pac_id, group_state, enrollment_ids[], individuals[{npi,name,specialty}],
    record_types{}} or {_error,...}."""
    enr = lookup_npi(org_npi)
    if enr and isinstance(enr[0], dict) and "_error" in enr[0]:
        return {"org_npi": org_npi, "_error": enr[0]["_error"]}
    eids = sorted({r.get("ENRLMT_ID") for r in enr
                   if str(r.get("ENRLMT_ID", "")).startswith("O")})
    if not eids:
        return {"org_npi": org_npi, "reassigned_provider_count": 0,
                "enrollment_ids": [], "individuals": [],
                "note": ("no organizational (O-prefixed) PECOS enrollment found for this "
                         "NPI — it may be an individual NPI, or not PECOS-enrolled as a group")}
    individuals: dict[str, dict] = {}
    record_types: dict[str, int] = {}
    group_meta: dict[str, Any] = {}
    count_field: int | None = None
    for eid in eids:
        rows = _api_call([("filter[Group Enrollment ID]", eid), ("size", "5000")],
                         url=REASSIGNMENT_API_URL)
        if isinstance(rows, dict):
            return {"org_npi": org_npi, "enrollment_ids": eids,
                    "_error": rows.get("_error", "reassignment API error")}
        for r in rows:
            rt = r.get("Record Type", "")
            record_types[rt] = record_types.get(rt, 0) + 1
            inpi = r.get("Individual NPI")
            if inpi and inpi not in individuals:
                individuals[inpi] = {
                    "npi": inpi,
                    "name": " ".join(filter(None, [r.get("Individual First Name", ""),
                                                   r.get("Individual Last Name", "")])).strip(),
                    "specialty": r.get("Individual Specialty Description", ""),
                    "record_type": rt,
                }
            if not group_meta and r.get("Group Legal Business Name"):
                group_meta = {
                    "group_legal_business_name": r.get("Group Legal Business Name"),
                    "group_pac_id": r.get("Group PAC ID"),
                    "group_state": r.get("Group State Code"),
                }
            cf = r.get("Group Reassignments and Physician Assistants")
            if cf not in (None, ""):
                try:
                    count_field = int(cf)
                except (TypeError, ValueError):
                    pass
    return {
        "org_npi": org_npi,
        "enrollment_ids": eids,
        # CMS's own per-group count field; fall back to distinct individuals seen
        "reassigned_provider_count": count_field if count_field is not None else len(individuals),
        "distinct_individuals_seen": len(individuals),
        "record_types": record_types,
        "individuals": sorted(individuals.values(), key=lambda x: x["npi"]),
        **group_meta,
    }


def find_by_specialty_in_state(specialty_keyword: str, state: str | None = None,
                                last_name: str | None = None,
                                size: int = 100) -> list[dict]:
    """Find PECOS-enrolled providers by substring match on specialty + state.

    ``specialty_keyword`` matches via the API's ``keyword=`` parameter, which
    is a substring search across PROVIDER_TYPE_DESC + name fields. State is
    an exact filter on STATE_CD; last_name is exact (case-sensitive in the API).
    """
    params: list[tuple[str, str]] = [("keyword", specialty_keyword),
                                      ("size", str(size))]
    if state:
        params.append(("filter[STATE_CD]", state.upper()))
    if last_name:
        params.append(("filter[LAST_NAME]", last_name.upper()))
    rows = _api_call(params)
    if isinstance(rows, dict) and "_error" in rows:
        return [rows]
    return rows if isinstance(rows, list) else []


def _format_enrollment_row(r: dict) -> str:
    enrlmt = r.get("ENRLMT_ID", "")
    # Encode the enrollment date from the ID prefix (I + YYYYMMDD + sequence)
    enrolled_date = "—"
    if enrlmt.startswith("I") and len(enrlmt) >= 9 and enrlmt[1:9].isdigit():
        d = enrlmt[1:9]
        enrolled_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    name = " ".join(filter(None, [
        r.get("FIRST_NAME", ""),
        r.get("MDL_NAME", ""),
        r.get("LAST_NAME", ""),
    ])).strip() or r.get("ORG_NAME", "—")
    return (
        f"  NPI {r.get('NPI'):>10} · {r.get('STATE_CD','??')} · "
        f"{r.get('PROVIDER_TYPE_DESC','—')} "
        f"({r.get('PROVIDER_TYPE_CD','—')}) · "
        f"enrolled {enrolled_date} · {name} "
        f"[enrlmt {enrlmt}]"
    )


def summarize_enrollment(rows: list[dict]) -> dict[str, Any]:
    """Aggregate per-NPI enrollment rows into a structured summary."""
    if not rows or (rows and "_error" in rows[0]):
        return {"enrolled": False, "rows": rows or []}
    npis = {r.get("NPI") for r in rows}
    states = {r.get("STATE_CD") for r in rows if r.get("STATE_CD")}
    specialties = {r.get("PROVIDER_TYPE_DESC") for r in rows
                   if r.get("PROVIDER_TYPE_DESC")}
    # Earliest enrollment per ID prefix date
    earliest = None
    for r in rows:
        eid = r.get("ENRLMT_ID", "")
        if eid.startswith("I") and len(eid) >= 9 and eid[1:9].isdigit():
            d = eid[1:9]
            if earliest is None or d < earliest:
                earliest = d
    earliest_str = (f"{earliest[:4]}-{earliest[4:6]}-{earliest[6:8]}"
                    if earliest else "—")
    return {
        "enrolled": True,
        "npi_count": len(npis),
        "enrollment_row_count": len(rows),
        "states_enrolled": sorted(states),
        "specialties_enrolled": sorted(specialties),
        "earliest_enrollment_date": earliest_str,
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"\nDataset landing page: {DATASET_LANDING}\n",
    )
    ap.add_argument("--npi",
                    help="Look up all enrollments for one NPI (10 digits)")
    ap.add_argument("--reassignments",
                    metavar="ORG_NPI",
                    help="For an ORG NPI: providers who reassign Medicare billing to it "
                         "+ the reassignment count (workforce-capacity denominator)")
    ap.add_argument("--keyword",
                    help="Substring search across specialty + name fields "
                         "(e.g., PSYCHOLOGIST, NEUROPSYCH, HOSPICE)")
    ap.add_argument("--specialty",
                    help="Alias for --keyword (matches PROVIDER_TYPE_DESC text)")
    ap.add_argument("--state", help="2-letter state code (exact match)")
    ap.add_argument("--last-name", help="Filter by exact LAST_NAME")
    ap.add_argument("--size", type=int, default=100,
                    help="Max rows for keyword/specialty searches (default 100)")
    ap.add_argument("--raw", action="store_true", help="JSON output")
    args = ap.parse_args(argv)

    if not (args.npi or args.reassignments or args.keyword or args.specialty):
        ap.error("Must provide one of: --npi, --reassignments, --keyword, --specialty")

    # ── Reassignment / workforce-capacity lookup (org NPI) ──────────
    if args.reassignments:
        res = lookup_reassignments(args.reassignments)
        if args.raw:
            print(json.dumps(res, indent=2))
            return 0
        if res.get("_error"):
            print(f"PECOS reassignment lookup error: {res['_error']}", file=sys.stderr)
            return 1
        n = res.get("reassigned_provider_count", 0)
        if not res.get("enrollment_ids"):
            print(f"ORG NPI {args.reassignments}: {res.get('note', 'no group enrollment found')}")
            return 0
        print(f"ORG NPI {args.reassignments}"
              + (f" — {res.get('group_legal_business_name')}" if res.get('group_legal_business_name') else "")
              + (f" (PAC {res.get('group_pac_id')}, {res.get('group_state')})" if res.get('group_pac_id') else ""))
        print(f"  Reassigned providers (workforce-capacity denominator): {n}")
        print(f"  Record types: {res.get('record_types', {})}")
        print(f"  Group enrollment id(s): {', '.join(res.get('enrollment_ids', []))}")
        print(f"\n  ⚠ A staffing-IMPOSSIBILITY count must measure billed clinical-staff "
              f"time against THIS workforce, not an assumed solo/small practice.")
        inds = res.get("individuals", [])
        if inds:
            print(f"\n  Reassigning providers ({len(inds)} distinct):")
            for r in inds[:60]:
                print(f"    NPI {r['npi']:>10} · {r.get('specialty','—')} · {r.get('name','')}")
            if len(inds) > 60:
                print(f"    … +{len(inds) - 60} more")
        print(f"\n  data: CMS Revalidation Clinic Group Practice Reassignment "
              f"(latest), filtered by Group Enrollment ID")
        print(f"  Reproducible: python scripts/pecos_lookup.py --reassignments {args.reassignments}")
        return 0

    # ── Per-NPI lookup ──────────────────────────────────────────────
    if args.npi:
        rows = lookup_npi(args.npi)
        summary = summarize_enrollment(rows)
        if args.raw:
            print(json.dumps({"npi": args.npi, **summary}, indent=2))
            return 0

        if not summary.get("enrolled"):
            print(f"NPI {args.npi}: NOT ENROLLED in PECOS")
            print(f"  This is a STRONG signal that the provider is not "
                  f"authorized to bill Medicare FFS.")
            err = (rows[0] if rows and isinstance(rows[0], dict) else {}).get("_error")
            if err:
                print(f"  (API note: {err})")
            print(f"\nVerify at: {DATASET_LANDING}")
            return 0

        print(f"NPI {args.npi}: ENROLLED in PECOS")
        print(f"  {summary['enrollment_row_count']} enrollment record(s) "
              f"across {len(summary['states_enrolled'])} state(s)")
        print(f"  States:      {', '.join(summary['states_enrolled'])}")
        print(f"  Specialties: {', '.join(summary['specialties_enrolled'])}")
        print(f"  Earliest enrollment: {summary['earliest_enrollment_date']}")
        print()
        print("Enrollment records:")
        for r in summary["rows"]:
            print(_format_enrollment_row(r))
        print(f"\nReproducible citation:")
        print(f"  python scripts/pecos_lookup.py --npi {args.npi}")
        print(f"  data: CMS PECOS public API "
              f"({PECOS_API_URL}?filter[NPI]={args.npi})")
        return 0

    # ── Specialty / keyword search ──────────────────────────────────
    keyword = args.keyword or args.specialty
    rows = find_by_specialty_in_state(keyword, args.state, args.last_name,
                                       args.size)

    if args.raw:
        print(json.dumps(rows, indent=2))
        return 0

    if rows and isinstance(rows[0], dict) and "_error" in rows[0]:
        print(f"API error: {rows[0]['_error']}", file=sys.stderr)
        return 1
    if not rows:
        print(f"NO PECOS-enrolled providers match keyword='{keyword}'"
              + (f", state={args.state}" if args.state else "")
              + (f", last_name={args.last_name}" if args.last_name else ""))
        print(f"\nThis exhaustive enumeration is dispositive when the only "
              f"defense is 'an unidentified out-of-state PECOS-enrolled "
              f"supervisor': IF the relevant state (or specialty) returns "
              f"zero matches, the defense is foreclosed.")
        return 0
    print(f"PECOS-enrolled providers matching keyword='{keyword}'"
          + (f", state={args.state}" if args.state else "")
          + (f", last_name={args.last_name}" if args.last_name else "")
          + f": {len(rows)} row(s)")
    print()
    # Group by NPI for readability
    by_npi: dict[str, list[dict]] = {}
    for r in rows:
        by_npi.setdefault(r.get("NPI", ""), []).append(r)
    for npi, items in sorted(by_npi.items()):
        print(f"NPI {npi}:")
        for r in items:
            print(_format_enrollment_row(r))
    print(f"\nTotal unique NPIs: {len(by_npi)}")
    print(f"Reproducible: python scripts/pecos_lookup.py --keyword \"{keyword}\""
          + (f" --state {args.state}" if args.state else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
