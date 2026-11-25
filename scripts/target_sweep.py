#!/usr/bin/env python3
"""Anchor-gated target sweep — productionized (T3c).

Same funnel as the original one-off analysis script: panel ranks (+ skin-sub
exclusion) -> per-candidate dominant HCPCS codes -> anchor_check (falsity-
anchor existence) -> ring_enrichment (entity/ring + novelty) -> JSONL +
state/ring_cache/. Differences from the one-off:

  - `--ranks A-B` and `--skip-npis-file` are real CLI parameters (no more
    hand-edited hardcoded rank cutoff / DONE set baked into the script).
  - The dominant-codes lookup was ONE R2 (S3-backed) query PER CANDIDATE NPI
    — real network round-trip latency per query. Batched into ONE
    ``IN (...)`` query per <= 500-NPI chunk (``--batch-size``), grouped by
    NPI in Python. `ring_enrichment.enrich_ring` stays per-candidate (it's a
    separate, already-scoped local+API lookup, not R2 panel I/O).
  - Output path is a real ``--output`` flag (dynamic default, not a
    filename with a hardcoded date baked in from when the script was written).

Usage:
    python scripts/target_sweep.py --ranks 16-120
    python scripts/target_sweep.py --ranks 1-500 --skip-npis-file done_npis.txt \
        --year 2024 --output state/target_sweep_2026-07-15.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from ring_enrichment import enrich_ring  # noqa: E402

try:
    from fraudlib import r2
except Exception:  # calibration jail / standalone copy: no fraudlib -> local-only, never R2
    class _LocalR2:
        @staticmethod
        def duckdb_r2():
            import duckdb
            return duckdb.connect()
    r2 = _LocalR2()

DEFAULT_SKIN_MAX = 0.3
DEFAULT_BATCH_SIZE = 500
DEFAULT_TOP_N_CODES = 2
DEFAULT_YEAR = 2024
ANCHOR_TIMEOUT_SEC = 120
_NPI_RE = re.compile(r"^[12]\d{9}$")


def parse_rank_range(s: str) -> tuple[int, int]:
    """'"A-B"' -> (A, B), validated A <= B, both >= 1."""
    m = re.fullmatch(r"(\d+)-(\d+)", s.strip())
    if not m:
        raise ValueError(f"--ranks must be 'A-B' (e.g. '16-120'), got {s!r}")
    lo, hi = int(m.group(1)), int(m.group(2))
    if lo < 1 or hi < lo:
        raise ValueError(f"--ranks 'A-B' must have 1 <= A <= B, got {s!r}")
    return lo, hi


def find_panel(root: Path, year: int) -> Path:
    """Locate the scored walk-forward panel for `year` (sorted glob, first
    match — mirrors the original one-off's tie-break). Raises a clear error
    on no match instead of a bare IndexError."""
    pattern = str(root / f"results/walk_forward_v4_top5_y{year}_*/scored_full_{year}.parquet")
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"no scored panel found for year={year} (pattern: {pattern})")
    return Path(matches[0])


def load_skip_npis(path: "str | Path | None") -> set[str]:
    """One NPI per line; blank lines and '#'-prefixed comments skipped."""
    if not path:
        return set()
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"--skip-npis-file not found: {p}")
    out = set()
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.split()[0])
    return out


def select_candidates(con, panel_path: Path, rank_lo: int, rank_hi: int,
                      skin_max: float = DEFAULT_SKIN_MAX):
    """Panel ranks [rank_lo, rank_hi] (by score_rrf desc), excluding
    high-skin-substitute payers (already-covered theory, per the panel's
    parked history). rank_lo/rank_hi are argparse-validated ints — safe to
    inline (mirrors the original script's own un-parameterized style)."""
    return con.execute(f"""
        WITH r AS (
          SELECT CAST(npi AS VARCHAR) npi,
                 trim(UPPER(COALESCE(partb_rndrng_prvdr_first_name,''))||' '||
                      UPPER(COALESCE(partb_rndrng_prvdr_last_org_name,''))) nm,
                 partb_provider_type sp, partb_state st,
                 COALESCE(cl_skin_sub_pay_share,0) skin,
                 ROW_NUMBER() OVER (ORDER BY score_rrf DESC) rnk
          FROM read_parquet('{panel_path}')
        )
        SELECT * FROM r WHERE rnk BETWEEN {rank_lo} AND {rank_hi} AND skin < {skin_max}
        ORDER BY rnk
    """).fetchdf()


def fetch_top_codes_batch(con_r2, npis: list[str], year: int,
                          top_n: int = DEFAULT_TOP_N_CODES) -> dict[str, list[dict]]:
    """Dominant HCPCS codes (by est. payment) for up to len(npis) NPIs in
    ONE R2 query, grouped + top-N'd in Python. This is the T3c throughput
    fix — the original one-off ran this as one query PER candidate NPI
    (real network round-trip latency each time); a batch of <=500 collapses
    that to one round-trip per batch. Returns {npi: [{code, pay}, ...]}."""
    valid = [n for n in npis if _NPI_RE.match(n)]
    if not valid:
        return {}
    placeholders = ",".join(f"'{n}'" for n in valid)
    rows = con_r2.execute(f"""
        SELECT CAST(Rndrng_NPI AS VARCHAR) npi, HCPCS_Cd,
               ROUND(SUM(Tot_Srvcs * Avg_Mdcr_Pymt_Amt)) pay
        FROM read_parquet('s3://needlepoint-data/processed/v1/cms_partb_svc_{year}.parquet')
        WHERE CAST(Rndrng_NPI AS VARCHAR) IN ({placeholders})
        GROUP BY 1, 2
    """).fetchall()
    by_npi: dict[str, list[tuple]] = {}
    for npi, code, pay in rows:
        by_npi.setdefault(npi, []).append((code, pay))
    return {
        npi: [{"code": c, "pay": p} for c, p in sorted(vals, key=lambda x: -x[1])[:top_n]]
        for npi, vals in by_npi.items()
    }


def run_anchor_check(hcpcs: str, *, python_bin: str = sys.executable,
                     timeout: int = ANCHOR_TIMEOUT_SEC) -> dict:
    """Falsity-anchor existence check on one code (subprocess — same
    mechanism as the original one-off; the funnel step being productionized
    here is the top-codes batching above, not this call)."""
    proc = subprocess.run(
        [python_bin, str(REPO_ROOT / "scripts" / "anchor_check.py"),
         "--hcpcs", hcpcs, "--format", "json"],
        capture_output=True, text=True, timeout=timeout,
    )
    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": (proc.stdout or proc.stderr)[:200]}


def process_candidate(cand: dict, top_codes: list[dict], ring_cache_dir: Path, *,
                      enrich_fn=enrich_ring, anchor_fn=run_anchor_check) -> dict:
    """One candidate's full record: dominant codes (already batch-fetched) +
    anchor check on the top code + ring/novelty enrichment (per-candidate,
    unchanged). Never raises — an error is captured in the record so one bad
    candidate can't kill the sweep."""
    rec: dict = {"rank": cand["rank"], "npi": cand["npi"], "name": cand["name"],
                "sp": cand["sp"], "st": cand["st"], "top_codes": top_codes}
    try:
        if top_codes:
            rec["anchor"] = anchor_fn(top_codes[0]["code"])
        r = enrich_fn(cand["npi"])
        (ring_cache_dir / f"{cand['npi']}.json").write_text(json.dumps(r, default=str))
        ent = r.get("collectible_entity") or {}
        nv = (r.get("novelty") or {}).get("verdict", "")
        rec.update({
            "entity": ent.get("name"), "kind": ent.get("kind"),
            "ring_flag": r.get("ring_flag"), "n_anom": r.get("n_anomalous"),
            "novelty": "GOV-KNOWN" if "GOV-KNOWN" in nv else "NOVEL",
        })
    except Exception as e:
        rec["_error"] = str(e)[:150]
    return rec


def _progress_line(rec: dict) -> str:
    anchor = rec.get("anchor") or {}
    has_anchor = bool(anchor.get("lcd_exists") or anchor.get("ncd_exists"))
    return (f"#{rec['rank']:<4}{rec['npi']} {str(rec.get('name'))[:22]:22} "
            f"ent={str(rec.get('entity'))[:22]:22} ring={rec.get('ring_flag')} "
            f"{rec.get('novelty', 'ERR')} anchor={has_anchor}")


def run_sweep(*, root: Path, year: int, rank_lo: int, rank_hi: int,
             skip_npis: set[str], output_path: Path, ring_cache_dir: Path,
             skin_max: float = DEFAULT_SKIN_MAX, batch_size: int = DEFAULT_BATCH_SIZE,
             con_local=None, con_r2=None, enrich_fn=enrich_ring,
             anchor_fn=run_anchor_check, print_progress=True) -> dict:
    """Full funnel: select -> batch top-codes -> per-candidate anchor+ring ->
    append JSONL. Returns {n_candidates, n_skipped, n_written, output_path}."""
    import duckdb
    con_local = con_local or duckdb.connect()
    con_r2 = con_r2 or r2.duckdb_r2()

    panel_path = find_panel(root, year)
    cands_df = select_candidates(con_local, panel_path, rank_lo, rank_hi, skin_max)
    cands = cands_df.rename(columns={"nm": "name", "rnk": "rank"}).to_dict("records")
    todo = [c for c in cands if c["npi"] not in skip_npis]

    ring_cache_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    n_written = 0
    with output_path.open("a") as out:
        for start in range(0, len(todo), batch_size):
            batch = todo[start:start + batch_size]
            top_codes_by_npi = fetch_top_codes_batch(
                con_r2, [c["npi"] for c in batch], year)
            for cand in batch:
                rec = process_candidate(
                    cand, top_codes_by_npi.get(cand["npi"], []), ring_cache_dir,
                    enrich_fn=enrich_fn, anchor_fn=anchor_fn)
                out.write(json.dumps(rec) + "\n")
                out.flush()
                n_written += 1
                if print_progress:
                    print(_progress_line(rec), flush=True)

    return {
        "n_candidates": len(cands), "n_skipped": len(cands) - len(todo),
        "n_written": n_written, "output_path": str(output_path),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ranks", required=True, metavar="A-B",
                    help="Panel rank range to sweep (by score_rrf desc), e.g. 16-120.")
    ap.add_argument("--skip-npis-file", default=None,
                    help="Text file, one NPI per line ('#' comments ok) — skip these "
                         "(replaces the old hardcoded DONE set).")
    ap.add_argument("--year", type=int, default=DEFAULT_YEAR,
                    help=f"Scored-panel + by-service PUF year (default {DEFAULT_YEAR}).")
    ap.add_argument("--skin-max", type=float, default=DEFAULT_SKIN_MAX,
                    help="Exclude candidates with cl_skin_sub_pay_share >= this (default 0.3).")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                    help="Max NPIs per R2 top-codes IN-list query (default 500).")
    ap.add_argument("--output", default=None,
                    help="Output JSONL path (default: state/target_sweep_<today>.jsonl).")
    ap.add_argument("--ring-cache-dir", default=None,
                    help="Ring-enrichment JSON cache dir (default: state/ring_cache).")
    ap.add_argument("--root", default=str(REPO_ROOT), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    root = Path(args.root)
    try:
        rank_lo, rank_hi = parse_rank_range(args.ranks)
        skip_npis = load_skip_npis(args.skip_npis_file)
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    output_path = Path(args.output) if args.output else (
        root / "state" / f"target_sweep_{datetime.now():%Y-%m-%d}.jsonl")
    ring_cache_dir = Path(args.ring_cache_dir) if args.ring_cache_dir else (root / "state" / "ring_cache")

    try:
        summary = run_sweep(
            root=root, year=args.year, rank_lo=rank_lo, rank_hi=rank_hi,
            skip_npis=skip_npis, output_path=output_path, ring_cache_dir=ring_cache_dir,
            skin_max=args.skin_max, batch_size=args.batch_size,
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"SWEEP DONE — {summary['n_written']} written, {summary['n_skipped']} skipped "
          f"(of {summary['n_candidates']} candidates) -> {summary['output_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
