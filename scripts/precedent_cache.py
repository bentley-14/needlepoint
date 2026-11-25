#!/usr/bin/env python3
"""Verified-citation cache — avoid re-verifying the same precedent across
cases/iterations.

Every count needs its precedent citation verified before it can be relied on
in a filing (see investigator.md's "Precedent discipline" / courtlistener_search.py
verify). That verification is per-CITATION, not per-case — the same controlling
case (e.g. *United States v. Mackby*, 261 F.3d 821 (9th Cir. 2001)) gets cited
across many unrelated cases, and without a cache each case/iteration re-runs the
same CourtListener lookup. This is a plain get/put cache over a JSONL log:

  get   --citation "..."                       -> most recent cached record, or a
                                                    clean MISS (never raises)
  put   --citation "..." --verified true|false
        [--docket D] [--source-url U] [--quote-file F] [--reviewer NAME]
                                                -> append a new record

Storage: data/cache/precedent_verifications.jsonl (one JSON record per line,
append-only — a re-verification just appends a newer record; `get` returns the
LAST match). Long quotes are NOT inlined into the JSONL (keeps it small/greppable)
— they're written as a blob under data/cache/policy_texts/<slug>.txt and the
record carries a `quote_path` pointer.

Matching is case/whitespace-insensitive on the citation string (mirrors
CaseState.verified_precedents' de-dup rule in schemas.py).

Usage:
    python scripts/precedent_cache.py get --citation "United States v. Mackby, 261 F.3d 821 (9th Cir. 2001)"
    python scripts/precedent_cache.py put --citation "United States v. Mackby, 261 F.3d 821 (9th Cir. 2001)" \
        --verified true --docket "97-15931" --source-url "https://www.courtlistener.com/opinion/..." \
        --quote-file /tmp/mackby_quote.txt
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CACHE_ROOT = REPO / "data" / "cache"
JSONL_PATH_DEFAULT = CACHE_ROOT / "precedent_verifications.jsonl"
BLOBS_DIR_DEFAULT = CACHE_ROOT / "policy_texts"


def _norm(citation: str) -> str:
    """Case/whitespace-insensitive match key."""
    return " ".join((citation or "").strip().lower().split())


def _slug(citation: str) -> str:
    """Filesystem-safe stem for a citation's quote blob."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (citation or "").strip().lower()).strip("_")
    return (s[:80] or "citation")


def _parse_bool(s: str) -> bool:
    return s.strip().lower() in ("1", "true", "yes", "y")


def _read_records(jsonl_path: Path) -> list[dict]:
    if not jsonl_path.exists():
        return []
    out = []
    for line in jsonl_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def get_verification(citation: str, *, jsonl_path: Path = JSONL_PATH_DEFAULT) -> dict | None:
    """Most recent cached record for ``citation`` (case/whitespace-insensitive),
    or None on a clean miss — never raises."""
    target = _norm(citation)
    hit = None
    for rec in _read_records(jsonl_path):
        if _norm(rec.get("citation", "")) == target:
            hit = rec   # last match wins — a re-verification supersedes the prior one
    return hit


def put_verification(
    citation: str,
    *,
    verified: bool,
    docket: str | None = None,
    source_url: str | None = None,
    quote: str | None = None,
    reviewer: str | None = None,
    jsonl_path: Path = JSONL_PATH_DEFAULT,
    blobs_dir: Path = BLOBS_DIR_DEFAULT,
) -> dict:
    """Append a verification record. A ``quote`` is stored as a blob under
    ``blobs_dir`` (the JSONL keeps only a ``quote_path`` pointer)."""
    rec: dict = {
        "citation": citation.strip(),
        "verified": bool(verified),
        "docket": docket,
        "source_url": source_url,
        "reviewer": reviewer,
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if quote:
        blobs_dir.mkdir(parents=True, exist_ok=True)
        blob_path = blobs_dir / f"{_slug(citation)}.txt"
        blob_path.write_text(quote)
        try:
            rec["quote_path"] = str(blob_path.relative_to(REPO))
        except ValueError:
            rec["quote_path"] = str(blob_path)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def _cmd_get(args) -> int:
    rec = get_verification(args.citation, jsonl_path=Path(args.jsonl_path))
    if rec is None:
        if args.format == "json":
            print(json.dumps({"hit": False, "citation": args.citation}))
        else:
            print(f"MISS: no cached verification for {args.citation!r} — "
                  f"verify via courtlistener_search.py, then `put` the result.")
        return 1
    if args.format == "json":
        print(json.dumps({"hit": True, **rec}, indent=2))
    else:
        print(f"HIT: {rec['citation']}")
        print(f"  verified:    {rec.get('verified')}")
        print(f"  docket:      {rec.get('docket') or '—'}")
        print(f"  source_url:  {rec.get('source_url') or '—'}")
        print(f"  verified_at: {rec.get('verified_at')}")
        if rec.get("quote_path"):
            print(f"  quote_path:  {rec['quote_path']}")
        if rec.get("reviewer"):
            print(f"  reviewer:    {rec['reviewer']}")
    return 0


def _cmd_put(args) -> int:
    quote = None
    if args.quote_file:
        quote = Path(args.quote_file).read_text()
    rec = put_verification(
        args.citation,
        verified=args.verified,
        docket=args.docket,
        source_url=args.source_url,
        quote=quote,
        reviewer=args.reviewer,
        jsonl_path=Path(args.jsonl_path),
    )
    if args.format == "json":
        print(json.dumps(rec, indent=2))
    else:
        print(f"cached: {rec['citation']} (verified={rec['verified']})"
              + (f" — quote blob: {rec['quote_path']}" if rec.get("quote_path") else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jsonl-path", default=str(JSONL_PATH_DEFAULT),
                    help="Cache file (default: data/cache/precedent_verifications.jsonl)")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get", help="Look up a citation's cached verification.")
    g.add_argument("--citation", required=True)
    g.set_defaults(fn=_cmd_get)

    p = sub.add_parser("put", help="Record a citation's verification result.")
    p.add_argument("--citation", required=True)
    p.add_argument("--verified", required=True, type=_parse_bool, metavar="true|false")
    p.add_argument("--docket", default=None)
    p.add_argument("--source-url", default=None)
    p.add_argument("--quote-file", default=None,
                   help="Path to a text file with the supporting quote (stored as a blob, "
                        "not inlined into the JSONL).")
    p.add_argument("--reviewer", default=None)
    p.set_defaults(fn=_cmd_put)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
