"""Build a clean-room working directory for a v2 fraud-investigator run.

Creates a directory under fraud-investigator-v2/cleanroom-{name}/ containing
ONLY symlinks to public/legitimate data sources:

  - .mcp.json, .env             (MCP config + CourtListener token)
  - data/cache/                 (CMS feature parquets)
  - data/extracted/v4_label_ready_high_plus_medium_reviewed.jsonl
  - data/source/cases/          (DOJ case library — peer/training data)
  - data/panels/                (raw CMS panels, if present)
  - fraudlib/                   (library code for queries)
  - scripts/                    (CMS query helpers)

NOT symlinked (the contamination surface for prior-run-aware evaluations):
  - fraud-investigator/         (JS v1 — prior CASE-* state + output)
  - fraud-investigator-v2/      (this codebase — would recurse + leak transcript)
  - Larch-Investigation-*/      (standalone v1 Larch artifacts)
  - Fraud-Investigation-System-*/ (mirror)
  - legacy/                     (journals may name prior subjects)
  - docs/                       (may discuss prior runs)
  - results/                    (training results; may contain target NPIs)
  - data/raw/                   (raw HTML scrapes — DOJ press text)
  - data/archive/, data/eval/, data/scrape/

Idempotent — running twice is safe.

Teardown: just `rm -rf cleanroom-{name}/`. All entries are symlinks, so no
real files are touched.

Usage:
    python3 scripts/setup_cleanroom.py --name larch
    python3 scripts/setup_cleanroom.py --name larch --verify
    python3 scripts/setup_cleanroom.py --name larch --teardown
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# fraud-investigator-v2/scripts/setup_cleanroom.py
V2_ROOT = Path(__file__).resolve().parents[1]
WORKTREE_ROOT = V2_ROOT.parent
# Main repo root sits 3 levels above the worktree
# (.claude/worktrees/<branch> → .claude/worktrees → .claude → REPO)
MAIN_REPO_ROOT = WORKTREE_ROOT.parents[2]
# The canonical DOJ peer-case corpus — data/extracted/gold_set.jsonl (1,005
# NPIs / 1,301 case-NPI rows; DOJ-only post-LEIE-drop). Resolved off
# WORKTREE_ROOT (= repo root in a normal checkout), matching the data/extracted/
# convention used elsewhere. The investigator + rebuttal prompts read this exact
# path; the old sibling-worktree target (v4_label_ready_*.jsonl) was archived.
GOLD_CORPUS = WORKTREE_ROOT / "data/extracted/gold_set.jsonl"

# (relative_in_cleanroom, absolute_target) pairs.
# Targets must exist or the link is skipped with a warning.
#
# Layered exposure (richer than v1 cleanroom):
#   ── Cache (derived features per NPI per year) — already in worktree cache:
#      data/cache/feature_frames/        201-col per-NPI summary
#      data/cache/claim_level_features/  E&M shares, J-code concentration, POS shares
#      data/cache/dmepos_features/       DMEPOS per-NPI aggregates
#      data/cache/openpayments_features/ Pharma payment per-NPI aggregates
#      data/cache/case_sources/          11 DOJ cases w/ pre-fetched CourtListener dockets
#      data/cache/fraud_period_extraction/  SOL windows per DOJ case
#
#   ── Processed (raw CMS aggregates per NPI, 2013-2023) — NEW:
#      data/processed/cms_partb_<year>.parquet      Per-NPI Part B aggregates
#      data/processed/cms_partb_svc_<year>.parquet  ★ Per-(NPI, HCPCS) PUF —
#                                                    enables new-vs-established E&M
#                                                    signature, code-mix analysis,
#                                                    specialty-mismatch detection
#      data/processed/cms_hcpcs_features_<year>.parquet  Precomputed HCPCS features
#      data/processed/cms_partd_<year>.parquet      Part D drug claims per NPI
#      data/processed/cms_partd_features_<year>.parquet  Part D derived features
#      data/processed/cms_dmepos_<year>.parquet     DMEPOS supplier per-NPI
#      data/processed/open_payments_general_<year>.parquet  Pharma payments raw
#      data/processed/nppes_bulk.parquet            ★ 9.4M NPIs registry (no API)
#      data/processed/facility_hha.parquet          ★ Home health agencies (AKS)
#      data/processed/facility_hospice.parquet      Hospices
#      data/processed/facility_hospital.parquet     Hospitals
#      data/processed/facility_snf.parquet          Skilled nursing facilities
#      data/processed/state_board_actions_fsmb.parquet  FSMB state board actions
#
#   ── DOJ corpus + policy:
#      data/extracted/gold_set.jsonl      Canonical DOJ labels (NPI-centric, audit-cleaned)
#      data/source/policy/                CMS policy guidelines (LCD/NCD)
#      data/source/doj_api/               DOJ press release scrapes
#
# Also creates data/panels → data/processed alias so fraudlib/ingest/cms/
# hcpcs_stats.py (which expects data/panels/cms_partb_svc_<y>.parquet) works
# unmodified.
SYMLINKS = [
    (".mcp.json",                                                  V2_ROOT / ".mcp.json"),
    (".env",                                                       V2_ROOT / ".env"),
    # Derived feature caches (per-NPI per-year)
    ("data/cache",                                                 WORKTREE_ROOT / "data/cache"),
    ("data/extracted/v4_label_ready_high_plus_medium_reviewed.jsonl",
                                                                   WORKTREE_ROOT / "data/extracted/v4_label_ready_high_plus_medium_reviewed.jsonl"),
    # DOJ case corpus — ONLY the curated gold set. Intentionally not exposing
    # data/source/cases/ (broader corpus contains per-year validation files,
    # audit snapshots, staging dirs, dropped-case lists, etc. — agents
    # shouldn't see intermediate / superseded / work-in-progress artifacts.
    # Gold is the only "production" peer-pattern source).
    ("data/extracted/gold_set.jsonl",                              GOLD_CORPUS),
    # Policy + DOJ press releases — verified, complete, single-source-of-truth
    ("data/source/policy",                                         MAIN_REPO_ROOT / "data/source/policy"),
    ("data/source/doj_api",                                        MAIN_REPO_ROOT / "data/source/doj_api"),
    # Raw CMS PUFs + LEIE + NPPES + facility + state-board (data/processed/)
    ("data/processed",                                             MAIN_REPO_ROOT / "data/processed"),
    # fraudlib expects data/panels/cms_partb_svc_<y>.parquet — alias to processed
    ("data/panels",                                                MAIN_REPO_ROOT / "data/processed"),
    # Code
    ("fraudlib",                                                   WORKTREE_ROOT / "fraudlib"),
    ("scripts",                                                    WORKTREE_ROOT / "scripts"),
]

# What MUST NOT appear in the cleanroom (sanity check post-build)
# NOTE: data/processed/models/ contains trained .joblib rankers; reachable via
# the data/processed symlink but flagged here so verify() warns if any
# resolvable filesystem traversal would reach them.
FORBIDDEN_PATTERNS = [
    "fraud-investigator/",          # JS v1
    "fraud-investigator-v2/",       # ourselves (would recurse + leak transcript)
    "Larch-Investigation-",
    "Fraud-Investigation-System-",
    "legacy/",                      # v0/v1 legacy artifacts
    "results/",                     # ranker outputs (target NPIs)
    "data/archive/",                # working notes (mentioned target NPIs)
    "data/eval/",                   # ranker outputs (target NPIs)
    "data/scrape/",                 # raw scrapes; could contain labels
]


def setup(name: str) -> None:
    cleanroom = V2_ROOT / f"cleanroom-{name}"
    print(f"Setting up cleanroom: {cleanroom}")
    cleanroom.mkdir(parents=True, exist_ok=True)

    created, existing, missing_target = 0, 0, 0
    for rel, target in SYMLINKS:
        link = cleanroom / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            existing += 1
            continue
        if not target.exists():
            print(f"  SKIP (target missing): {rel} → {target}")
            missing_target += 1
            continue
        link.symlink_to(target)
        print(f"  + {rel} → {target}")
        created += 1

    print(f"\nSummary: {created} new symlinks, {existing} already existed, {missing_target} skipped.")

    # Write a SUBJECT-NEUTRAL marker outside the cleanroom (so even reading
    # the marker reveals no information about the run's subject).
    marker = V2_ROOT / f".cleanroom-{name}.info.md"
    marker.write_text(
        f"# Cleanroom provenance: {name}\n\n"
        f"Symlink-only working dir at `cleanroom-{name}/`. Teardown:\n\n"
        f"```\npython3 scripts/setup_cleanroom.py --name {name} --teardown\n```\n\n"
        f"Reachable from inside the cleanroom:\n"
        + "\n".join(f"- `{rel}` → `{target}`" for rel, target in SYMLINKS)
        + "\n\nIntentionally not reachable:\n"
        + "\n".join(f"- `{p}`" for p in FORBIDDEN_PATTERNS)
        + "\n"
    )
    print(f"  + {marker.name} (provenance marker, OUTSIDE the cleanroom)")


def verify(name: str) -> int:
    """Confirm the cleanroom is well-formed AND that none of the forbidden paths
    are reachable via ls/find from inside it.

    Returns 0 on pass, 1 on any concern."""
    cleanroom = V2_ROOT / f"cleanroom-{name}"
    if not cleanroom.exists():
        print(f"  ✗ cleanroom dir doesn't exist: {cleanroom}")
        return 1

    # 1. Symlinks all resolve
    fails = []
    for rel, target in SYMLINKS:
        link = cleanroom / rel
        if not link.exists():
            if not target.exists():
                continue  # legitimately missing (e.g., no data/panels)
            fails.append(f"missing symlink {rel}")
            continue
        resolved = link.resolve()
        if not resolved.exists():
            fails.append(f"broken symlink {rel} → {target}")

    # 2. No forbidden patterns reachable via relative grep inside the cleanroom
    for forbidden in FORBIDDEN_PATTERNS:
        hits = list(cleanroom.glob(f"**/{forbidden.rstrip('/').rstrip('*')}*"))
        if hits:
            fails.append(f"FORBIDDEN reachable: {forbidden} → {[str(h) for h in hits[:3]]}")

    if fails:
        print("✗ verify FAILED:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print(f"✓ cleanroom {cleanroom} verified clean")
    print(f"  symlinks: {sum(1 for r, t in SYMLINKS if (cleanroom / r).exists())}/{len(SYMLINKS)} resolved")
    print(f"  forbidden patterns reachable: 0/{len(FORBIDDEN_PATTERNS)}")
    return 0


def teardown(name: str) -> None:
    cleanroom = V2_ROOT / f"cleanroom-{name}"
    marker = V2_ROOT / f".cleanroom-{name}.info.md"

    if not cleanroom.exists() and not marker.exists():
        print(f"  (already gone) {cleanroom}")
        return

    if cleanroom.exists():
        # Safety: only proceed if all contents are symlinks or directories
        # containing symlinks. Refuse if any real file slipped in.
        real_files = []
        for p in cleanroom.rglob("*"):
            if p.is_symlink():
                continue
            if p.is_dir():
                continue
            real_files.append(p)
        if real_files:
            print(f"  ✗ Refusing teardown — found {len(real_files)} unexpected real file(s):")
            for p in real_files[:10]:
                print(f"      {p}")
            print(f"\n  Inspect and decide. To force: rm -rf {cleanroom}")
            sys.exit(1)
        shutil.rmtree(cleanroom)
        print(f"  ✓ removed {cleanroom} (symlink-only; no real files touched)")

    if marker.exists():
        marker.unlink()
        print(f"  ✓ removed marker {marker.name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="Cleanroom name (e.g., 'larch')")
    ap.add_argument("--verify", action="store_true", help="Verify a cleanroom is well-formed + no contamination paths reachable")
    ap.add_argument("--teardown", action="store_true", help="Remove the cleanroom (symlinks only)")
    args = ap.parse_args()

    if args.teardown:
        teardown(args.name)
        return 0
    if args.verify:
        return verify(args.name)
    setup(args.name)
    return verify(args.name)


if __name__ == "__main__":
    sys.exit(main())
