#!/usr/bin/env python3
"""Falsity-anchor existence gate (failure_mode_taxonomy_2026-07.md #2).

Answers, for one HCPCS code (+ optional MAC/state), the single question that
corpus-wide predicts whether a case can FILE: does a coverage rule (LCD/NCD) or
a per-se statutory/regulatory hook exist for this code at all? Every filed count
in the 26-case sweep was anchored on a public coverage/enrollment rule; the
worst-case failure (CASE-6A4588BF, Sohal/Eye Clinic Chestnut) burned 4 investigator
iterations + 3 adversarial rounds + 1 human checkpoint to learn "no 65778
LCD/NCD exists" — a fact this script answers in one call at intake.

Built on scripts/lcd_lookup.py's parquet source (no new data dependency).

SURFACES ONLY — this does not gate verdicts. It labels the Step-0 evidence
pack's "FALSITY-ANCHOR AVAILABILITY" section so counsel sees the anchor
landscape at iteration 1, before investing in a build.

Usage:
    python scripts/anchor_check.py --hcpcs 65778
    python scripts/anchor_check.py --hcpcs 87798 --mac J6
    python scripts/anchor_check.py --hcpcs 65778 --format json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import pandas as pd

import lcd_lookup  # noqa: E402

# Per-se statutory/regulatory hooks that don't require an LCD/NCD to exist —
# a code can still be a falsity anchor via one of these even with no coverage
# determination on file (mirrors floors.py's falsity-anchor definition).
_PER_SE_HOOKS = [
    "PECOS enrollment mismatch (rendering state != enrolled state)",
    "scope-of-practice / licensure incompatibility with the billing specialty",
    "supervision-requirement violation (incident-to / PA-NP oversight)",
    "manual non-coverage (e.g. MBPM ch.15, FDA enforcement-discretion end)",
    "CLIA-certification absence for a billed lab code",
]


def check_anchor(hcpcs: str, mac: str | None = None, effective_on: str | None = None) -> dict:
    """Return {hcpcs, mac, lcd_exists, ncd_exists, article_only, per_se_candidates}.

    ``effective_on`` (YYYY-MM-DD) restricts to determinations in force on that
    date — required for calibration/point-in-time safety: whether a coverage
    rule "exists" for a code can itself be a post-cutoff fact (a new LCD can be
    published after the calibration as-of date), so a calibration caller MUST
    pass its cutoff date here rather than querying "current" scope.
    """
    # "all" scope (not "current") when point-in-time filtering, so a
    # since-retired-but-then-in-force determination is not missed.
    scope = "all" if effective_on else "current"
    lcd_df = lcd_lookup.filter_by_hcpcs(lcd_lookup._load("lcd", scope), hcpcs)
    if mac:
        lcd_df = lcd_lookup.filter_by_mac(lcd_df, mac)
    ncd_df = lcd_lookup.filter_by_hcpcs(lcd_lookup._load("ncd", "all"), hcpcs)
    article_df = lcd_lookup.filter_by_hcpcs(lcd_lookup._load("article", scope), hcpcs)
    if mac:
        article_df = lcd_lookup.filter_by_mac(article_df, mac)
    if effective_on:
        when = pd.to_datetime(effective_on)
        lcd_df = lcd_lookup.filter_effective_on(lcd_df, "lcd", when)
        ncd_df = lcd_lookup.filter_effective_on(ncd_df, "ncd", when)
        article_df = lcd_lookup.filter_effective_on(article_df, "article", when)

    lcd_exists = not lcd_df.empty
    ncd_exists = not ncd_df.empty
    article_only = (not lcd_exists) and (not ncd_exists) and (not article_df.empty)

    lcds = lcd_lookup._summarize(lcd_df, "lcd") if lcd_exists else []
    ncds = lcd_lookup._summarize(ncd_df, "ncd") if ncd_exists else []
    articles = lcd_lookup._summarize(article_df, "article") if not article_df.empty else []

    return {
        "hcpcs": hcpcs.strip().upper(),
        "mac": mac,
        "effective_on": effective_on,
        "lcd_exists": lcd_exists,
        "ncd_exists": ncd_exists,
        "article_only": article_only,
        "lcds": [{"id": r["id"], "title": r["title"]} for r in lcds],
        "ncds": [{"id": r["id"], "title": r["title"]} for r in ncds],
        "articles": [{"id": r["id"], "title": r["title"]} for r in articles],
        "per_se_candidates": _PER_SE_HOOKS,
    }


def render_text(result: dict) -> str:
    lines = [f"Falsity-anchor availability — HCPCS {result['hcpcs']}"
             + (f" (MAC {result['mac']})" if result["mac"] else "")]
    lines.append(f"  lcd_exists:    {result['lcd_exists']}"
                 + (f" ({len(result['lcds'])}: "
                    + "; ".join(f"{r['id']} {r['title']}" for r in result["lcds"]) + ")"
                    if result["lcds"] else ""))
    lines.append(f"  ncd_exists:    {result['ncd_exists']}"
                 + (f" ({len(result['ncds'])}: "
                    + "; ".join(f"{r['id']} {r['title']}" for r in result["ncds"]) + ")"
                    if result["ncds"] else ""))
    lines.append(f"  article_only:  {result['article_only']}"
                 + (f" ({len(result['articles'])}: "
                    + "; ".join(f"{r['id']} {r['title']}" for r in result["articles"]) + ")"
                    if result["articles"] else ""))
    if not (result["lcd_exists"] or result["ncd_exists"] or result["articles"]):
        lines.append("  NO LCD/NCD/article found for this code — falsity must rest on a "
                      "per-se statutory/regulatory hook (see per_se_candidates) or "
                      "confirmed-fact evidence, not a coverage-criteria violation.")
    lines.append("  per_se_candidates (always-available fallback anchors, not verified "
                  "against the target — the investigator must confirm which apply):")
    for c in result["per_se_candidates"]:
        lines.append(f"    - {c}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hcpcs", required=True, help="HCPCS/CPT code to check.")
    ap.add_argument("--mac", help="MAC jurisdiction filter (e.g. J3, J5, J6).")
    ap.add_argument("--effective-on", help="YYYY-MM-DD point-in-time filter "
                     "(calibration/point-in-time safety — see check_anchor docstring).")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args()

    result = check_anchor(args.hcpcs, args.mac, args.effective_on)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print(render_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
