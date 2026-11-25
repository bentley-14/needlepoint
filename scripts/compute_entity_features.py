#!/usr/bin/env python3
"""Entity feature frame — aggregates entity_graph_{year}.parquet member NPIs
into one row per entity_id.

The missing piece after ``fraudlib/entity/resolve.py``'s panel-wide connected-
components graph. Turns (npi, entity_id) membership into entity-LEVEL features
an entity/org scoring head (P4, NOT built here) or the console's entity board
can consume directly, instead of every consumer re-aggregating member NPIs
itself. This is the feature layer ONLY: no scoring head, no loop/evidence-pack
wiring, and it does not touch ``feature_frames.py``'s Type-2 drop flag.

Reads (all via DuckDB SQL over parquet -- no full-panel pandas loads):
  data/processed/entity_graph_{year}.parquet   (fraudlib.entity.resolve output;
                                                 REQUIRED -- run resolve.py first)
  cms_partb_{year}.parquet                     (member $, specialty, entity
                                                 code, address; REQUIRED, same
                                                 source resolve.py itself needs)
  data/registries/nppes_bulk.parquet           (member Enumeration_Date --
                                                 OPTIONAL, degrades to NULL
                                                 enumeration_cadence)
  data/panels/cms_partb_svc_{year}.parquet     (member dominant HCPCS --
                                                 OPTIONAL, only fetched for
                                                 2..MAX_ENTITY_SIZE-member
                                                 entities; degrades to NULL
                                                 cross_member_code_uniformity)
  results/.../scored_full_{year}.parquet       (ranker panel -- OPTIONAL,
                                                 degrades to NULL
                                                 pct_members_anomalous)

nppes_bulk / cms_partb_svc are read LOCAL-FIRST (r2_first=False), not R2-first
like most of this codebase: both are large, NPI-UNSORTED tables, and an R2
httpfs scan/semi-join against them (no row-group pruning possible on a random
NPI subset) measured at ~1.2 HOURS for a single full-table pass in this
environment, vs <1s local. `make pull-entity-deps` (see Makefile) stages both
locally once; if absent, these two sources degrade to NULL rather than stall.

Output: data/processed/entity_features_{year}.parquet -- one row per entity_id:
  entity_id, n_members, n_billing_npis, entity_total_pay, dominant_specialty,
  has_type2_member, n_distinct_addresses, enumeration_cadence,
  cross_member_code_uniformity, pct_members_anomalous, collectible_entity.

``collectible_entity`` picks the entity's target NPI -- preferring (qui tam counsel: the
entity that RETAINS the money, not the rendering cog) a Type-2 org member,
else a member that contributed via the 'org_pac' (shared billing-entity)
edge, else the highest-paid member. True PECOS-reassignment-vehicle
preference is deferred along with resolve.py's reassignment edge (no bulk
table mirrored yet -- see resolve.py's module docstring).

Usage:
    python -m scripts.compute_entity_features --year 2024
    python -m scripts.compute_entity_features --all-years
    python -m scripts.compute_entity_features --year 2024 --dry-run
"""
from __future__ import annotations

import argparse
import glob
import logging
import sys
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve().parent          # scripts/
REPO_ROOT = _HERE.parent
for _p in (str(_HERE), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fraudlib import r2                                    # noqa: E402
from fraudlib.entity.resolve import MAX_ENTITY_SIZE         # noqa: E402

LOG = logging.getLogger("scripts.compute_entity_features")

# A ring member counts as "anomalous" if it lands in the top ANOMALY_PCT of the
# ranker panel by score_rrf. Mirrors scripts/ring_enrichment.py::ANOMALY_PCT
# (top 2%) -- kept as a separate literal rather than importing that module to
# avoid coupling this batch feature-builder to an interactive investigator
# tool's dependency chain (pecos_lookup/physician_compare_lookup); update both
# together if this is ever retuned.
ANOMALY_PCT = 0.02

ENTITY_GRAPH_DIR = REPO_ROOT / "data/processed"
PARTB_DIR = REPO_ROOT / "data/processed"
PARTB_SVC_DIR = REPO_ROOT / "data/panels"          # mirrors compute_hcpcs_rich_features.py
OUTPUT_DIR = REPO_ROOT / "data/processed"

HCPCS_BATCH_SIZE = 500   # mirrors scripts/target_sweep.py::DEFAULT_BATCH_SIZE

# Canonical (3,1,0) walk-forward baseline config suffix (mirrors Makefile's
# RESULTS_GLOB) -- used only as the R2 fallback when no local results/ run
# exists for the year (ring_enrichment.py's local-only glob has no such
# fallback; this script adds one so pct_members_anomalous doesn't silently
# go NULL on a machine that hasn't pulled the multi-GB results/ baseline).
_RESULTS_CONFIG_SUFFIX = "extras-True_agentfilter-floor_0.50_perspec-False_es50"


# ─────────────────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────────────────
def entity_graph_path(year: int, base_dir: Path = ENTITY_GRAPH_DIR) -> Path:
    return base_dir / f"entity_graph_{year}.parquet"


def available_entity_graph_years(base_dir: Path = ENTITY_GRAPH_DIR) -> list[int]:
    """Years with a local entity_graph_{year}.parquet (drives --all-years)."""
    years = []
    for p in base_dir.glob("entity_graph_*.parquet"):
        tail = p.stem.rsplit("_", 1)[-1]
        if tail.isdigit():
            years.append(int(tail))
    return sorted(years)


def scored_panel_source(year: int) -> str | None:
    """Local-glob-first (mirrors ring_enrichment.py::_scored_panel), else the
    canonical (3,1,0) baseline config on R2. Returns None if neither a local
    run nor a guessable R2 key can be formed (never raises)."""
    local = sorted(glob.glob(
        str(REPO_ROOT / f"results/walk_forward_v4_top5_y{year}_*/scored_full_{year}.parquet")))
    if local:
        return local[0]
    return (f"s3://needlepoint-data/results/current/"
            f"walk_forward_v4_top5_y{year}_{_RESULTS_CONFIG_SUFFIX}/scored_full_{year}.parquet")


# ─────────────────────────────────────────────────────────────────────────────
# I/O -- DuckDB fetch. Heavy aggregation happens in SQL; only already-
# entity/npi-scoped rows touch pandas (mirrors fraudlib/entity/resolve.py).
# ─────────────────────────────────────────────────────────────────────────────
def fetch_member_aggregates(con, entity_src: str, partb_src: str) -> pd.DataFrame:
    """Per-entity_id aggregates derivable from entity_graph + the SAME year's
    Part-B panel alone (no optional/external source needed): n_members,
    n_billing_npis, entity_total_pay, dominant_specialty, has_type2_member,
    n_distinct_addresses, collectible_entity."""
    return con.execute(f"""
        WITH m AS (
          SELECT e.entity_id, e.npi, e.edge_types_joined,
                 COALESCE(p.Tot_Mdcr_Pymt_Amt, 0) AS pay,
                 p.Rndrng_Prvdr_Type AS specialty,
                 p.Rndrng_Prvdr_Ent_Cd AS ent_cd,
                 CASE WHEN p.Rndrng_Prvdr_St1 IS NOT NULL AND TRIM(p.Rndrng_Prvdr_St1) <> ''
                       AND p.Rndrng_Prvdr_Zip5 IS NOT NULL AND TRIM(CAST(p.Rndrng_Prvdr_Zip5 AS VARCHAR)) <> ''
                      THEN TRIM(p.Rndrng_Prvdr_St1) || '|' || TRIM(CAST(p.Rndrng_Prvdr_Zip5 AS VARCHAR))
                      ELSE NULL END AS address_key
          FROM read_parquet('{entity_src}') e
          LEFT JOIN read_parquet('{partb_src}') p
            ON CAST(p.Rndrng_NPI AS VARCHAR) = e.npi
        )
        SELECT entity_id,
               COUNT(*)                                        AS n_members,
               COUNT(*) FILTER (WHERE pay > 0)                 AS n_billing_npis,
               SUM(pay)                                        AS entity_total_pay,
               mode(specialty)                                 AS dominant_specialty,
               bool_or(ent_cd = 'O')                           AS has_type2_member,
               COUNT(DISTINCT address_key)                     AS n_distinct_addresses,
               COALESCE(
                 arg_max(npi, pay) FILTER (WHERE ent_cd = 'O'),
                 arg_max(npi, pay) FILTER (WHERE edge_types_joined LIKE '%org_pac%'),
                 arg_max(npi, pay)
               )                                                AS collectible_entity
        FROM m
        GROUP BY entity_id
    """).fetchdf()


def fetch_enumeration_dates(con, npis: list[str], nppes_src: str) -> pd.DataFrame:
    """(npi, enum_date) for the given npis via nppes_bulk. A reference table
    (like the DAC), so this mirrors resolve.py::fetch_org_pac_edges' IN-list
    convention rather than target_sweep.py's client-side batching (reserved
    for the much larger line-item cms_partb_svc file, see
    fetch_dominant_hcpcs)."""
    if not npis:
        return pd.DataFrame(columns=["npi", "enum_date"])
    ids = ",".join(f"'{n}'" for n in npis)
    return con.execute(f"""
        SELECT CAST(npi AS VARCHAR) AS npi,
               TRY_STRPTIME(Enumeration_Date, '%m/%d/%Y')::DATE AS enum_date
        FROM read_parquet('{nppes_src}')
        WHERE CAST(npi AS VARCHAR) IN ({ids})
          AND Enumeration_Date IS NOT NULL AND Enumeration_Date <> ''
    """).fetchdf()


def fetch_dominant_hcpcs(con, npis: list[str], year: int, svc_src: str,
                         batch_size: int = HCPCS_BATCH_SIZE) -> pd.DataFrame:
    """Per-npi dominant (highest estimated-payment) HCPCS code, batched at
    <=500 NPIs per query -- mirrors scripts/target_sweep.py::fetch_top_codes_batch
    (same cms_partb_svc line-item file, same throughput convention)."""
    if not npis:
        return pd.DataFrame(columns=["npi", "dominant_code"])
    frames = []
    for start in range(0, len(npis), batch_size):
        batch = npis[start:start + batch_size]
        ids = ",".join(f"'{n}'" for n in batch)
        frames.append(con.execute(f"""
            SELECT CAST(Rndrng_NPI AS VARCHAR) AS npi, HCPCS_Cd,
                   SUM(Tot_Srvcs * Avg_Mdcr_Pymt_Amt) AS pay
            FROM read_parquet('{svc_src}')
            WHERE CAST(Rndrng_NPI AS VARCHAR) IN ({ids})
            GROUP BY 1, 2
        """).fetchdf())
    all_codes = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if all_codes.empty:
        return pd.DataFrame(columns=["npi", "dominant_code"])
    top_idx = all_codes.groupby("npi")["pay"].idxmax()
    top = all_codes.loc[top_idx, ["npi", "HCPCS_Cd"]].rename(columns={"HCPCS_Cd": "dominant_code"})
    return top.reset_index(drop=True)


def fetch_anomaly_ranks(con, year: int) -> pd.DataFrame:
    """(npi, pct) rank-percentile (0=top) from the canonical scored ranker
    panel for the WHOLE year -- mirrors ring_enrichment.py::_panel_ranks, but
    fetched once for every panel npi rather than per-seed, since (unlike
    enumeration_cadence/cross_member_code_uniformity) this is well-defined
    even for a singleton entity. Best-effort: returns empty on any failure
    (no local run AND R2 unreachable/disabled) so pct_members_anomalous
    degrades to NULL rather than failing the whole build."""
    src = scored_panel_source(year)
    if not src:
        return pd.DataFrame(columns=["npi", "pct"])
    try:
        return con.execute(f"""
            SELECT CAST(npi AS VARCHAR) AS npi,
                   CAST(ROW_NUMBER() OVER (ORDER BY score_rrf DESC) AS DOUBLE)
                     / COUNT(*) OVER () AS pct
            FROM read_parquet('{src}')
        """).fetchdf()
    except Exception as e:
        LOG.warning("  %d: scored panel unavailable (%s) -- pct_members_anomalous will be NULL", year, e)
        return pd.DataFrame(columns=["npi", "pct"])


# ─────────────────────────────────────────────────────────────────────────────
# Pure derivation -- takes the entity graph + already-fetched ingredient
# frames and merges them per entity_id. No file I/O, no DuckDB -- this is
# what tests/test_entity_resolution.py exercises directly.
# ─────────────────────────────────────────────────────────────────────────────
def entity_enumeration_cadence(entity_df: pd.DataFrame, enum_dates: pd.DataFrame) -> pd.DataFrame:
    """entity_id -> enumeration_cadence (days) = (max - min enum date span)
    / (n_dated_members - 1); NULL when fewer than 2 dated members. Small
    cadence + recent dates = sequential rapid spin-up (the Red Sun signal);
    a large span or NULL = an organically-grown or single-founder practice."""
    cols = ["entity_id", "enumeration_cadence"]
    if enum_dates.empty:
        return pd.DataFrame(columns=cols)
    merged = entity_df[["npi", "entity_id"]].merge(enum_dates.dropna(subset=["enum_date"]),
                                                    on="npi", how="inner")
    if merged.empty:
        return pd.DataFrame(columns=cols)
    merged["enum_date"] = pd.to_datetime(merged["enum_date"])
    g = merged.groupby("entity_id")["enum_date"].agg(["min", "max", "count"])
    g["enumeration_cadence"] = (g["max"] - g["min"]).dt.days / (g["count"] - 1)
    g.loc[g["count"] < 2, "enumeration_cadence"] = float("nan")
    return g[["enumeration_cadence"]].reset_index()[cols]


def entity_code_uniformity(entity_df: pd.DataFrame, dominant_hcpcs: pd.DataFrame) -> pd.DataFrame:
    """entity_id -> cross_member_code_uniformity = share of billing members
    whose dominant HCPCS code equals the entity's most common member-dominant
    code (the ring signature: 1.0 = every member's #1 code matches). NULL
    when fewer than 2 members have a determined dominant code."""
    cols = ["entity_id", "cross_member_code_uniformity"]
    if dominant_hcpcs.empty:
        return pd.DataFrame(columns=cols)
    merged = entity_df[["npi", "entity_id"]].merge(dominant_hcpcs, on="npi", how="inner")
    if merged.empty:
        return pd.DataFrame(columns=cols)
    counts = merged.groupby(["entity_id", "dominant_code"]).size().reset_index(name="n")
    totals = merged.groupby("entity_id").size().reset_index(name="n_total")
    top = counts.sort_values("n", ascending=False).drop_duplicates("entity_id")
    out = top.merge(totals, on="entity_id")
    out["cross_member_code_uniformity"] = out["n"] / out["n_total"]
    out.loc[out["n_total"] < 2, "cross_member_code_uniformity"] = float("nan")
    return out[cols]


def entity_pct_anomalous(entity_df: pd.DataFrame, anomaly_ranks: pd.DataFrame,
                         anomaly_pct: float = ANOMALY_PCT) -> pd.DataFrame:
    """entity_id -> pct_members_anomalous = share of ALL entity members
    (denominator = n_members, not just ranked members -- a member absent
    from the scored panel, e.g. dropped by the agent filter, counts as
    not-anomalous, mirroring ring_enrichment.py's implicit convention) whose
    ranker-panel percentile lands in the top anomaly_pct. Empty
    anomaly_ranks (no scored panel reachable at all) -> no rows, i.e. NULL
    for every entity after the caller's left-join."""
    if anomaly_ranks.empty:
        return pd.DataFrame(columns=["entity_id", "pct_members_anomalous"])
    anom_npis = set(anomaly_ranks.loc[anomaly_ranks["pct"] <= anomaly_pct, "npi"])
    tmp = entity_df[["npi", "entity_id"]].copy()
    tmp["is_anom"] = tmp["npi"].isin(anom_npis)
    return tmp.groupby("entity_id")["is_anom"].mean().rename("pct_members_anomalous").reset_index()


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────
def compute_year(year: int, entity_dir: Path = ENTITY_GRAPH_DIR, partb_dir: Path = PARTB_DIR,
                 partb_svc_dir: Path = PARTB_SVC_DIR) -> pd.DataFrame:
    """Fetch + compute the full entity feature frame for one year."""
    entity_src = str(entity_graph_path(year, entity_dir))
    if not Path(entity_src).exists():
        raise FileNotFoundError(
            f"{entity_src} not found -- run `python -m fraudlib.entity.resolve --year {year}` first")
    partb_src = str(partb_dir / f"cms_partb_{year}.parquet")
    if not Path(partb_src).exists():
        raise FileNotFoundError(f"{partb_src} not found")

    con = r2.duckdb_r2()

    LOG.info("  %d: core aggregates (entity_graph + Part-B panel)...", year)
    base = fetch_member_aggregates(con, entity_src, partb_src)

    entity_df = pd.read_parquet(entity_src)  # small: (npi, entity_id, edge_types_joined)
    sizes = entity_df.groupby("entity_id")["npi"].transform("size")
    # Enrichment-eligible members: entities with 2..MAX_ENTITY_SIZE members --
    # excludes singletons (no peer to compare against) AND flagged
    # megaclusters (resolve.py's own backstop -- transitive-chaining
    # artifacts, not real rings; comparing "cadence"/"uniformity" across one
    # would be both meaningless and needlessly expensive to fetch).
    eligible_npis = entity_df.loc[(sizes >= 2) & (sizes <= MAX_ENTITY_SIZE), "npi"].tolist()
    LOG.info("  %d: %d/%d panel NPIs eligible for cross-member enrichment (2..%d-member entities)",
              year, len(eligible_npis), len(entity_df), MAX_ENTITY_SIZE)

    LOG.info("  %d: enumeration dates (nppes_bulk)...", year)
    try:
        nppes_src = r2.parquet_source(REPO_ROOT / "data/registries/nppes_bulk.parquet",
                                      f"{r2.R2_PANELS}/nppes_bulk.parquet", r2_first=False)
        enum_dates = fetch_enumeration_dates(con, eligible_npis, nppes_src)
    except Exception as e:
        LOG.warning("  %d: nppes_bulk unavailable (%s) -- enumeration_cadence will be NULL", year, e)
        enum_dates = pd.DataFrame(columns=["npi", "enum_date"])

    LOG.info("  %d: dominant HCPCS codes (cms_partb_svc, batched)...", year)
    try:
        svc_src = r2.parquet_source(partb_svc_dir / f"cms_partb_svc_{year}.parquet",
                                    f"{r2.R2_PANELS}/cms_partb_svc_{year}.parquet", r2_first=False)
        dominant_hcpcs = fetch_dominant_hcpcs(con, eligible_npis, year, svc_src)
    except Exception as e:
        LOG.warning("  %d: cms_partb_svc unavailable (%s) -- cross_member_code_uniformity will be NULL",
                    year, e)
        dominant_hcpcs = pd.DataFrame(columns=["npi", "dominant_code"])

    LOG.info("  %d: ranker-anomaly ranks (scored_full panel)...", year)
    anomaly_ranks = fetch_anomaly_ranks(con, year)

    out = base
    for extra in (entity_enumeration_cadence(entity_df, enum_dates),
                  entity_code_uniformity(entity_df, dominant_hcpcs),
                  entity_pct_anomalous(entity_df, anomaly_ranks)):
        out = out.merge(extra, on="entity_id", how="left")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--year", type=int, help="Single panel year (e.g. 2024)")
    ap.add_argument("--all-years", action="store_true",
                    help="Every year with a local entity_graph_{year}.parquet")
    ap.add_argument("--entity-dir", type=Path, default=ENTITY_GRAPH_DIR)
    ap.add_argument("--partb-dir", type=Path, default=PARTB_DIR)
    ap.add_argument("--partb-svc-dir", type=Path, default=PARTB_SVC_DIR)
    ap.add_argument("--out-dir", type=Path, default=OUTPUT_DIR)
    ap.add_argument("--dry-run", action="store_true", help="Compute but don't write parquet")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.all_years:
        years = available_entity_graph_years(args.entity_dir)
        if not years:
            LOG.error("no entity_graph_*.parquet found under %s", args.entity_dir)
            return 2
    elif args.year:
        years = [args.year]
    else:
        LOG.error("specify --year YYYY or --all-years")
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)

    for y in years:
        try:
            df = compute_year(y, entity_dir=args.entity_dir, partb_dir=args.partb_dir,
                              partb_svc_dir=args.partb_svc_dir)
        except FileNotFoundError as e:
            LOG.error("  %d: %s -- skipping", y, e)
            continue
        LOG.info("  %d: %d entities computed", y, len(df))
        if not args.dry_run:
            out_path = args.out_dir / f"entity_features_{y}.parquet"
            df.to_parquet(out_path, index=False)
            LOG.info("  %d: wrote -> %s", y, out_path)
        else:
            LOG.info("  %d: --dry-run, not writing", y)

    return 0


if __name__ == "__main__":
    sys.exit(main())
