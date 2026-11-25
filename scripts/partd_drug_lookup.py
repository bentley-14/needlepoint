"""Medicare Part D Prescribers by Provider and Drug lookup.

Per-(NPI, brand_name) prescribing detail 2013-2024. Tells you WHICH specific
drugs a prescriber is driving, not just aggregate metrics.

Coverage: ~25M (NPI × drug) rows per year × 12 years. Sourced from CMS Part D
Prescriber Public Use File (data.cms.gov).

Usage examples:

    # All drugs prescribed by an NPI in 2023 (top-50 by claims)
    python scripts/partd_drug_lookup.py --npi 1900000045 --year 2023

    # An NPI's top opioids across all years
    python scripts/partd_drug_lookup.py --npi 1900000045 \\
        --drug-class opioid --top 20

    # All prescribers of a specific drug in CA (2023, top 20 by claims)
    python scripts/partd_drug_lookup.py --drug "Oxycodone Hcl" --state CA --year 2023

    # Compare two NPIs' drug mix
    python scripts/partd_drug_lookup.py --npi 1900000045 --year 2023 --top 10

    # Generic name search (e.g., all benzodiazepines)
    python scripts/partd_drug_lookup.py --generic-name "alprazolam" --state CA \\
        --year 2023 --limit 20
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from fraudlib import r2  # noqa: E402  (local files, else private bucket / public mirror)
PARQUET_GLOB = str(r2.DataPath(REPO_ROOT / "data" / "processed" / "cms_partd_prescriber_drug_*.parquet"))

# High-fraud-risk drug-class keyword patterns (case-insensitive partial match
# on either brand or generic name). These are convenience filters for common
# fraud-investigation queries. Each pattern matches via ILIKE '%pattern%' on
# Brnd_Name OR Gnrc_Name.
DRUG_CLASS_PATTERNS: dict[str, list[str]] = {
    "opioid": [
        "Oxycodone", "Hydrocodone", "Morphine", "Fentanyl", "Hydromorphone",
        "Oxymorphone", "Methadone", "Tapentadol", "Tramadol", "Codeine",
        "Buprenorphine", "Meperidine", "Opana",
    ],
    "benzo": [
        "Alprazolam", "Lorazepam", "Diazepam", "Clonazepam", "Temazepam",
        "Oxazepam", "Triazolam", "Midazolam", "Chlordiazepoxide",
    ],
    "stimulant": [
        "Amphetamine", "Methylphenidate", "Lisdexamfetamine", "Dexmethylphenidate",
        "Modafinil",
    ],
    "antipsychotic": [
        "Olanzapine", "Risperidone", "Quetiapine", "Aripiprazole", "Haloperidol",
        "Clozapine", "Ziprasidone", "Lurasidone", "Paliperidone",
    ],
    "compounded": [
        "Compound", "Topical", "Lidocaine", "Ketamine",
    ],
}


def _q(s: str) -> str:
    return s.replace("'", "''")


def query(
    *,
    npi: str | None = None,
    drug_brand: str | None = None,
    drug_generic: str | None = None,
    drug_class: str | None = None,
    state: str | None = None,
    provider_type: str | None = None,
    year: int | None = None,
    sort_by: str = "Tot_Clms",
    top: int = 50,
) -> list[dict]:
    where = []
    if npi:
        where.append(f"Prscrbr_NPI = '{_q(npi.zfill(10))}'")
    if drug_brand:
        where.append(f"Brnd_Name ILIKE '%{_q(drug_brand)}%'")
    if drug_generic:
        where.append(f"Gnrc_Name ILIKE '%{_q(drug_generic)}%'")
    if drug_class:
        patterns = DRUG_CLASS_PATTERNS.get(drug_class.lower())
        if not patterns:
            print(f"unknown drug-class: {drug_class}. Known: {list(DRUG_CLASS_PATTERNS)}",
                  file=sys.stderr)
            return []
        clauses = [
            f"(Brnd_Name ILIKE '%{_q(p)}%' OR Gnrc_Name ILIKE '%{_q(p)}%')"
            for p in patterns
        ]
        where.append("(" + " OR ".join(clauses) + ")")
    if state:
        where.append(f"Prscrbr_State_Abrvtn = '{_q(state.upper())}'")
    if provider_type:
        where.append(f"Prscrbr_Type ILIKE '%{_q(provider_type)}%'")
    if year:
        where.append(f"year = {int(year)}")
    where_sql = " AND ".join(where) if where else "1=1"
    sort_col = sort_by if sort_by in {"Tot_Clms", "Tot_Drug_Cst", "Tot_Benes", "Tot_Day_Suply"} else "Tot_Clms"
    sql = f"""
        SELECT
            year, Prscrbr_NPI,
            Prscrbr_Last_Org_Name AS last_or_org,
            Prscrbr_First_Name AS first_name,
            Prscrbr_City AS city,
            Prscrbr_State_Abrvtn AS state,
            Prscrbr_Type AS specialty,
            Brnd_Name AS brand,
            Gnrc_Name AS generic,
            Tot_Clms, Tot_30day_Fills, Tot_Day_Suply, Tot_Drug_Cst, Tot_Benes,
            GE65_Tot_Clms, GE65_Tot_Drug_Cst
        FROM read_parquet('{PARQUET_GLOB}')
        WHERE {where_sql}
        ORDER BY {sort_col} DESC NULLS LAST
        LIMIT {int(top)}
    """
    return r2.duckdb_r2().sql(sql).df().to_dict("records")


def _format(rows: list[dict]) -> str:
    if not rows:
        return "  ✓ No matching prescriber-drug rows found."
    def _num(v):  # coerce to a clean float; CMS suppresses small counts as NaN/null
        try:
            f = float(v)
        except (ValueError, TypeError):
            return 0.0
        return 0.0 if f != f else f   # f != f is True only for NaN
    out = [f"  {len(rows)} match(es):\n"]
    for r in rows:
        who = f"{r.get('first_name','')} {r.get('last_or_org','')}".strip()
        out.append(
            f"  [{r['year']}] {who}  (NPI {r['Prscrbr_NPI']}, {r.get('specialty','?')}, {r.get('state','?')})"
        )
        out.append(
            f"      {r.get('brand','?')} ({r.get('generic','?')})"
        )
        clms = _num(r.get("Tot_Clms")); cost = _num(r.get("Tot_Drug_Cst"))
        benes = _num(r.get("Tot_Benes")); days = _num(r.get("Tot_Day_Suply"))
        out.append(
            f"      claims={clms:,.0f}  drug_cost=${cost:,.0f}  "
            f"benes={int(benes) if benes else '?'}  day_supply={int(days) if days else '?'}"
        )
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage")[1] if "Usage" in __doc__ else "",
    )
    ap.add_argument("--npi", type=str, help="10-digit NPI")
    ap.add_argument("--drug", dest="drug_brand", type=str, help="Brand name (fuzzy)")
    ap.add_argument("--generic-name", dest="drug_generic", type=str, help="Generic name (fuzzy)")
    ap.add_argument("--drug-class", type=str,
                    help=f"Convenience filter — one of {sorted(DRUG_CLASS_PATTERNS)}")
    ap.add_argument("--state", type=str, help="2-letter state code")
    ap.add_argument("--provider-type", type=str, help="Specialty substring (e.g., 'Pain Medicine')")
    ap.add_argument("--year", type=int, help="Single year filter (2013-2024)")
    ap.add_argument("--sort-by", type=str, default="Tot_Clms",
                    choices=["Tot_Clms", "Tot_Drug_Cst", "Tot_Benes", "Tot_Day_Suply"])
    ap.add_argument("--top", type=int, default=50, help="Return top N rows (default 50)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    has_filter = any([
        args.npi, args.drug_brand, args.drug_generic, args.drug_class,
        args.state, args.provider_type, args.year,
    ])
    if not has_filter:
        ap.print_help()
        return 1

    rows = query(
        npi=args.npi,
        drug_brand=args.drug_brand,
        drug_generic=args.drug_generic,
        drug_class=args.drug_class,
        state=args.state,
        provider_type=args.provider_type,
        year=args.year,
        sort_by=args.sort_by,
        top=args.top,
    )

    if args.json:
        print(json.dumps({"rows": rows}, indent=2, default=str))
        return 0

    print("=== Medicare Part D Prescribers by Provider + Drug ===\n")
    print(f"  Source: cms_partd_prescriber_drug_<year>.parquet  (2013-2024)")
    print(f"  Sort:   {args.sort_by} desc, top {args.top}")
    print()
    print(_format(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
