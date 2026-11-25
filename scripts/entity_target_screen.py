#!/usr/bin/env python3
"""Entity target screen — turn P4 org-head scores into a reviewable SHORTLIST.

Consumes results/entity_head/entity_scores_{year}.parquet (train_entity_head.py)
and applies the deterministic half of the qui tam counsel qui-tam value gates:

1. NOVELTY (government-known exclusion): drop every org that appears in ANY
   DOJ-derived list we hold — confirmed labels (entity_labels.jsonl), grounded
   press-release candidates (validation/curator/grounded_candidates_*.jsonl),
   curator side-files, and gold_set NPIs. A government-known org has ZERO
   qui-tam value regardless of score: labels are training signal, targets are
   the unlabeled lookalikes. (True novelty vs prior sealed qui tams / PACER is
   NOT checkable here — that stays the investigator loop's Guardrail work.)
2. COLLECTIBILITY proxy: Type-2 organizations (default) above a Medicaid-pay
   floor — a $40k/yr sole proprietor is not a collectible FCA defendant.
3. REVIEWABILITY enrichment: NPPES org name/state, entity-cluster size when
   the org bridges to a Part-B cluster, and the concentration/state-percentile
   fields a human needs to smell the theory (why is THIS org weird vs its own
   class in its own state).

Output: results/entity_head/target_shortlist_<date>.{jsonl,md} — top-N leads
for human review -> innocent-explanation pre-screen -> ONE investigator run.
This script is a FILTER + REPORT; it never launches investigations and never
writes labels.

Usage:
    python scripts/entity_target_screen.py                       # 2024, top 25
    python scripts/entity_target_screen.py --year 2024 --top 40 --min-pay 250000
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import logging
import sys
from pathlib import Path

import duckdb
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG = logging.getLogger("target_screen")

SCORES_DIR = REPO_ROOT / "results" / "entity_head"
LABELS_PATH = REPO_ROOT / "data" / "extracted" / "entity_labels.jsonl"
CURATOR_GLOBS = [
    str(REPO_ROOT / "validation" / "curator" / "grounded_candidates_*.jsonl"),
    str(REPO_ROOT / "validation" / "curator" / "entity_labels_CONFIRMED_*.jsonl"),
    str(REPO_ROOT / "validation" / "curator" / "entity_medicaid_*.jsonl"),
]
GOLD_SET_PATH = REPO_ROOT / "data" / "extracted" / "gold_set.jsonl"
NPPES_PATH = REPO_ROOT / "data" / "registries" / "nppes_bulk.parquet"
MEDICAID_DIR = REPO_ROOT / "data" / "processed"
ENTITY_FEATURES_DIR = REPO_ROOT / "data" / "processed"


def known_npis(labels_path: Path = LABELS_PATH,
               curator_globs: list[str] | None = None,
               gold_set_path: Path = GOLD_SET_PATH) -> set[str]:
    """Every NPI that is government-known via any DOJ-derived list we hold.
    Pure + testable: callers can pass tmp paths."""
    known: set[str] = set()
    if labels_path.exists():
        for line in labels_path.open():
            if line.strip():
                npi = json.loads(line).get("org_npi")
                if npi:
                    known.add(str(npi))
    for pattern in (curator_globs if curator_globs is not None
                    else CURATOR_GLOBS):
        for fp in glob.glob(pattern):
            for line in Path(fp).open():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for key in ("org_npi", "npi"):
                    if row.get(key):
                        known.add(str(row[key]))
    if gold_set_path.exists():
        for line in gold_set_path.open():
            if line.strip():
                try:
                    npi = json.loads(line).get("npi")
                except json.JSONDecodeError:
                    continue
                if npi:
                    known.add(str(npi))
    return known


def apply_gates(df: pd.DataFrame, known: set[str], *, min_pay: float,
                orgs_only: bool = True) -> pd.DataFrame:
    """Novelty + collectibility gates. Returns the surviving rows (score order
    preserved) with a `dropped_` audit of counts attached as attrs."""
    out = df.copy()
    n0 = len(out)
    out = out[~out["labeled"].fillna(False)]
    n_labeled = n0 - len(out)
    out = out[~out["npi"].astype(str).isin(known)]
    n_known = n0 - n_labeled - len(out)
    if orgs_only:
        out = out[out["entity_type"].astype("string") == "2"]
    n_type = n0 - n_labeled - n_known - len(out)
    out = out[pd.to_numeric(out["medicaid_total_pay"], errors="coerce") >= min_pay]
    out.attrs["funnel"] = {
        "n_scored": n0, "dropped_labeled": n_labeled,
        "dropped_government_known": n_known, "dropped_type": n_type,
        "dropped_below_pay_floor": n0 - n_labeled - n_known - n_type - len(out),
        "n_survivors": len(out),
    }
    return out


def enrich(con: duckdb.DuckDBPyConnection, df: pd.DataFrame, year: int) -> pd.DataFrame:
    """NPPES name/state + medicaid review fields + entity-cluster size."""
    ids = ",".join(f"'{n}'" for n in df["npi"].astype(str))
    npp = con.execute(f"""
        SELECT CAST(npi AS VARCHAR) AS npi, Provider_Organization_Name AS org_name,
               state AS nppes_state
        FROM read_parquet('{NPPES_PATH}') WHERE CAST(npi AS VARCHAR) IN ({ids})
    """).fetchdf()
    med = con.execute(f"""
        SELECT CAST(npi AS VARCHAR) AS npi, top_hcpcs, top1_code_share,
               top1_code_share_pctile_in_class_state, medicaid_total_pay_pctile_in_class_state,
               yoy_growth, provider_state
        FROM read_parquet('{MEDICAID_DIR / f"medicaid_features_{year}.parquet"}')
        WHERE CAST(npi AS VARCHAR) IN ({ids})
    """).fetchdf()
    out = df.merge(npp, on="npi", how="left").merge(med, on="npi", how="left")
    ef = ENTITY_FEATURES_DIR / f"entity_features_{year}.parquet"
    if ef.exists() and out["entity_id"].notna().any():
        ent = con.execute(f"select entity_id, n_members from read_parquet('{ef}')").fetchdf()
        out = out.merge(ent, on="entity_id", how="left")
    else:
        out["n_members"] = pd.NA
    return out


def write_report(df: pd.DataFrame, funnel: dict, out_dir: Path, year: int,
                 top: int) -> tuple[Path, Path]:
    date = datetime.date.today().isoformat()
    jl = out_dir / f"target_shortlist_{date}.jsonl"
    md = out_dir / f"target_shortlist_{date}.md"
    rows = df.head(top)
    with jl.open("w") as f:
        for _, r in rows.iterrows():
            f.write(json.dumps({k: (None if pd.isna(v) else v) for k, v in
                                r.to_dict().items()}, default=str) + "\n")
    lines = [
        f"# Entity target shortlist — {date} (score year {year})",
        "",
        "**What this is:** the top UNLABELED organizations by the P4 org-head score after the",
        "deterministic qui tam counsel gates (government-known excluded; Type-2 orgs; Medicaid-pay floor).",
        "**What this is NOT:** cases. The model is label-starved (48 labels) until the DOJ-harvest",
        "corpus lands; prior-qui-tam/PACER novelty and innocent-explanation foreclosure are NOT",
        "checked here — that is the pre-flight + investigator loop's job. Pick ONE for pre-flight.",
        "",
        f"Funnel: {funnel}",
        "",
        "| TIER | ANCHOR | MO | NPI | org (NPPES) | st | Medicaid pay | top code | E&M% |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    tier_lbl = {3: "★3 FILE", 2: "2 refer", 1: "1"}
    for _, r in rows.iterrows():
        pay = r.get("medicaid_total_pay")
        pay = f"${pay:,.0f}" if pd.notna(pay) else "—"
        mo = str(r.get("best_mo") or "—")[:18]
        em = r.get("em_share")
        em = f"{em:.0%}" if pd.notna(em) else "—"
        anc = str(r.get("anchor_ids"))[:16] if r.get("anchor_fileable") else "—"
        tier = tier_lbl.get(int(r.get("fileability_tier") or 1), "1")
        lines.append(
            f"| {tier} | {anc} | {mo} | {r['npi']} | {str(r.get('org_name') or '?')[:30]} "
            f"| {r.get('provider_state') or r.get('nppes_state') or '?'} "
            f"| {pay} | {r.get('top_hcpcs')} | {em} |")
    lines.append("")
    md.write_text("\n".join(lines))
    return jl, md


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--min-pay", type=float, default=500_000.0,
                    help="Medicaid-pay collectibility floor (default $500k/yr)")
    ap.add_argument("--include-individuals", action="store_true",
                    help="keep Type-1 NPIs too (default: Type-2 orgs only)")
    ap.add_argument("--scores", type=Path, default=None,
                    help="default: results/entity_head/entity_scores_{year}.parquet")
    ap.add_argument("--out-dir", type=Path, default=SCORES_DIR)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    scores_path = args.scores or (SCORES_DIR / f"entity_scores_{args.year}.parquet")
    if not scores_path.exists():
        LOG.error("scores parquet missing: %s (run train_entity_head.py --score-year)", scores_path)
        return 2
    df = pd.read_parquet(scores_path).sort_values("rank")
    known = known_npis()
    LOG.info("government-known NPIs excluded: %d", len(known))
    survivors = apply_gates(df, known, min_pay=args.min_pay,
                            orgs_only=not args.include_individuals)
    funnel = survivors.attrs["funnel"]
    LOG.info("funnel: %s", funnel)
    con = duckdb.connect()
    # ANCHOR PRE-PASS over ALL survivors (cheap top_hcpcs join): the fileable
    # band (UDT labs etc.) often scores below the top-150 by model score, so
    # without this the anchor sort key never sees its own candidates. Tag every
    # survivor's dominant code -> anchor_fileable, then enrich (a) the top by
    # score AND (b) the top anchor-fileable by score.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from fraudlib.anchors import fileable as _fileable
    med_path = MEDICAID_DIR / f"medicaid_features_{args.year}.parquet"
    sids = ",".join(f"'{n}'" for n in survivors["npi"].astype(str))
    codes = con.execute(
        f"SELECT CAST(npi AS VARCHAR) npi, top_hcpcs FROM read_parquet('{med_path}') "
        f"WHERE CAST(npi AS VARCHAR) IN ({sids})").fetchdf()
    code_map = dict(zip(codes["npi"], codes["top_hcpcs"]))
    survivors = survivors.copy()
    survivors["_anchor_pre"] = [bool(_fileable(code_map.get(str(n))))
                                for n in survivors["npi"]]
    n_fileable = int(survivors["_anchor_pre"].sum())
    LOG.info("survivors in fileable band (public coverage anchor): %d", n_fileable)
    to_enrich = pd.concat([
        survivors.head(max(args.top * 4, 150)),
        survivors[survivors["_anchor_pre"]].head(max(args.top * 2, 40)),
    ]).drop_duplicates(subset=["npi"])
    enriched = enrich(con, to_enrich, args.year)

    # ── DOCTRINE (feedback_scheme_mo_not_math_extremity): target by scheme-MO
    # configuration, not math extremity. (1) drop public/government billers —
    # a state can't defraud itself onto our shortlist; (2) score each candidate
    # against conviction-MO correlates and RE-RANK by MO-match first, dollars
    # second — never a lone percentile. p99 single-axis outliers are what state
    # PI units already screen (low qui-tam novelty); the value is the
    # camouflaged middle with a recognizable MO.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts.mo_signatures import PUBLIC_RE, score_one
    pub = enriched["org_name"].fillna("").str.contains(PUBLIC_RE)
    if pub.any():
        LOG.info("dropping %d public/government entities", int(pub.sum()))
    enriched = enriched[~pub].copy()
    # MO scoring re-scans the T-MSIS parquet per NPI, so bound it to the top by
    # model score PLUS every fileable-band survivor (already tagged above).
    pool = pd.concat([
        enriched.head(max(args.top * 2, 24)),
        enriched[enriched["_anchor_pre"]].head(max(args.top, 12)),
    ]).drop_duplicates(subset=["npi"])
    head = pool.copy()
    mo = [score_one(con, str(n), args.year) for n in head["npi"]]
    head["best_mo"] = [m.get("mo_match", {}).get("best_mo") for m in mo]
    head["mo_correlates"] = [m.get("mo_match", {}).get("total_correlates", 0) for m in mo]
    head["recruitment_shape"] = [m.get("recruitment_shape") for m in mo]
    head["blanket_top_share"] = [m.get("blanket_top_share") for m in mo]
    head["orders_and_renders"] = [bool(m.get("orders_and_renders")) for m in mo]
    head["em_share"] = [m.get("em_share") for m in mo]
    # ANCHOR REGISTRY (the Lab Ebony lesson): a target whose dominant code family
    # carries a public coverage-rule condition (fileable_band) outranks anchorless
    # extremity — that band is where public-data cases can actually be FILED.
    from fraudlib.anchors import anchor_for_code
    anch = [anchor_for_code(c) for c in head["top_hcpcs"]]
    head["anchor_fileable"] = [bool(a and a.get("fileable_band")) for a in anch]
    head["anchor_ids"] = [",".join(a.get("anchor_ids", [])) if a and a.get("fileable_band") else "" for a in anch]
    # FILEABILITY TIER (Lab Ebony A/B): 3 = anchor + orders-and-renders (integrated
    # clinic controls the order -> Rowan-FILEABLE); 2 = anchor + pass-through
    # (reference lab -> strong referral only); 1 = no public anchor. Rank tier 3
    # first — that is the band that actually reaches FILE on public data.
    head["fileability_tier"] = [
        3 if (a and o) else (2 if a else 1)
        for a, o in zip(head["anchor_fileable"], head["orders_and_renders"])]
    enriched = head.sort_values(
        ["fileability_tier", "mo_correlates", "score"], ascending=[False, False, False])

    jl, md = write_report(enriched, funnel, args.out_dir, args.year, args.top)
    LOG.info("shortlist -> %s / %s", jl, md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
