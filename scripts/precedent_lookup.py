#!/usr/bin/env python3
"""FCA doctrine / precedent lookup for the LEGAL-reasoning agents (counsel_build,
counsel_rebuttal, counsel_disposition, legal_adv_response, defense, first_chair).

Reads the curated FCA-doctrine corpus **from R2** (``r2:needlepoint-data/processed/v1/
doctrine_corpus.jsonl``), falling back to the local mirror. Given a defense mode
and the case's venue/circuit, returns the CONTROLLING precedent + pleading guidance
— so counsel pleads to the venue's standard (e.g. 7th-Cir. Rule 9(b) -> Lusby
relaxed reliable-indicia) WITHOUT the case name being hard-coded in the prompt.
With no filters it returns the full doctrine corpus.

Investigator agents do NOT use this — they find/verify EVIDENCE; counsel builds the
case behind precedent. The corpus is a *pointer*; verify any cite via
``courtlistener_search.py verify`` before relying on it in a filing.

Usage:
  python scripts/precedent_lookup.py --defense-mode rule_9b_insufficient --circuit 7th
  python scripts/precedent_lookup.py --doctrine scienter
  python scripts/precedent_lookup.py                 # full doctrine corpus
  python scripts/precedent_lookup.py --list-modes
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORPUS = REPO / "data" / "ground_truth" / "doctrine_corpus.jsonl"
R2_KEY = "r2:needlepoint-data/processed/v1/doctrine_corpus.jsonl"


def _load() -> list[dict]:
    """Prefer the canonical bucket copy (private bucket or public mirror); fall back to the
    local mirror."""
    import sys
    sys.path.insert(0, str(REPO))
    text = ""
    try:
        from fraudlib import r2
        text = r2.fetch_local(CORPUS, R2_KEY).read_text()
    except Exception:
        text = ""
    if not text.strip() and CORPUS.exists():
        text = CORPUS.read_text()
    if not text.strip():
        raise SystemExit(
            f"doctrine corpus unavailable from R2 ({R2_KEY}) and no local copy at {CORPUS}.\n"
            f"  set NP_DATA_URL to the public mirror, or: rclone copy {R2_KEY} data/ground_truth/"
        )
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _circuit_match(entry_circuit: str | None, want: str | None) -> bool:
    """A circuit query returns that circuit's entries PLUS SCOTUS / all-circuit
    doctrines (which apply everywhere)."""
    if not want:
        return True
    ec = (entry_circuit or "").lower()
    if "scotus" in ec or ec == "all" or "all" in ec:
        return True
    return want.lower() in ec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--defense-mode", help="defense agent attack type, e.g. rule_9b_insufficient")
    ap.add_argument("--doctrine", help="legal doctrine, e.g. scienter, materiality")
    ap.add_argument("--circuit", help="e.g. 7th, 5th, SCOTUS — returns circuit-specific + SCOTUS/all")
    ap.add_argument("--keyword", help="free-text match across the entry")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--list-modes", action="store_true", help="list the defense_mode values covered")
    args = ap.parse_args(argv)

    rows = _load()

    if args.list_modes:
        for m in sorted({r.get("defense_mode", "?") for r in rows}):
            print(m)
        return 0

    def keep(r: dict) -> bool:
        if args.defense_mode and r.get("defense_mode") != args.defense_mode:
            return False
        if args.doctrine and args.doctrine.lower() not in r.get("doctrine", "").lower():
            return False
        if not _circuit_match(r.get("circuit"), args.circuit):
            return False
        if args.keyword and args.keyword.lower() not in json.dumps(r).lower():
            return False
        return True

    hits = [r for r in rows if keep(r)]

    if args.format == "json":
        print(json.dumps(hits, indent=2))
        return 0

    if not hits:
        print("(no matching doctrine entries — try --list-modes or a broader --keyword)")
        return 0

    for r in hits:
        print(f"\n### {r['controlling_case']} — {r['citation']}")
        print(f"  doctrine={r['doctrine']} | defense_mode={r['defense_mode']} | "
              f"circuit={r['circuit']} | standard={r['standard']} | status={r['verification_status']}")
        print(f"  HOLDING: {r['holding']}")
        print(f"  PLEAD:   {r['pleading_guidance']}")
    print(f"\n[{len(hits)} entr{'y' if len(hits) == 1 else 'ies'}] — "
          f"VERIFY each cite via `courtlistener_search.py verify` before filing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
