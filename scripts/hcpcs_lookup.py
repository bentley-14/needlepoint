#!/usr/bin/env python3
"""HCPCS / CPT ground-truth lookup.

Resolve a code to its authoritative CMS descriptor, drug-vs-procedure flag, and
billing unit so you state what a code IS instead of guessing. Reads
data/ground_truth/hcpcs_codes.parquet (build: scripts/ingest/build_hcpcs_reference.py).

Usage:
  python scripts/hcpcs_lookup.py --code J3304
  python scripts/hcpcs_lookup.py --codes J3304,J7322,20610        # batch
  python scripts/hcpcs_lookup.py --search triamcinolone           # by descriptor
  python scripts/hcpcs_lookup.py --drug euflexxa                  # Part D brand/generic
  python scripts/hcpcs_lookup.py --code J3304 --format json

Why this matters (the failure it prevents):
  - J3304 is *triamcinolone* (per 1 mg), NOT methylprednisolone.
  - J7322 is *Hymovis*; Euflexxa is J7323 (per dose).
  - "85 services" of a per-1mg drug code is ~85 MILLIGRAMS, not 85 injections.
    Injection COUNT comes from the procedure codes (20610/20611 = 'per service').
  Resolve every code here before pleading a drug name or an injection frequency.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repo root, so fraudlib imports

import argparse
import json
import sys
from pathlib import Path

import duckdb

try:
    from fraudlib import r2
except Exception:  # calibration jail / standalone copy: no fraudlib → local-only, never R2
    from pathlib import Path as _RP
    class _LocalR2:
        R2_PANELS = ""
        @staticmethod
        def parquet_source(local, key): return str(local)
        @staticmethod
        def fetch_local(local, key): return _RP(local)
        @staticmethod
        def attach_r2(con): pass
        @staticmethod
        def duckdb_r2():
            import duckdb; return duckdb.connect()
    r2 = _LocalR2()

REPO = Path(__file__).resolve().parents[1]
_GT = "r2:needlepoint-data/ground_truth/v1"
HCPCS = REPO / "data" / "ground_truth" / "hcpcs_codes.parquet"
HCPCS_BY_YEAR = REPO / "data" / "ground_truth" / "hcpcs_codes_by_year.parquet"
HCPCS_LEVEL2_FULL = REPO / "data" / "ground_truth" / "hcpcs_level2_full.parquet"
DRUGS = REPO / "data" / "ground_truth" / "partd_drugs.parquet"

_DRUG_UNIT_HINTS = ("mg", "mcg", "ml", "iu", "gm", "unit", "meq", "per dose", "cc")


def _require(path: Path) -> None:
    # With R2 fallback, local absence is OK — parquet_source will resolve to R2.
    pass


def _con():
    return r2.duckdb_r2()


def _rows(sql: str, params: list) -> list[dict]:
    return _con().execute(sql, params).df().to_dict(orient="records")


def _caution(r: dict) -> str | None:
    if not r.get("is_drug"):
        return None
    unit = (r.get("unit_basis") or "").lower()
    if any(h in unit for h in _DRUG_UNIT_HINTS) and unit != "per service":
        return (f"per-unit drug code: 'services' are billed in '{r['unit_basis']}', NOT injections. "
                f"Injection/procedure COUNT comes from the procedure codes (e.g. 20610/20611).")
    return None


import re as _re

_OVERRIDES_PATH = REPO / "scripts" / "ingest" / "hcpcs_authoritative_overrides.json"
try:
    _HCPCS_OVERRIDES = {k.upper(): v for k, v in
                        (json.load(open(_OVERRIDES_PATH)).get("codes") or {}).items()}
except Exception:
    _HCPCS_OVERRIDES = {}


def _apply_override(r: dict) -> dict:
    """Authoritative-constant guardrail. The CMS PUF descriptor is a denormalized copy
    that can be WRONG (e.g. 99489 flipped 30->60 min in 2021-2024). For codes in the
    curated override table (scripts/ingest/hcpcs_authoritative_overrides.json), parse the
    PUF descriptor's minutes and cross-check against the AMA-CPT-authoritative value; on
    conflict, swap in the authoritative descriptor and attach a hard warning. Never let a
    high-stakes unit->time/$ constant ride on the unverified PUF copy."""
    ov = _HCPCS_OVERRIDES.get(str(r.get("code", "")).upper())
    if not ov:
        return r
    r = dict(r)
    auth_min = ov.get("minutes_per_unit")
    m = _re.search(r"(\d+)\s*minute", (r.get("descriptor") or "").lower())
    puf_min = int(m.group(1)) if m else None
    r["authoritative_minutes_per_unit"] = auth_min
    if auth_min is not None and puf_min is not None and puf_min != auth_min:
        r["puf_descriptor"] = r.get("descriptor")
        r["descriptor"] = ov.get("authoritative_descriptor", r.get("descriptor"))
        r["_auth_note"] = (f"AUTHORITATIVE OVERRIDE — CMS PUF descriptor says {puf_min} min, but the "
                           f"AMA-CPT-authoritative value is {auth_min} min ({ov.get('basis', 'AMA CPT')}). "
                           f"Using {auth_min} min; the PUF copy is wrong for this code/year. Any "
                           f"unit->time/$/FTE math MUST use {auth_min} min.")
    return r


def _minutes_from_descriptor(desc: str | None) -> int | None:
    """Parse a single 'NN minute(s)' quantity out of a descriptor, else None."""
    if not desc:
        return None
    m = _re.search(r"(\d+)\s*minute", str(desc).lower())
    return int(m.group(1)) if m else None


def canonical_minutes_per_unit(code: str, year: int | None = None) -> dict | None:
    """SINGLE SOURCE OF TRUTH for unit->time math.

    Resolves the authoritative minutes-per-billed-unit for one time-based
    HCPCS/CPT code, in priority order:
      1. authoritative_override  — curated AMA-CPT constant (hcpcs_authoritative_overrides.json)
      2. cms_mpfs_rvu            — canonical CMS MPFS RVU short descriptor (year-specific)
      3. cms_partb_puf           — CMS Part B PUF descriptor (last resort)

    Both the display layer (fmt_*) and the Guardrail-2 numeric auditor resolve
    minutes through THIS function, so there is exactly one place the constant is
    decided — the fix for the 99489 (60-vs-30 min) class of error.

    Returns {code, year, minutes_per_unit, source, basis, descriptor} or None
    when the code carries no parseable time quantity (e.g. a drug/procedure code)."""
    code = str(code).strip().upper()
    ov = _HCPCS_OVERRIDES.get(code)
    if ov and ov.get("minutes_per_unit") is not None:
        return {"code": code, "year": year,
                "minutes_per_unit": int(ov["minutes_per_unit"]),
                "source": "authoritative_override",
                "basis": ov.get("basis", "AMA CPT"),
                "descriptor": ov.get("authoritative_descriptor", "")}
    if year is not None:
        src = r2.parquet_source(HCPCS_BY_YEAR, f"{_GT}/{HCPCS_BY_YEAR.name}")
        rows = _rows("SELECT code,year,descriptor,canonical_short_descriptor,descriptor_source "
                     f"FROM read_parquet('{src}') WHERE upper(code)=? AND year=?", [code, year])
    else:
        src = r2.parquet_source(HCPCS, f"{_GT}/{HCPCS.name}")
        rows = _rows("SELECT code,descriptor,canonical_short_descriptor,descriptor_source "
                     f"FROM read_parquet('{src}') WHERE upper(code)=?", [code])
    if not rows:
        return None
    r = _apply_override(rows[0])
    for field, source in (("canonical_short_descriptor", "cms_mpfs_rvu"),
                          ("descriptor", r.get("descriptor_source") or "cms_partb_puf")):
        mins = _minutes_from_descriptor(r.get(field))
        if mins is not None:
            return {"code": code, "year": year, "minutes_per_unit": mins,
                    "source": source, "basis": "", "descriptor": r.get(field)}
    return None


def resolve_code(code: str) -> dict | None:
    """SINGLE SOURCE OF TRUTH for "what IS this code" — in-process, importable
    (no subprocess). Part B first (rich: descriptor + is_drug + unit_basis),
    falling back to the full HCPCS Level II reference for codes never billed
    under Part B in our data. Returns None if the code is unrecognized by
    either source. Used by tier_lint.lint_code_identity_claims (Guardrail:
    code-identity verification) to check an evidence item's HCPCS/drug-code
    identity claim against ground truth."""
    code = code.strip().upper()
    try:
        hcpcs_src = r2.parquet_source(HCPCS, f"{_GT}/{HCPCS.name}")
        rows = _rows(f"SELECT * FROM read_parquet('{hcpcs_src}') WHERE upper(code)=?", [code])
        if rows:
            return _apply_override(rows[0])
        if HCPCS_LEVEL2_FULL.exists():
            hcpcs_level2_src = r2.parquet_source(HCPCS_LEVEL2_FULL, f"{_GT}/{HCPCS_LEVEL2_FULL.name}")
            rows = _rows(f"SELECT * FROM read_parquet('{hcpcs_level2_src}') WHERE upper(code)=?", [code])
            if rows:
                return rows[0]
    except Exception:
        # Unrecognized-code lookup must never raise — an absent/unreachable
        # reference source is indistinguishable from "code not found" for the
        # caller's purposes (lint flags it as unresolvable either way).
        return None
    return None


def fmt_text(r: dict) -> str:
    r = _apply_override(r)
    tag = "drug" if r.get("is_drug") else "procedure/other"
    head = f"{r['code']}  [{tag}]  unit: {r['unit_basis']}  ·  {r['code_type']}"
    lines = [head, f"  {r['descriptor']}"]
    if r.get("canonical_short_descriptor"):
        lines.append(f"  ✓ canonical (CMS MPFS RVU): {r['canonical_short_descriptor']}")
    elif r.get("descriptor_source") == "cms_partb_puf":
        lines.append("  ⓘ descriptor source: CMS Part B PUF (no MPFS RVU match for this code/year)")
    lines.append(f"  billed {r['first_year']}-{r['last_year']} ({r['n_years']} yr) · "
                 f"{int(r['total_services']):,} services · {int(r['total_beneficiaries']):,} beneficiaries")
    c = _caution(r)
    if c:
        lines.append(f"  ⚠ {c}")
    if r.get("varies_by_year"):
        lines.append(f"  ⚠ identity VARIES across years ({r.get('n_distinct_descriptors')} descriptors / "
                     f"{r.get('n_distinct_units')} units) — for a claim in year Y use "
                     f"`--code {r['code']} --year Y` (or --history); the line above is most-recent.")
    if r.get("_auth_note"):
        lines.append(f"  🛑 {r['_auth_note']}")
    return "\n".join(lines)


def fmt_year(r: dict) -> str:
    r = _apply_override(r)
    tag = "drug" if r.get("is_drug") else "procedure/other"
    out = (f"{r['code']} [{r['year']}]  [{tag}]  unit: {r['unit_basis']}  ·  {r['code_type']}\n"
           f"  {r['descriptor']}")
    if r.get("canonical_short_descriptor"):
        out += f"\n  ✓ canonical (CMS MPFS RVU): {r['canonical_short_descriptor']}"
    elif r.get("descriptor_source") == "cms_partb_puf":
        out += "\n  ⓘ descriptor source: CMS Part B PUF (no MPFS RVU match for this code/year)"
    out += f"\n  {int(r['total_services']):,} services · {int(r['total_beneficiaries']):,} beneficiaries"
    if r.get("_auth_note"):
        out += f"\n  🛑 {r['_auth_note']}"
    return out


def fmt_full(r: dict) -> str:
    """Format a code resolved only from the full HCPCS Level II release."""
    tag = "drug" if r.get("is_drug") else "procedure/other"
    return (f"{r['code']}  [{tag}]  unit: {r['unit_basis']}  ·  {r['code_type']}\n"
            f"  {r['descriptor']}\n"
            f"  ⓘ HCPCS Level II reference ({r.get('release')}) — not billed under Part B in our data "
            f"(identity only; no prevalence / per-year).")


def main() -> int:
    ap = argparse.ArgumentParser(description="HCPCS/CPT ground-truth lookup")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--code", help="single HCPCS/CPT code, e.g. J3304")
    g.add_argument("--codes", help="comma-separated codes, e.g. J3304,J7322,20610")
    g.add_argument("--search", help="substring search over the CMS descriptor")
    g.add_argument("--drug", help="substring search over Part D brand/generic names")
    ap.add_argument("--year", type=int, help="resolve --code/--codes AS OF this year (per-year history)")
    ap.add_argument("--history", action="store_true", help="show every year for --code/--codes")
    ap.add_argument("--format", choices=["text", "json", "tsv"], default="text")
    ap.add_argument("--limit", type=int, default=50, help="max rows for --search/--drug")
    args = ap.parse_args()

    drugs_src = r2.parquet_source(DRUGS, f"{_GT}/{DRUGS.name}")
    hcpcs_src = r2.parquet_source(HCPCS, f"{_GT}/{HCPCS.name}")
    hcpcs_by_year_src = r2.parquet_source(HCPCS_BY_YEAR, f"{_GT}/{HCPCS_BY_YEAR.name}")
    hcpcs_level2_src = r2.parquet_source(HCPCS_LEVEL2_FULL, f"{_GT}/{HCPCS_LEVEL2_FULL.name}")

    if args.drug is not None:
        _require(DRUGS)
        rows = _rows(
            f"SELECT brand_name, generic_name FROM read_parquet('{drugs_src}') "
            "WHERE lower(brand_name) LIKE ? OR lower(generic_name) LIKE ? ORDER BY 1,2 LIMIT ?",
            [f"%{args.drug.lower()}%", f"%{args.drug.lower()}%", args.limit],
        )
        if args.format == "json":
            print(json.dumps(rows, indent=2, default=str))
        else:
            if not rows:
                print(f"No Part D drug matching {args.drug!r}")
            for r in rows:
                print(f"{r['brand_name']}  ->  {r['generic_name']}")
        return 0

    # Per-year history — year-specific identity for a historical claim.
    if args.history or args.year is not None:
        if not (args.code or args.codes):
            sys.exit("--year/--history require --code or --codes")
        _require(HCPCS_BY_YEAR)
        codes = ([args.code.strip().upper()] if args.code
                 else [c.strip().upper() for c in args.codes.split(",") if c.strip()])
        ph = ",".join("?" for _ in codes)
        q = ("SELECT code,year,descriptor,is_drug,unit_basis,code_type,"
             "total_services,total_beneficiaries,canonical_short_descriptor,descriptor_source "
             f"FROM read_parquet('{hcpcs_by_year_src}') "
             f"WHERE upper(code) IN ({ph})")
        params = list(codes)
        if args.year is not None:
            q += " AND year=?"
            params.append(args.year)
        rows = _rows(q + " ORDER BY code, year", params)
        if not rows:
            tgt = ",".join(codes) + (f" (year {args.year})" if args.year is not None else "")
            print(f"No per-year record for {tgt}")
            return 1
        if args.format == "json":
            print(json.dumps(rows, indent=2, default=str))
        elif args.format == "tsv":
            cols = ["code", "year", "descriptor", "is_drug", "unit_basis", "code_type"]
            print("\t".join(cols))
            for r in rows:
                print("\t".join(str(r[c]) for c in cols))
        else:
            print("\n".join(fmt_year(r) for r in rows))
        return 0

    _require(HCPCS)
    partb = f"SELECT * FROM read_parquet('{hcpcs_src}')"
    full_avail = HCPCS_LEVEL2_FULL.exists()

    if args.search is not None:  # search Part B descriptors, then top up from the full HCPCS-II set
        term = f"%{args.search.lower()}%"
        rows = _rows(partb + " WHERE lower(descriptor) LIKE ? ORDER BY code LIMIT ?", [term, args.limit])
        seen = {r["code"].upper() for r in rows}
        full_rows = []
        if full_avail and len(rows) < args.limit:
            cand = _rows(f"SELECT * FROM read_parquet('{hcpcs_level2_src}') "
                         "WHERE lower(descriptor) LIKE ? ORDER BY code LIMIT ?", [term, args.limit])
            full_rows = [r for r in cand if r["code"].upper() not in seen][: args.limit - len(rows)]
    else:  # --code / --codes : Part B first (rich), fall back to the full HCPCS-II release
        codes = ([args.code.strip().upper()] if args.code is not None
                 else [c.strip().upper() for c in args.codes.split(",") if c.strip()])
        ph = ",".join("?" for _ in codes)
        rows = _rows(partb + f" WHERE upper(code) IN ({ph}) ORDER BY code", codes)
        missing = [c for c in codes if c not in {r["code"].upper() for r in rows}]
        full_rows = []
        if missing and full_avail:
            ph2 = ",".join("?" for _ in missing)
            full_rows = _rows(f"SELECT * FROM read_parquet('{hcpcs_level2_src}') "
                              f"WHERE upper(code) IN ({ph2}) ORDER BY code", missing)

    if not rows and not full_rows:
        target = args.code or args.codes or args.search
        print(f"No HCPCS/CPT match for {target!r}")
        return 1

    if args.format == "json":
        print(json.dumps(rows if not full_rows else {"part_b": rows, "reference_only": full_rows},
                         indent=2, default=str))
    elif args.format == "tsv":
        cols = ["code", "descriptor", "is_drug", "unit_basis", "code_type"]
        print("\t".join(cols))
        for r in rows + full_rows:
            print("\t".join(str(r.get(c, "")) for c in cols))
    else:
        print("\n\n".join([fmt_text(r) for r in rows] + [fmt_full(r) for r in full_rows]))

    # Batch-level reminder when both drug and procedure codes are present.
    allrows = rows + full_rows
    if args.code is None and len({bool(r.get("is_drug")) for r in allrows}) > 1 and args.format == "text":
        print("\n⚠ mix of drug + procedure codes: count injections from the procedure codes, "
              "not from drug-unit 'services'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
