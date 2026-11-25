#!/usr/bin/env python3
"""Public-proxy retrieval for the foreclosure gate — TRY before conceding.

Every org case to date died on `innocent_foreclosed`, and in several the gate
declared an explanation `cannot_close_pre_filing` WITHOUT attempting retrieval
that public data supports. This tool answers, for a given innocent-explanation
taxonomy tag + target NPI: what PUBLIC evidence bears on it, what does that
evidence actually say, and is this genuinely non-public?

Registry: data/reference/foreclosure_proxies.yaml (per-tag sources, strength,
`truly_non_public`). Where a source is local + mechanizable, this RUNS it and
reports the finding; otherwise it prints the exact command/URL for the agent.

Subcommands
-----------
  tags                        list every taxonomy tag + whether it is closeable
  check <tag> --npi <NPI>     run the retrievable proxies for that tag
  staff --npi <NPI>           PECOS/T-MSIS/NPPES workforce count (the 410.26 tag)
  scope --npi <NPI>           NPPES taxonomy vs billed-code family
  exclusion --npi <NPI>       LEIE / revocation / board-action screen

Usage:
    python scripts/foreclosure_proxy.py tags
    python scripts/foreclosure_proxy.py check large_clinical_staff_workforce_410_26 --npi 1900000082
    python scripts/foreclosure_proxy.py staff --npi 1900000082
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REGISTRY = REPO_ROOT / "data" / "reference" / "foreclosure_proxies.yaml"
PECOS = REPO_ROOT / "data" / "processed" / "pecos_reassignment.parquet"
NPPES = REPO_ROOT / "data" / "registries" / "nppes_bulk.parquet"
TMSIS = REPO_ROOT / "data" / "raw" / "medicaid" / "provider_spending_full.parquet"
LEIE = REPO_ROOT / "data" / "processed" / "leie.parquet"
REVOKED = REPO_ROOT / "data" / "processed" / "revoked_medicare_providers.parquet"
BOARD = REPO_ROOT / "data" / "processed" / "state_board_actions_fsmb.parquet"


def registry() -> dict:
    with REGISTRY.open() as f:
        return (yaml.safe_load(f) or {}).get("proxies", {}) or {}


def _cols(con, path: Path) -> list[str]:
    return con.execute(f"SELECT * FROM read_parquet('{path}') LIMIT 0").df().columns.tolist()


def staff_count(con, npi: str) -> dict:
    """Workforce evidence for the incident-to / large-staff explanation."""
    out: dict = {"npi": npi, "sources": {}}
    if PECOS.exists():
        # PECOS keys the GROUP side by PAC ID (there is no "Group NPI" column —
        # verified 2026-07-26), so resolve the target's group(s) via its
        # Individual NPI, then count the siblings reassigning to those groups.
        ind, grp = "Individual NPI", "Group PAC ID"
        cols = _cols(con, PECOS)
        if ind in cols and grp in cols:
            groups = con.execute(f"""
                SELECT "{grp}" g, ANY_VALUE("Group Legal Business Name") nm
                FROM read_parquet('{PECOS}') WHERE CAST("{ind}" AS VARCHAR)='{npi}'
                GROUP BY 1""").fetchdf()
            glist = [str(g) for g in groups["g"].tolist() if g is not None]
            siblings = 0
            if glist:
                ids = ",".join(f"'{g}'" for g in glist)
                siblings = con.execute(f"""
                    SELECT COUNT(DISTINCT "{ind}") FROM read_parquet('{PECOS}')
                    WHERE CAST("{grp}" AS VARCHAR) IN ({ids})
                      AND CAST("{ind}" AS VARCHAR) <> '{npi}'""").fetchone()[0]
            out["sources"]["pecos_reassignment"] = {
                "groups_this_npi_reassigns_to": glist,
                "group_names": [n for n in groups["nm"].tolist() if n],
                "other_individuals_in_those_groups": int(siblings),
                "reading": ("a real incident-to workforce shows MANY other individuals "
                            "reassigning to the same group(s); 0-1 undercuts the "
                            "large-staff defense. NOTE: PECOS covers Medicare-enrolled "
                            "individual practitioners only — unlicensed/aide staff and "
                            "W-2 non-billing clinicians do NOT appear, so a low count "
                            "narrows but does not alone foreclose."),
            }
    if TMSIS.exists():
        n = con.execute(f"""
            SELECT COUNT(DISTINCT SERVICING_PROVIDER_NPI_NUM) FROM read_parquet('{TMSIS}')
            WHERE BILLING_PROVIDER_NPI_NUM='{npi}'
              AND SERVICING_PROVIDER_NPI_NUM ~ '^[12][0-9]{{9}}$'
              AND SERVICING_PROVIDER_NPI_NUM <> BILLING_PROVIDER_NPI_NUM""").fetchone()[0]
        out["sources"]["tmsis_servicing_roster"] = {
            "distinct_non_self_servicing_npis": int(n),
            "caveat": "org self-report is common; 0 may mean unpopulated, not no-staff",
        }
    return out


def scope_check(con, npi: str) -> dict:
    from scripts.mo_signatures import code_family  # noqa: PLC0415
    row = con.execute(f"""
        SELECT Healthcare_Provider_Taxonomy_Code_1 tax,
               COALESCE(Provider_Organization_Name, provider_name) AS nm,
               Entity_Type_Code et
        FROM read_parquet('{NPPES}') WHERE CAST(npi AS VARCHAR)='{npi}'""").fetchdf()
    return {"npi": npi, "taxonomy": (row.at[0, "tax"] if len(row) else None),
            "name": (row.at[0, "nm"] if len(row) else None),
            "entity_type": (row.at[0, "et"] if len(row) else None),
            "how_to_finish": "compare to dominant billed-code family "
                             "(scripts/mo_signatures.py --npi <NPI>)"}


def exclusion_check(con, npi: str) -> dict:
    out: dict = {"npi": npi}
    for label, path in (("leie", LEIE), ("revoked_medicare", REVOKED), ("board_actions", BOARD)):
        if not path.exists():
            out[label] = "source not local"
            continue
        cols = _cols(con, path)
        c = next((x for x in cols if x.upper() in ("NPI", "NPI_NUM") or x.lower() == "npi"), None)
        if not c:
            out[label] = f"no NPI column (cols: {cols[:6]})"
            continue
        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{path}') "
                        f"WHERE CAST(\"{c}\" AS VARCHAR)='{npi}'").fetchone()[0]
        out[label] = {"hits": int(n), "clean": n == 0}
    return out


def check_tag(con, tag: str, npi: str | None) -> dict:
    reg = registry()
    if tag not in reg:
        return {"error": f"unknown tag '{tag}'", "known": sorted(reg)}
    entry = reg[tag]
    out = {"tag": tag, "closes": entry.get("closes"),
           "truly_non_public": bool(entry.get("truly_non_public")),
           "sources": entry.get("sources", []), "findings": {}}
    if out["truly_non_public"]:
        out["verdict"] = ("GENUINELY NON-PUBLIC — cannot_close_pre_filing is CORRECT here; "
                          "this is a referral-track explanation, not a retrieval failure.")
        return out
    if npi:
        if tag == "large_clinical_staff_workforce_410_26":
            out["findings"]["workforce"] = staff_count(con, npi)
        if tag in ("specialty_case_mix", "unqualified_or_unlicensed_staff"):
            out["findings"]["scope"] = scope_check(con, npi)
        if tag == "unqualified_or_unlicensed_staff":
            out["findings"]["exclusion"] = exclusion_check(con, npi)
    out["verdict"] = ("CLOSEABLE ON PUBLIC DATA — run the sources above before setting "
                      "cannot_close_pre_filing; state what you ran and what it showed.")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tags")
    for name in ("check",):
        p = sub.add_parser(name); p.add_argument("tag"); p.add_argument("--npi")
    for name in ("staff", "scope", "exclusion"):
        p = sub.add_parser(name); p.add_argument("--npi", required=True)
    args = ap.parse_args(argv)
    con = duckdb.connect()
    if args.cmd == "tags":
        for tag, e in sorted(registry().items()):
            flag = "NON-PUBLIC (decline OK)" if e.get("truly_non_public") else "closeable on public data"
            print(f"  {tag:42s} {flag}\n      closes: {e.get('closes')}")
        return 0
    fn = {"check": lambda: check_tag(con, args.tag, args.npi),
          "staff": lambda: staff_count(con, args.npi),
          "scope": lambda: scope_check(con, args.npi),
          "exclusion": lambda: exclusion_check(con, args.npi)}[args.cmd]
    print(json.dumps(fn(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
