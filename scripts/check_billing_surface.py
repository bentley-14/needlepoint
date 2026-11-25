"""Check whether NPIs have a Medicare billing surface (Part B / Part D / DMEPOS).

The walk-forward features are built ONLY from the CMS Medicare panels, so a DOJ
defendant with no Part B/D/DMEPOS billing in their fraud (active) years cannot be
featured by the model — i.e. the case is valid DOJ truth but UNSCOREABLE. This
typically happens for commercial-plan fraud (e.g. a private employer health plan),
state-Medicaid-only fraud (not in the Medicare panels), and RN / biller / pure-owner
defendants who don't bill Medicare under their own NPI.

The corpus_curator runs this per resolved NPI candidate so proposals arrive pre-flagged;
it is also the concrete implementation of the data_auditor's rule 7 (billing surface).

Verdicts (per case, evaluated only over the case's active years):
  SURFACE_FOUND            - NPI appears in >=1 program/year within the active years
  NO_SURFACE               - panels exist for the active years but the NPI is absent
  UNVALIDATABLE_NO_PANELS  - no CMS panel exists yet for any active year (e.g. 2025)

Usage:
    # verdict per case in a curator proposals file:
    python scripts/check_billing_surface.py --proposals data/extracted/proposals/proposed_cases_2026-05-30.jsonl

    # ad-hoc one or more NPIs over specific years:
    python scripts/check_billing_surface.py --npi 1900000015 --years 2021,2022,2023,2024,2025
    python scripts/check_billing_surface.py --npi 1900000015,1900000083 --years 2019,2020,2021
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
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

PROGRAMS = {
    "partb": "cms_partb_%d.parquet",
    "partd": "cms_partd_%d.parquet",
    "dmepos": "cms_dmepos_%d.parquet",
}


def _npi_col(path: str) -> str | None:
    names = pq.read_schema(path).names
    for c in names:
        if c.lower() == "npi":
            return c
    for c in names:
        if "npi" in c.lower():
            return c
    return None


def _present_npis(path: str, col: str) -> set[str]:
    s = pd.read_parquet(path, columns=[col])[col]
    if s.dtype == object:
        return set(s.dropna().astype(str).str.split(".").str[0])
    return set(s.dropna().astype("int64").astype(str))


def load_surfaces(base: str, years: list[int]):
    """Return ({(program, year): set(npi)}, {(program, year) with no panel file})."""
    surf: dict[tuple[str, int], set[str]] = {}
    no_panel: set[tuple[str, int]] = set()
    for prog, patt in PROGRAMS.items():
        for y in years:
            basename = patt % y
            local = os.path.join(base, basename)
            try:
                p = str(r2.fetch_local(local, f"{r2.R2_PANELS}/{basename}"))
            except Exception:
                no_panel.add((prog, y))
                continue
            col = _npi_col(p)
            if not col:
                no_panel.add((prog, y))
                continue
            surf[(prog, y)] = _present_npis(p, col)
    return surf, no_panel


def _remote_npi_col(con, src: str) -> str | None:
    """The npi column of a remote parquet, preferring the canonical ``npi``.

    Mirrors :func:`_npi_col` but over an ``s3://`` source via a DuckDB
    ``DESCRIBE`` (a parquet-footer read, no row data). The normalized panels
    carry both a raw ``Rndrng_NPI`` and the canonical ``npi``; we want ``npi``.
    """
    names = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{src}')").fetchall()]
    for c in names:
        if c.lower() == "npi":
            return c
    for c in names:
        if "npi" in c.lower():
            return c
    return None


def load_surfaces_r2(years: list[int], npis, *, con=None):
    """R2-native surface load — the membership-pushdown peer of :func:`load_surfaces`.

    :func:`load_surfaces` downloads every panel and builds the *full* NPI set per
    ``(program, year)``, then membership-tests against it. That forces the whole
    27 GiB panel store onto the runner (``make pull-data``) — which overruns a
    GitHub runner's disk. This variant instead pushes the *small* candidate-NPI
    set down into a DuckDB httpfs query against the canonical ``s3://`` panels
    (projection + predicate pushdown → a few MB of range reads per panel, never
    a full download), and returns the **same** ``(surf, no_panel)`` shape — so
    :func:`verdict_for` is unchanged. ``surf[(prog, year)]`` holds only the
    subset of ``npis`` actually present, which is all membership-testing needs.

    Availability (``no_panel``) is derived from :func:`r2.list_v1_years` (a cached
    ``rclone lsf``, no transfer); a year with no panel file is marked exactly as
    the local loader would.
    """
    con = con or r2.duckdb_r2()
    cand = sorted({str(n).split(".")[0] for n in (npis or []) if n})
    surf: dict[tuple[str, int], set[str]] = {}
    no_panel: set[tuple[str, int]] = set()
    for prog, patt in PROGRAMS.items():
        family = patt.replace("_%d.parquet", "")          # "cms_partb_%d.parquet" -> "cms_partb"
        available = set(r2.list_v1_years(family))
        for y in years:
            if y not in available:
                no_panel.add((prog, y))
                continue
            if not cand:
                surf[(prog, y)] = set()                    # panel exists, no candidates to find
                continue
            basename = patt % y
            src = r2.parquet_source(os.path.join("data/panels", basename),
                                    f"{r2.R2_PANELS}/{basename}")
            col = _remote_npi_col(con, src)
            if not col:
                no_panel.add((prog, y))
                continue
            norm = rf"regexp_replace(CAST({col} AS VARCHAR), '\.0$', '')"
            placeholders = ",".join(["?"] * len(cand))
            q = (
                f"SELECT DISTINCT {norm} AS npi FROM read_parquet('{src}') "
                f"WHERE {norm} IN ({placeholders})"
            )
            surf[(prog, y)] = {row[0] for row in con.execute(q, cand).fetchall()}
    return surf, no_panel


def verdict_for(npi: str, active_years: list[int], surf, no_panel):
    hits: dict[str, list[int]] = {}
    for prog in PROGRAMS:
        ys = [y for y in active_years if (prog, y) in surf and npi in surf[(prog, y)]]
        if ys:
            hits[prog] = ys
    present = sorted({y for ys in hits.values() for y in ys})
    unvalidatable = [y for y in active_years if all((prog, y) in no_panel for prog in PROGRAMS)]
    if present:
        v = "SURFACE_FOUND"
    elif active_years and len(unvalidatable) == len(active_years):
        v = "UNVALIDATABLE_NO_PANELS"
    else:
        v = "NO_SURFACE"
    return v, hits, unvalidatable


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check Medicare billing surface for NPIs / curator proposals")
    ap.add_argument("--proposals", help="proposals JSONL (uses defendant_npi_candidates[0] + all_active_years per row)")
    ap.add_argument("--npi", help="comma-separated NPI(s)")
    ap.add_argument("--years", help="comma-separated years (required with --npi)")
    ap.add_argument("--base", default="data/panels", help="panels directory (default data/panels)")
    args = ap.parse_args(argv)

    cases: list[tuple[str, str | None, list[int]]] = []
    if args.proposals:
        with open(args.proposals) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                npi = (d.get("defendant_npi_candidates") or [None])[0]
                label = d.get("case_id") or d.get("defendant_name") or str(npi)
                cases.append((label, npi, d.get("all_active_years") or []))
    elif args.npi and args.years:
        years = [int(y) for y in args.years.split(",") if y.strip()]
        for npi in args.npi.split(","):
            if npi.strip():
                cases.append((npi.strip(), npi.strip(), years))
    else:
        ap.error("provide --proposals OR (--npi and --years)")

    all_years = sorted({y for _, _, ys in cases for y in ys})
    surf, no_panel = load_surfaces(args.base, all_years)

    n_scoreable = 0
    for label, npi, active in cases:
        if not npi:
            print(f"(no NPI)  {label}\n   active={active}  verdict=NO_NPI")
            continue
        v, hits, unval = verdict_for(str(npi), active, surf, no_panel)
        if v == "SURFACE_FOUND":
            n_scoreable += 1
        detail = "  ".join(f"{prog}={hits[prog]}" for prog in ("partb", "partd", "dmepos") if prog in hits) or "-"
        extra = f"   (unvalidatable / no panel yet: {unval})" if unval and v != "SURFACE_FOUND" else ""
        print(f"{npi}  {label}")
        print(f"   active={active}  verdict={v}")
        print(f"   surface_in_active: {detail}{extra}")
    print(f"\n{n_scoreable}/{len(cases)} have a Medicare billing surface in their active years")
    return 0


if __name__ == "__main__":
    sys.exit(main())
