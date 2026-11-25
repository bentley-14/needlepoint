"""Panel-wide entity resolution — NPIs -> connected-component ``entity_id``.

Generalizes ``scripts/ring_enrichment.py`` (a per-NPI, on-demand tool: "given this
ONE flagged NPI, who's its ring?") into a PANEL-WIDE graph so every NPI in a year's
Part B panel gets an ``entity_id`` up front — rings/orgs/suites become first-class
objects the ranker/loop/console can query directly, instead of re-deriving them
per investigation.

This is the resolution LAYER ONLY: no scoring head (P4), no loop/evidence-pack
wiring (P5), and it does not touch ``feature_frames.py``'s Type-2 drop flag.

Edge sources wired (mirrors ``ring_enrichment.py``'s two entity sources, generalized
panel-wide, plus the third source the spec calls out — practice address, already
used by ``scripts/cluster_queue.py``):

  (a) shared **Org_PAC_ID**       — CMS DAC/physician_compare "same billing entity"
                                     (``ring_enrichment.py::_dac_entity``).
  (b) shared **PECOS reassignment group** — DEFERRED. ``scripts/pecos_lookup.py``'s
                                     reassignment dataset is a LIVE per-NPI /
                                     per-Group-Enrollment-ID API
                                     (``REASSIGNMENT_API_URL``), not a bulk table —
                                     there is no local or R2 parquet mirror to join
                                     panel-wide (confirmed by inspection: no bulk
                                     PECOS file under data/raw, data/registries, or
                                     R2 processed/v1). Panel-wide use would mean one
                                     HTTP call per panel NPI, which does not scale to
                                     a ~200k-NPI panel. Follow-up: if CMS ever ships
                                     a bulk reassignment extract (or one gets mirrored
                                     to R2), wire it here as a third ``edge_type``
                                     exactly like (a)/(c) below (the union-find
                                     already generalizes to N edge sources).
  (c) shared **practice address**  — exact Part-B panel ``Rndrng_Prvdr_St1`` +
                                     ``Rndrng_Prvdr_Zip5`` match, the same exact-match
                                     convention ``scripts/cluster_queue.py`` uses for
                                     triage-queue clustering, generalized to the
                                     WHOLE panel rather than just HIGH/MODERATE queue
                                     items.

Connected components over the union of these edges (via union-find) become
entities. Both a shared address AND a shared Org_PAC_ID are much weaker signals
at panel width than at queue-width or single-seed width (a triage queue is
pre-filtered to a few hundred flagged NPIs; ``ring_enrichment.py`` looks at one
seed NPI at a time). At full-panel scale two distinct over-merge failure modes
showed up empirically when this was first run against the real 2024 panel and
had to be guarded against:

  1. **Address megabuilding** — a single street+ZIP shared by dozens/hundreds of
     unrelated tenants (a hospital, a large multi-specialty medical office
     building) must not collapse into one fake entity. See
     ``ADDRESS_MEGACLUSTER_CAP``.
  2. **Org_PAC_ID emergent mega-component (transitive chaining)** — the DAC's
     grain is NPI x Org_PAC_ID x address (a physician enrolled at several
     practice locations gets one row per enrollment), so a small fraction of
     NPIs carry many (sometimes 100+) distinct Org_PAC_ID memberships. Even
     after excluding any single oversized Org_PAC_ID group (mirroring the
     address guard, see ``ORG_PAC_MEGACLUSTER_CAP``), transitive closure through
     many individually-reasonable-sized groups bridged by these multi-enrolled
     NPIs still produced ONE connected component covering ~34% of the 2024
     panel in testing (down from ~88% before the two per-key caps, but still
     not a coherent "ring"). This is a known limitation of transitive-closure
     clustering on shared attributes (small-world percolation), not a bug in
     any single edge; ``MAX_ENTITY_SIZE`` is a final backstop that FLAGS (does
     not silently ship as a normal small entity) any resolved component beyond
     a size no real fraud ring plausibly reaches, so a downstream consumer can
     filter it out rather than being misled by a giant "entity."

Output: ``data/processed/entity_graph_{year}.parquet`` — one row per panel NPI:
``npi``, ``entity_id`` (stable, deterministic: ``"ENT_" + min(member npi)``),
``edge_types_joined`` (comma-joined sorted edge types that actually connected this
NPI to another member; empty string for an isolated/singleton entity).

Usage:
    python -m fraudlib.entity.resolve --year 2024
    python -m fraudlib.entity.resolve --all-years
    python -m fraudlib.entity.resolve --year 2024 --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fraudlib import r2  # noqa: E402

LOG = logging.getLogger("fraudlib.entity.resolve")

# Address keys with MORE than this many distinct member NPIs are excluded from
# clustering entirely (flagged, not collapsed) -- see module docstring. Red Sun's
# own signature is 3 NPIs at one suite; a real single-tenant ring/suite is small.
# Anything bigger is much more likely a shared office tower / hospital campus /
# large multi-specialty group with no common ownership -- letting those merge
# would fabricate one giant fake "entity" out of unrelated tenants.
ADDRESS_MEGACLUSTER_CAP = 20

# Same guard, applied to Org_PAC_ID groups: a legitimate group practice can be
# bigger than a shared-suite address (CMS enrollment, not incidental co-location),
# so the cap is looser -- but a group in the thousands is a regional/national
# health system, not a "ring" this layer can usefully treat as one entity.
ORG_PAC_MEGACLUSTER_CAP = 50

# Final backstop (see module docstring point 2): even with both per-key caps
# above, transitive chaining through multi-enrolled "hub" NPIs can still merge
# many individually-reasonable-sized groups into one emergent giant component.
# Any FINAL resolved component bigger than this is flagged (edge_type
# "component" in the megaclusters report) rather than silently shipped as a
# normal entity -- it is never a coherent, investigable ring at this size.
MAX_ENTITY_SIZE = 200

PARTB_DIR = REPO_ROOT / "data/processed"
OUTPUT_DIR = REPO_ROOT / "data/processed"


# ─────────────────────────────────────────────────────────────────────────────
# Union-find -- pure, no I/O. Nodes are either real NPI strings or synthetic
# "<edge_type>:<key>" group nodes; resolve_components() only ever reads back
# roots for REAL npis, so it doesn't matter whether a synthetic node or an NPI
# ends up as a component's root.
# ─────────────────────────────────────────────────────────────────────────────
class _UnionFind:
    """Path-compressed union-find over string node ids."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


# ─────────────────────────────────────────────────────────────────────────────
# Pure derivation -- takes a panel NPI list + an edge-row frame (from DuckDB, or
# a synthetic fixture in tests) and resolves connected components. No file I/O,
# no DuckDB -- this is what tests/test_entity_resolution.py exercises directly.
# ─────────────────────────────────────────────────────────────────────────────
_KEY_CAPS = {"address": ADDRESS_MEGACLUSTER_CAP, "org_pac": ORG_PAC_MEGACLUSTER_CAP}


def resolve_components(
    panel_npis: list[str],
    edge_rows: pd.DataFrame,
    cap: int = ADDRESS_MEGACLUSTER_CAP,
    org_pac_cap: int = ORG_PAC_MEGACLUSTER_CAP,
    max_entity_size: int = MAX_ENTITY_SIZE,
) -> tuple[pd.DataFrame, list[dict]]:
    """Resolve panel NPIs into connected-component entities.

    Parameters
    ----------
    panel_npis : the full node set -- every NPI in the panel, including ones
        with zero edge_rows (they surface as size-1 singleton entities).
    edge_rows : one row per (npi, key) membership offered by an edge source.
        Required columns: ``npi``, ``edge_type`` (e.g. "org_pac", "address"),
        ``key`` (the shared value, e.g. an Org_PAC_ID or "street|zip5"). Two
        NPIs with a row sharing the same (edge_type-implied) key are unioned.
    cap : "address"-type keys with more than `cap` distinct member NPIs are
        EXCLUDED from clustering (never unioned) -- a shared office TOWER must
        not collapse dozens/hundreds of unrelated tenants into one fake entity.
    org_pac_cap : same guard for "org_pac"-type keys (looser -- a legitimate
        group practice can be bigger than a shared suite -- but a group in the
        thousands is a health system, not a "ring").
    max_entity_size : FINAL backstop applied to the resolved components
        themselves (after union-find), regardless of which edge type(s)
        contributed -- transitive chaining through multi-enrolled NPIs can
        still merge many individually-capped groups into one emergent giant
        component (see module docstring). Any component bigger than this is
        reported in ``megaclusters`` with ``edge_type: "component"`` -- FLAGGED,
        not silently shipped as an ordinary small entity -- but still keeps one
        (marked) entity_id, since un-merging an emergent component correctly
        would need real graph partitioning, out of scope for this layer.

    Returns
    -------
    (entity_df, megaclusters):
        entity_df -- one row per panel_npis entry (the persisted 3-column
        contract): [npi, entity_id, edge_types_joined].
        megaclusters -- [{"edge_type": "address"|"org_pac", "key": ...,
        "n_members": ...}, ...] for every key excluded for exceeding its cap,
        PLUS one {"edge_type": "component", "key": entity_id, "n_members": ...}
        row per final component exceeding `max_entity_size` -- this is the
        FLAG (a megacluster is reported here, never silently collapsed into an
        ordinary-looking entity row; callers that want a per-NPI boolean can
        join entity_df.entity_id against these keys/entity_ids).
    """
    panel_npis = sorted({str(n) for n in panel_npis})
    panel_set = set(panel_npis)

    if edge_rows is None or len(edge_rows) == 0:
        edge_rows = pd.DataFrame(columns=["npi", "edge_type", "key"])
    edge_rows = edge_rows.copy()
    edge_rows["npi"] = edge_rows["npi"].astype(str)

    caps = {"address": cap, "org_pac": org_pac_cap}

    # ── megacluster guard: oversized keys (either edge type) excluded up front ──
    megaclusters: list[dict] = []
    oversized: set[str] = set()   # "<edge_type>:<key>" -- namespaced so an address
                                   # key and an org_pac key can never collide
    shared: set[str] = set()      # "<edge_type>:<key>" keys held by >=2 distinct
                                   # panel NPIs -- i.e. keys that actually connect
                                   # two members, vs. one NPI's own unshared
                                   # address/org_pac value (a key held by exactly
                                   # one NPI doesn't "connect" it to anyone and
                                   # must not appear in that NPI's
                                   # edge_types_joined -- see docstring contract:
                                   # "edge types that actually connected this NPI
                                   # to another member").
    for etype, this_cap in caps.items():
        sub = edge_rows[edge_rows["edge_type"] == etype]
        if not len(sub):
            continue
        counts = sub.groupby("key")["npi"].nunique()
        for key, n in counts[counts > this_cap].items():
            oversized.add(f"{etype}:{key}")
            megaclusters.append({"edge_type": etype, "key": key, "n_members": int(n)})
        for key, n in counts[counts >= 2].items():
            shared.add(f"{etype}:{key}")

    # ── union-find over panel NPIs + synthetic "<edge_type>:<key>" nodes ─────
    uf = _UnionFind()
    for n in panel_npis:
        uf.find(n)  # register every panel NPI even with zero edges -> singleton

    contributed: dict[str, set[str]] = {n: set() for n in panel_npis}
    for row in edge_rows.itertuples(index=False):
        npi, etype, key = row.npi, row.edge_type, row.key
        if npi not in panel_set:
            continue  # defensive: edge rows should already be panel-scoped
        node_key = f"{etype}:{key}"
        if node_key in oversized:
            continue  # excluded -- too common to be a meaningful entity signal
        uf.union(npi, node_key)
        if node_key in shared:
            contributed[npi].add(etype)

    roots = {n: uf.find(n) for n in panel_npis}
    groups: dict[str, list[str]] = {}
    for n, r in roots.items():
        groups.setdefault(r, []).append(n)
    entity_id_of_root = {r: f"ENT_{min(members)}" for r, members in groups.items()}

    # ── final backstop: flag (not un-merge) any component beyond max_entity_size ──
    for r, members in groups.items():
        if len(members) > max_entity_size:
            megaclusters.append({"edge_type": "component", "key": entity_id_of_root[r],
                                  "n_members": len(members)})

    out = pd.DataFrame({
        "npi": panel_npis,
        "entity_id": [entity_id_of_root[roots[n]] for n in panel_npis],
        "edge_types_joined": [",".join(sorted(contributed[n])) for n in panel_npis],
    })
    return out, megaclusters


# ─────────────────────────────────────────────────────────────────────────────
# I/O -- DuckDB fetch (local Part-B panel + R2 physician_compare). Heavy
# aggregation happens in SQL; only the collapsed (npi, edge_type, key) rows
# touch pandas.
# ─────────────────────────────────────────────────────────────────────────────
def physician_compare_source() -> str:
    """Resolve the DAC/physician_compare parquet source: local override else R2
    (mirrors ``scripts/physician_compare_lookup.py::_source(None)`` for the
    un-dated current snapshot -- reused by ``scripts/compute_entity_features.py``
    so both scripts resolve the SAME DAC snapshot the SAME way)."""
    local = REPO_ROOT / "data/registries/physician_compare.parquet"
    return r2.parquet_source(local, f"{r2.R2_PANELS}/physician_compare.parquet")


def partb_path(year: int, partb_dir: Path = PARTB_DIR) -> Path:
    return partb_dir / f"cms_partb_{year}.parquet"


def available_partb_years(partb_dir: Path = PARTB_DIR) -> list[int]:
    """Years with a local ``cms_partb_{year}.parquet`` (drives ``--all-years``)."""
    years = []
    for p in partb_dir.glob("cms_partb_*.parquet"):
        tail = p.stem.rsplit("_", 1)[-1]
        if tail.isdigit():
            years.append(int(tail))
    return sorted(years)


def fetch_panel_npis(con, partb_src: str) -> list[str]:
    df = con.execute(
        f"SELECT DISTINCT CAST(Rndrng_NPI AS VARCHAR) AS npi FROM read_parquet('{partb_src}')"
    ).fetchdf()
    return df["npi"].tolist()


def fetch_address_edges(con, partb_src: str) -> pd.DataFrame:
    """(npi, 'address', 'St1|Zip5') for every panel NPI with a usable address.

    Exact-match convention (TRIM only, no case-folding) -- mirrors
    ``scripts/cluster_queue.py::_panel_address_map`` / ``build_report``.
    """
    return con.execute(f"""
        SELECT DISTINCT CAST(Rndrng_NPI AS VARCHAR) AS npi,
               'address' AS edge_type,
               TRIM(Rndrng_Prvdr_St1) || '|' || TRIM(CAST(Rndrng_Prvdr_Zip5 AS VARCHAR)) AS key
        FROM read_parquet('{partb_src}')
        WHERE Rndrng_Prvdr_St1 IS NOT NULL AND TRIM(Rndrng_Prvdr_St1) <> ''
          AND Rndrng_Prvdr_Zip5 IS NOT NULL AND TRIM(CAST(Rndrng_Prvdr_Zip5 AS VARCHAR)) <> ''
    """).fetchdf()


def fetch_org_pac_edges(con, partb_src: str, dac_src: str) -> pd.DataFrame:
    """(npi, 'org_pac', Org_PAC_ID) for panel NPIs enrolled under a group.

    Scoped to panel NPIs on BOTH sides (mirrors ``ring_enrichment.py::_dac_entity``'s
    Org_PAC_ID join, generalized panel-wide instead of per-seed-NPI): the DAC
    snapshot is not year-keyed (see the lookup script's own docstring), so this
    reflects the CURRENT snapshot regardless of the panel year, same limitation
    ``ring_enrichment.py`` already has.
    """
    # NB: Org_PAC_ID IS NOT NULL alone is not enough -- ~10% of DAC rows carry an
    # EMPTY STRING (not SQL NULL) Org_PAC_ID (confirmed by inspection against the
    # live R2 file), which would otherwise union ~350k unrelated NPIs into one
    # fake entity through the empty key. TRIM(...) <> '' excludes it.
    return con.execute(f"""
        SELECT DISTINCT CAST(dac.npi AS VARCHAR) AS npi,
               'org_pac' AS edge_type,
               CAST(dac.Org_PAC_ID AS VARCHAR) AS key
        FROM read_parquet('{dac_src}') AS dac
        WHERE dac.Org_PAC_ID IS NOT NULL AND TRIM(CAST(dac.Org_PAC_ID AS VARCHAR)) <> ''
          AND CAST(dac.npi AS VARCHAR) IN (
              SELECT DISTINCT CAST(Rndrng_NPI AS VARCHAR) FROM read_parquet('{partb_src}')
          )
    """).fetchdf()


def resolve_year(
    year: int,
    partb_dir: Path = PARTB_DIR,
    cap: int = ADDRESS_MEGACLUSTER_CAP,
    org_pac_cap: int = ORG_PAC_MEGACLUSTER_CAP,
    max_entity_size: int = MAX_ENTITY_SIZE,
) -> tuple[pd.DataFrame, list[dict]]:
    """Fetch + resolve the full entity graph for one year's Part-B panel."""
    src = str(partb_path(year, partb_dir))
    con = r2.duckdb_r2()

    LOG.info("  %d: panel NPIs...", year)
    panel_npis = fetch_panel_npis(con, src)
    LOG.info("  %d: %d panel NPIs", year, len(panel_npis))

    LOG.info("  %d: address edges (Part-B St1+Zip5)...", year)
    addr_edges = fetch_address_edges(con, src)
    LOG.info("  %d: org_pac edges (DAC Org_PAC_ID)...", year)
    org_edges = fetch_org_pac_edges(con, src, physician_compare_source())

    edge_rows = pd.concat([addr_edges, org_edges], ignore_index=True)
    return resolve_components(panel_npis, edge_rows, cap=cap,
                               org_pac_cap=org_pac_cap, max_entity_size=max_entity_size)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--year", type=int, help="Single panel year (e.g. 2024)")
    ap.add_argument("--all-years", action="store_true",
                     help="Every year with a local cms_partb_{year}.parquet")
    ap.add_argument("--address-cap", type=int, default=ADDRESS_MEGACLUSTER_CAP,
                     help=f"Address-megacluster exclusion threshold (default {ADDRESS_MEGACLUSTER_CAP})")
    ap.add_argument("--org-pac-cap", type=int, default=ORG_PAC_MEGACLUSTER_CAP,
                     help=f"Org_PAC_ID-megacluster exclusion threshold (default {ORG_PAC_MEGACLUSTER_CAP})")
    ap.add_argument("--max-entity-size", type=int, default=MAX_ENTITY_SIZE,
                     help=f"Final component-size backstop flag threshold (default {MAX_ENTITY_SIZE})")
    ap.add_argument("--out-dir", type=Path, default=OUTPUT_DIR)
    ap.add_argument("--dry-run", action="store_true", help="Resolve but don't write parquet")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.all_years:
        years = available_partb_years()
        if not years:
            LOG.error("no cms_partb_*.parquet found under %s", PARTB_DIR)
            return 2
    elif args.year:
        years = [args.year]
    else:
        LOG.error("specify --year YYYY or --all-years")
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)

    for y in years:
        if not partb_path(y).exists():
            LOG.error("  %d: %s not found -- skipping", y, partb_path(y))
            continue
        df, megaclusters = resolve_year(y, cap=args.address_cap, org_pac_cap=args.org_pac_cap,
                                         max_entity_size=args.max_entity_size)
        n_entities = df["entity_id"].nunique()
        n_multi = (df.groupby("entity_id").size() > 1).sum()
        by_type = {}
        for mc in megaclusters:
            by_type[mc["edge_type"]] = by_type.get(mc["edge_type"], 0) + 1
        LOG.info("  %d: %d NPIs -> %d entities (%d multi-member) -- megaclusters flagged: %s",
                  y, len(df), n_entities, n_multi, by_type or "none")
        if not args.dry_run:
            out_path = args.out_dir / f"entity_graph_{y}.parquet"
            df.to_parquet(out_path, index=False)
            LOG.info("  %d: wrote -> %s", y, out_path)
            if megaclusters:
                mc_path = args.out_dir / f"entity_graph_{y}.megaclusters.json"
                mc_path.write_text(json.dumps(megaclusters, indent=2))
                LOG.info("  %d: wrote -> %s (%d flagged megacluster(s))",
                          y, mc_path, len(megaclusters))
        else:
            LOG.info("  %d: --dry-run, not writing", y)

    return 0


if __name__ == "__main__":
    sys.exit(main())
