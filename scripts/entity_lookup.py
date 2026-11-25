#!/usr/bin/env python3
"""Entity/provider graph traversal — investigator runtime lookup tool.

Lets the NeedlePoint loop LOOK ACROSS providers and entities mid-investigation
(P5): given any NPI it answers "who is this, which entity do they belong to,
who else is in it, who do they render for / who renders for them, and which
orgs share their staff" — from local data only, JSON out.

Subcommands
-----------
  npi <NPI> [--year 2024]     identity + entity membership + co-members +
                              Medicaid/Part-B billing summary + BOTH sides of
                              the T-MSIS billing<->servicing split:
                                as_billing_org : who renders under this org
                                as_servicer    : which orgs this NPI renders for
                              (the provider<->entity funnel, natively)
  entity <ENTITY_ID> --year   entity_features row + member roster w/ pay
  org-name "<q>" [--state]    NPPES Type-2 org-name substring search -> NPIs
  shared-staff <NPI>          orgs sharing >=MIN_SHARED servicing NPIs with
                              this billing org (the ring edge)

Data (all local; no network): nppes_bulk, entity_graph/{features}_{year},
cms_partb_{year}, medicaid_features_{year}, T-MSIS provider_spending_full.
Servicing-field caveat rides every answer: many org billers self-report
servicing (validated 2026-07-16 — see scripts/medicaid_preflight.py), so an
empty staff list means "field unpopulated", NOT "no staff".

Usage:
    python scripts/entity_lookup.py npi 1900000087
    python scripts/entity_lookup.py shared-staff 1900000087
    python scripts/entity_lookup.py org-name "CAREGIVERS" --state CO
    python scripts/entity_lookup.py entity ENT_1900000030 --year 2024
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from fraudlib.medicaid_cutoff import claim_clause, clamp_feature_year  # noqa: E402
from fraudlib import r2  # noqa: E402

# DataPath: the local file when present, else the remote copy (private bucket or public mirror)
PROC = r2.DataPath(REPO_ROOT / "data/processed")
NPPES = r2.DataPath(REPO_ROOT / "data/registries/nppes_bulk.parquet")
TMSIS = r2.DataPath(REPO_ROOT / "data/raw/medicaid/provider_spending_full.parquet")

NPI_RE = "^[12][0-9]{9}$"
MIN_SHARED = 3
DEFAULT_YEAR = 2024


def _con():
    return r2.duckdb_r2()


def _one(con, q):
    df = con.execute(q).fetchdf()
    return df.iloc[0].to_dict() if len(df) else None


def identity(con, npi: str) -> dict | None:
    return _one(con, f"""
        SELECT CAST(npi AS VARCHAR) npi, Entity_Type_Code entity_type,
               COALESCE(Provider_Organization_Name, provider_name) AS "name",
               state, Healthcare_Provider_Taxonomy_Code_1 taxonomy,
               Enumeration_Date enumerated, NPI_Deactivation_Date deactivated
        FROM read_parquet('{NPPES}') WHERE CAST(npi AS VARCHAR)='{npi}'""")


def entity_membership(con, npi: str, year: int) -> dict | None:
    g = PROC / f"entity_graph_{year}.parquet"
    if not g.exists():
        return None
    row = _one(con, f"""SELECT entity_id, edge_types_joined FROM read_parquet('{g}')
                    WHERE CAST(npi AS VARCHAR)='{npi}'""")
    if not row:
        return None
    feats = _one(con, f"""SELECT * FROM read_parquet('{PROC / f"entity_features_{year}.parquet"}')
                      WHERE entity_id='{row["entity_id"]}'""")
    members = con.execute(f"""
        SELECT CAST(g.npi AS VARCHAR) npi,
               COALESCE(n.Provider_Organization_Name, n.provider_name) AS "name",
               n.Entity_Type_Code entity_type,
               pb.Tot_Mdcr_Pymt_Amt partb_pay
        FROM read_parquet('{g}') g
        LEFT JOIN read_parquet('{NPPES}') n ON CAST(n.npi AS VARCHAR)=CAST(g.npi AS VARCHAR)
        LEFT JOIN read_parquet('{PROC / f"cms_partb_{year}.parquet"}') pb
               ON CAST(pb.Rndrng_NPI AS VARCHAR)=CAST(g.npi AS VARCHAR)
        WHERE g.entity_id='{row["entity_id"]}' AND CAST(g.npi AS VARCHAR) <> '{npi}'
        ORDER BY pb.Tot_Mdcr_Pymt_Amt DESC NULLS LAST LIMIT 25""").fetchdf()
    return {"entity_id": row["entity_id"], "edge_types": row["edge_types_joined"],
            "features": feats,
            "co_members": members.to_dict(orient="records")}


def billing_summary(con, npi: str, year: int) -> dict:
    out: dict = {}
    m = PROC / f"medicaid_features_{year}.parquet"
    if m.exists():
        out["medicaid"] = _one(con, f"""
            SELECT medicaid_total_pay, medicaid_claim_lines, top_hcpcs,
                   service_class, top1_code_share,
                   medicaid_total_pay_pctile_in_class_state
            FROM read_parquet('{m}') WHERE CAST(npi AS VARCHAR)='{npi}'""")
    pb = PROC / f"cms_partb_{year}.parquet"
    if pb.exists():
        out["partb"] = _one(con, f"""
            SELECT Tot_Mdcr_Pymt_Amt partb_pay, Rndrng_Prvdr_Type provider_type
            FROM read_parquet('{pb}') WHERE CAST(Rndrng_NPI AS VARCHAR)='{npi}'""")
    return out


def servicing_edges(con, npi: str, year: int) -> dict:
    """Both directions of the T-MSIS billing<->servicing split."""
    staff = con.execute(f"""
        SELECT t.SERVICING_PROVIDER_NPI_NUM npi,
               COALESCE(n.provider_name, n.Provider_Organization_Name) AS "name",
               SUM(t.TOTAL_PAID) paid, SUM(t.TOTAL_CLAIM_LINES) lines
        FROM read_parquet('{TMSIS}') t
        LEFT JOIN read_parquet('{NPPES}') n
               ON CAST(n.npi AS VARCHAR)=t.SERVICING_PROVIDER_NPI_NUM
        WHERE t.BILLING_PROVIDER_NPI_NUM='{npi}'
          AND SUBSTR(t.CLAIM_FROM_MONTH,1,4)='{year}'
          AND t.SERVICING_PROVIDER_NPI_NUM ~ '{NPI_RE}'
          AND t.SERVICING_PROVIDER_NPI_NUM <> '{npi}'
        GROUP BY 1,2 ORDER BY paid DESC LIMIT 15""").fetchdf()
    renders_for = con.execute(f"""
        SELECT t.BILLING_PROVIDER_NPI_NUM npi,
               COALESCE(n.Provider_Organization_Name, n.provider_name) AS "name",
               SUM(t.TOTAL_PAID) paid
        FROM read_parquet('{TMSIS}') t
        LEFT JOIN read_parquet('{NPPES}') n
               ON CAST(n.npi AS VARCHAR)=t.BILLING_PROVIDER_NPI_NUM
        WHERE t.SERVICING_PROVIDER_NPI_NUM='{npi}'
          AND SUBSTR(t.CLAIM_FROM_MONTH,1,4)='{year}'
          AND t.BILLING_PROVIDER_NPI_NUM <> '{npi}'
        GROUP BY 1,2 ORDER BY paid DESC LIMIT 15""").fetchdf()
    return {
        "as_billing_org_staff": staff.to_dict(orient="records"),
        "renders_under_orgs": renders_for.to_dict(orient="records"),
        "caveat": "empty staff list may mean the servicing field is org-self-"
                  "reported (unpopulated), not that no staff exist",
    }


def shared_staff(con, npi: str, min_shared: int = MIN_SHARED) -> list[dict]:
    df = con.execute(f"""
        WITH tgt AS (SELECT DISTINCT SERVICING_PROVIDER_NPI_NUM s
                     FROM read_parquet('{TMSIS}')
                     WHERE BILLING_PROVIDER_NPI_NUM='{npi}'
                       AND SERVICING_PROVIDER_NPI_NUM ~ '{NPI_RE}'
                       AND SERVICING_PROVIDER_NPI_NUM <> '{npi}'{claim_clause("CLAIM_FROM_MONTH")})
        SELECT t.BILLING_PROVIDER_NPI_NUM npi,
               ANY_VALUE(COALESCE(n.Provider_Organization_Name, n.provider_name)) AS "name",
               COUNT(DISTINCT t.SERVICING_PROVIDER_NPI_NUM) shared_staff
        FROM read_parquet('{TMSIS}') t
        JOIN tgt ON tgt.s = t.SERVICING_PROVIDER_NPI_NUM
        LEFT JOIN read_parquet('{NPPES}') n
               ON CAST(n.npi AS VARCHAR)=t.BILLING_PROVIDER_NPI_NUM
        WHERE t.BILLING_PROVIDER_NPI_NUM <> '{npi}'{claim_clause("t.CLAIM_FROM_MONTH")}
        GROUP BY 1 HAVING COUNT(DISTINCT t.SERVICING_PROVIDER_NPI_NUM) >= {min_shared}
        ORDER BY shared_staff DESC LIMIT 25""").fetchdf()
    return df.to_dict(orient="records")


def org_name_search(con, q: str, state: str | None) -> list[dict]:
    ql = q.upper().replace("'", "''")
    st = f"AND state='{state.upper()}'" if state else ""
    df = con.execute(f"""
        SELECT CAST(npi AS VARCHAR) npi, Provider_Organization_Name AS "name", state,
               Enumeration_Date enumerated
        FROM read_parquet('{NPPES}')
        WHERE Entity_Type_Code='2'
          AND UPPER(Provider_Organization_Name) LIKE '%{ql}%' {st}
        LIMIT 40""").fetchdf()
    return df.to_dict(orient="records")


def entity_detail(con, entity_id: str, year: int) -> dict | None:
    f = PROC / f"entity_features_{year}.parquet"
    g = PROC / f"entity_graph_{year}.parquet"
    if not (f.exists() and g.exists()):
        return None
    feats = _one(con, f"SELECT * FROM read_parquet('{f}') WHERE entity_id='{entity_id}'")
    members = con.execute(f"""
        SELECT CAST(g.npi AS VARCHAR) npi,
               COALESCE(n.Provider_Organization_Name, n.provider_name) AS "name",
               n.Entity_Type_Code entity_type, g.edge_types_joined
        FROM read_parquet('{g}') g
        LEFT JOIN read_parquet('{NPPES}') n ON CAST(n.npi AS VARCHAR)=CAST(g.npi AS VARCHAR)
        WHERE g.entity_id='{entity_id}' LIMIT 200""").fetchdf()
    return {"entity_id": entity_id, "features": feats,
            "members": members.to_dict(orient="records")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("npi"); p.add_argument("npi"); p.add_argument("--year", type=int, default=DEFAULT_YEAR)
    p = sub.add_parser("entity"); p.add_argument("entity_id"); p.add_argument("--year", type=int, default=DEFAULT_YEAR)
    p = sub.add_parser("org-name"); p.add_argument("query"); p.add_argument("--state", default=None)
    p = sub.add_parser("shared-staff"); p.add_argument("npi"); p.add_argument("--min-shared", type=int, default=MIN_SHARED)
    args = ap.parse_args(argv)
    # Leak-safe calibration: a --year past the cutoff reads the newest
    # admissible frame instead (inert in production).
    if getattr(args, "year", None) is not None:
        args.year = clamp_feature_year(args.year)
    con = _con()

    if args.cmd == "npi":
        out = {"identity": identity(con, args.npi),
               "entity": entity_membership(con, args.npi, args.year),
               "billing": billing_summary(con, args.npi, args.year),
               "servicing": servicing_edges(con, args.npi, args.year),
               "year": args.year}
    elif args.cmd == "entity":
        out = entity_detail(con, args.entity_id, args.year)
    elif args.cmd == "org-name":
        out = {"query": args.query, "state": args.state,
               "matches": org_name_search(con, args.query, args.state)}
    else:
        out = {"npi": args.npi,
               "orgs_sharing_servicing_staff": shared_staff(con, args.npi, args.min_shared)}

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
