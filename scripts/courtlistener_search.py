"""CourtListener REST API wrapper with token auth.

Wraps the CourtListener REST API v3 to provide a clean CLI for the agent.
Without this script the agent has to construct Authorization headers per
call, which it sometimes skips and gets rate-limited / partial results.

CourtListener resources:
  - opinions   — court opinions with full text (the "verify a precedent" use)
  - dockets    — court dockets (the "is there a prior qui tam vs this defendant" use)
  - recap-docs — RECAP archive of PACER docs (sealed/unsealed federal filings)
  - clusters   — opinion clusters (groups of related opinions in one case)

Token lives in environment as COURTLISTENER_TOKEN (loaded from .env).

Usage:
    # Verify a citation
    python scripts/courtlistener_search.py verify "Mackby" --reporter "261 F.3d 821"

    # Search opinions
    python scripts/courtlistener_search.py opinions "incident-to physical therapy fraud"

    # Search RECAP dockets (for qui tam / FCA against a defendant)
    python scripts/courtlistener_search.py dockets "Larch" --court cacd

    # Search by party name
    python scripts/courtlistener_search.py dockets-party "Provider Larch"

    # Fetch a single opinion's full text
    python scripts/courtlistener_search.py opinion-text 152575

    # Free-text RECAP search (legal docs, e.g., sealed qui tam)
    python scripts/courtlistener_search.py recap "qui tam NPI 1900000085"
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

import requests

# Load .env from V2 root or worktree root
def _load_dotenv() -> None:
    for p in [
        Path(__file__).resolve().parents[1] / "fraud-investigator-v2" / ".env",
        Path(__file__).resolve().parents[1] / ".env",
    ]:
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
            return

_load_dotenv()

BASE_URL = "https://www.courtlistener.com/api/rest/v4"


def _resolve_token(env: dict | None = None) -> str:
    """Token from env. Accept either name so a teammate who set the older
    COURTLISTENER_API_TOKEN (used by some legacy scrape scripts) still works
    without editing their .env. Pure function so tests can inject the env."""
    if env is None:
        env = os.environ
    return (env.get("COURTLISTENER_TOKEN")
            or env.get("COURTLISTENER_API_TOKEN") or "").strip()


TOKEN = _resolve_token()
DEFAULT_TIMEOUT_SEC = 45

# Backoff delays (seconds) for retry attempts: attempt 1, 2, 3
_RETRY_DELAYS = [1, 3, 6]
_RETRY_MAX = 3
_RETRYABLE_HTTP = {429, 502, 503, 504}

# Mutable so tests can override
_timeout_sec = DEFAULT_TIMEOUT_SEC


def _headers() -> dict[str, str]:
    h = {"User-Agent": "fraud-investigator/1.0", "Accept": "application/json"}
    if TOKEN:
        h["Authorization"] = f"Token {TOKEN}"
    return h


def _get(endpoint: str, params: dict[str, Any] | None = None) -> dict:
    """GET <BASE>/<endpoint>/ with auth + params.

    Retries up to _RETRY_MAX times on Timeout, ConnectionError, and
    HTTP 429/502/503/504. On final failure emits COURTLISTENER_UNREACHABLE
    via a special ``_unreachable`` key so callers can distinguish a network
    failure from a genuine 0-results response.

    Returns JSON dict on success, or ``{"_error": ..., "_unreachable": True}``
    on unrecoverable network failure, or ``{"_error": ..., "_body": ...}`` on
    a non-retryable HTTP error.
    """
    url = f"{BASE_URL}/{endpoint.lstrip('/').rstrip('/')}/"
    last_exc: Exception | None = None
    last_status: int | None = None

    for attempt in range(1, _RETRY_MAX + 1):
        try:
            r = requests.get(url, params=params or {}, headers=_headers(),
                             timeout=_timeout_sec)
            if r.status_code in _RETRYABLE_HTTP:
                last_status = r.status_code
                if attempt < _RETRY_MAX:
                    delay = _RETRY_DELAYS[attempt - 1] + random.uniform(0, 0.5)
                    time.sleep(delay)
                    continue
                # Final attempt exhausted on retryable HTTP
                detail = f"HTTP {r.status_code}"
                msg = (f"COURTLISTENER_UNREACHABLE: {detail} after {_RETRY_MAX} attempts"
                       f" — cite via doj_pr_lookup.py or mark needs-verification")
                return {"_error": msg, "_unreachable": True}
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            # Non-retryable HTTP error (e.g. 404, 401)
            return {"_error": f"HTTP {e.response.status_code}",
                    "_body": (e.response.text or "")[:300]}
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt < _RETRY_MAX:
                delay = _RETRY_DELAYS[attempt - 1] + random.uniform(0, 0.5)
                time.sleep(delay)
        except requests.RequestException as e:
            return {"_error": str(e)}

    # All retries exhausted on Timeout / ConnectionError
    detail = str(last_exc) if last_exc else f"HTTP {last_status}"
    msg = (f"COURTLISTENER_UNREACHABLE: {detail} after {_RETRY_MAX} attempts"
           f" — cite via doj_pr_lookup.py or mark needs-verification")
    return {"_error": msg, "_unreachable": True}


# ─────────────────────────────────────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────────────────────────────────────


def _handle_error(res: dict) -> int:
    """Print error from a failed _get() response and return exit code 1.

    Prints the raw message (no extra "ERROR:" prefix) when _unreachable is set
    so the COURTLISTENER_UNREACHABLE sentinel reaches the caller cleanly.
    Otherwise prefixes with ERROR: for non-network failures.
    """
    msg = res["_error"]
    if res.get("_unreachable"):
        print(msg, file=sys.stderr)
    else:
        print(f"ERROR: {msg}", file=sys.stderr)
    return 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Look up an opinion by case-name search; print top hits."""
    params: dict[str, Any] = {"type": "o", "q": args.case_name}
    if args.reporter:
        params["q"] = f'{args.case_name} "{args.reporter}"'
    if args.court:
        params["court"] = args.court
    res = _get("search", params)
    if "_error" in res:
        return _handle_error(res)
    results = res.get("results") or []
    print(f"Verification search for '{args.case_name}'"
          + (f" + reporter '{args.reporter}'" if args.reporter else "")
          + f" — {len(results)} hits (showing top 5):\n")
    for r in results[:5]:
        case_name = r.get("caseName") or "?"
        court = r.get("court_citation_string") or r.get("court") or "?"
        date = r.get("dateFiled") or ""
        cite = r.get("citation") or []
        cite_s = "; ".join(cite) if cite else "(no canonical cite)"
        opinion_url = r.get("absolute_url") or ""
        print(f"  • {case_name} ({court}, {date})")
        print(f"      cite: {cite_s}")
        print(f"      url:  https://www.courtlistener.com{opinion_url}")
        if r.get("snippet"):
            snip = r["snippet"].replace("\n", " ")[:200]
            print(f"      snippet: ...{snip}...")
        print()
    return 0


def cmd_opinions(args: argparse.Namespace) -> int:
    """Free-text opinion search."""
    params: dict[str, Any] = {"type": "o", "q": args.query}
    if args.court: params["court"] = args.court
    if args.judge: params["judge"] = args.judge
    res = _get("search", params)
    if "_error" in res:
        return _handle_error(res)
    results = res.get("results") or []
    print(f"Opinion search '{args.query}' — {len(results)} hits (top {args.limit}):\n")
    for r in results[:args.limit]:
        case_name = r.get("caseName") or "?"
        print(f"  • {case_name}")
        print(f"      court: {r.get('court') or '?'}  filed: {r.get('dateFiled') or '?'}")
        print(f"      url:   https://www.courtlistener.com{r.get('absolute_url') or ''}")
        if r.get("snippet"):
            snip = r["snippet"].replace("\n", " ")[:200]
            print(f"      snippet: ...{snip}...")
        print()
    return 0


def cmd_dockets(args: argparse.Namespace) -> int:
    """Search RECAP dockets by free text."""
    params: dict[str, Any] = {"type": "r", "q": args.query}
    if args.court: params["court"] = args.court
    if args.date_filed_after: params["filed_after"] = args.date_filed_after
    res = _get("search", params)
    if "_error" in res:
        return _handle_error(res)
    results = res.get("results") or []
    print(f"Docket search '{args.query}'"
          + (f" in court={args.court}" if args.court else "")
          + f" — {len(results)} hits (top {args.limit}):\n")
    for r in results[:args.limit]:
        print(f"  • {r.get('caseName') or '?'}")
        print(f"      court:  {r.get('court_citation_string') or r.get('court') or '?'}")
        print(f"      filed:  {r.get('dateFiled') or '?'}")
        print(f"      docket: {r.get('docketNumber') or '?'}")
        print(f"      nature: {r.get('suitNature') or '?'}")
        print(f"      url:    https://www.courtlistener.com{r.get('absolute_url') or ''}")
        if r.get("snippet"):
            snip = r["snippet"].replace("\n", " ")[:200]
            print(f"      snippet: ...{snip}...")
        print()
    return 0


def cmd_dockets_party(args: argparse.Namespace) -> int:
    """Search dockets where a specific party is named.

    v4 doesn't expose a party_name filter on /dockets. Use the search
    endpoint (type=r for RECAP) with the party name in the q parameter —
    CourtListener's full-text indexes party fields so name searches work.
    """
    # Quote the name to keep multi-word names intact
    q = f'"{args.party_name}"'
    res = _get("search", {"type": "r", "q": q})
    if "_error" in res:
        return _handle_error(res)
    results = res.get("results") or []
    print(f"Dockets / RECAP docs mentioning party '{args.party_name}' "
          f"— {len(results)} hits (showing top {args.limit}):\n")
    for r in results[:args.limit]:
        print(f"  • {r.get('caseName') or r.get('case_name') or '?'}")
        print(f"      court:  {r.get('court_citation_string') or r.get('court') or '?'}")
        print(f"      filed:  {r.get('dateFiled') or r.get('date_filed') or '?'}")
        print(f"      docket: {r.get('docketNumber') or r.get('docket_number') or '?'}")
        print(f"      nature: {r.get('suitNature') or '?'}")
        print(f"      url:    https://www.courtlistener.com{r.get('absolute_url') or ''}")
        if r.get("snippet"):
            snip = r["snippet"].replace("\n", " ")[:200]
            print(f"      snippet: ...{snip}...")
        print()
    return 0


def cmd_opinion_text(args: argparse.Namespace) -> int:
    """Fetch full text of one opinion by id."""
    res = _get(f"opinions/{args.opinion_id}", {})
    if "_error" in res:
        return _handle_error(res)
    print(f"Opinion {args.opinion_id}")
    print(f"  cluster: {res.get('cluster') or '?'}")
    print(f"  type:    {res.get('type') or '?'}")
    print(f"  author:  {res.get('author') or '?'}")
    text = res.get("plain_text") or res.get("html") or "(no text)"
    if args.full:
        print("\n--- FULL TEXT ---\n")
        print(text)
    else:
        print(f"\n--- TEXT (first 2000 chars) ---\n")
        print(text[:2000])
        if len(text) > 2000:
            print(f"\n... [{len(text)-2000} more chars, pass --full to see]")
    return 0


def cmd_recap(args: argparse.Namespace) -> int:
    """Search the RECAP archive (federal court documents)."""
    params: dict[str, Any] = {"type": "r", "q": args.query}
    if args.court: params["court"] = args.court
    res = _get("search", params)
    if "_error" in res:
        return _handle_error(res)
    results = res.get("results") or []
    print(f"RECAP search '{args.query}' — {len(results)} hits (top {args.limit}):\n")
    for r in results[:args.limit]:
        print(f"  • {r.get('caseName') or '?'}  [{r.get('docketNumber')}]")
        print(f"      court: {r.get('court_citation_string') or r.get('court') or '?'}")
        print(f"      filed: {r.get('dateFiled') or '?'}")
        print(f"      url:   https://www.courtlistener.com{r.get('absolute_url') or ''}")
        if r.get("snippet"):
            snip = r["snippet"].replace("\n", " ")[:250]
            print(f"      snippet: ...{snip}...")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    global _timeout_sec
    ap = argparse.ArgumentParser(description="CourtListener REST API CLI")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC,
                    metavar="SECS",
                    help=f"Per-request read timeout in seconds (default {DEFAULT_TIMEOUT_SEC})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_verify = sub.add_parser("verify", help="Verify a specific case citation")
    p_verify.add_argument("case_name", type=str)
    p_verify.add_argument("--reporter", type=str, help='e.g., "261 F.3d 821"')
    p_verify.add_argument("--court", type=str)

    p_op = sub.add_parser("opinions", help="Free-text opinion search")
    p_op.add_argument("query", type=str)
    p_op.add_argument("--court", type=str)
    p_op.add_argument("--judge", type=str)
    p_op.add_argument("--limit", type=int, default=10)

    p_dk = sub.add_parser("dockets", help="RECAP docket search")
    p_dk.add_argument("query", type=str)
    p_dk.add_argument("--court", type=str, help="e.g., 'cacd' for C.D. Cal.")
    p_dk.add_argument("--date-filed-after", type=str)
    p_dk.add_argument("--limit", type=int, default=10)

    p_dp = sub.add_parser("dockets-party", help="Dockets with a specific party name")
    p_dp.add_argument("party_name", type=str)
    p_dp.add_argument("--limit", type=int, default=20)

    p_ot = sub.add_parser("opinion-text", help="Fetch full text of an opinion")
    p_ot.add_argument("opinion_id", type=int)
    p_ot.add_argument("--full", action="store_true")

    p_rc = sub.add_parser("recap", help="Free-text RECAP archive search")
    p_rc.add_argument("query", type=str)
    p_rc.add_argument("--court", type=str)
    p_rc.add_argument("--limit", type=int, default=10)

    args = ap.parse_args(argv)
    _timeout_sec = args.timeout

    if not TOKEN:
        print("WARNING: COURTLISTENER_TOKEN not set; using unauthenticated tier (rate-limited).",
              file=sys.stderr)

    dispatch = {
        "verify": cmd_verify,
        "opinions": cmd_opinions,
        "dockets": cmd_dockets,
        "dockets-party": cmd_dockets_party,
        "opinion-text": cmd_opinion_text,
        "recap": cmd_recap,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
