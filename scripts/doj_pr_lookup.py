"""DOJ press release lookup via the public JSON API + local cache.

DOJ exposes a public, unauthenticated REST API at
``https://www.justice.gov/api/v1/press_releases.json`` that returns full
press release content as structured JSON. The API returns 200 with full body
text in milliseconds — no headless browser needed.

**IMPORTANT — body-field search uses LOCAL CACHE, not the API.** Empirical
testing (2026-05-21) confirmed the DOJ API silently ignores the ``body=``
parameter — body queries return the entire 267K-release corpus regardless
of the query string. The cache at ``data/source/doj_api/raw_press_releases.jsonl``
(5,937 healthcare-fraud releases) is grep'd locally instead. Title queries
still hit the live API since ``title=`` filtering works correctly.

API fields:
  - uuid          stable id
  - title         press release title
  - date, created updated  dates
  - body          full HTML body
  - teaser        first-paragraph summary (plain text)
  - url           public justice.gov URL
  - component     issuing USAO / division
  - topic         array of DOJ-side topic tags

Search filters:
  - title=<text>           substring match on title  (DOJ API)
  - body=<text>            substring match on body   (LOCAL CACHE — 5,937 hc-fraud releases)
  - date=YYYY-MM-DD        exact date                (DOJ API)
  - date_>=YYYY-MM-DD      after                     (DOJ API)
  - date_<=YYYY-MM-DD      before                    (DOJ API)
  - component=<short>      USAO code                 (DOJ API)
  - topic=<text>           topic tag                 (DOJ API)
  - pagesize=N (max 50)
  - page=N (0-indexed)

Usage:
    # Search by title text
    python scripts/doj_pr_lookup.py search "Larch" --limit 5

    # Search by body text (e.g., a defendant name appearing in any release)
    python scripts/doj_pr_lookup.py search "Provider Larch" --field body

    # Time-window + topic
    python scripts/doj_pr_lookup.py search "incident-to" \
        --date-after 2020-01-01 --component USAO-CDCA

    # Fetch a known URL (extracts slug from URL, hits API for full text)
    python scripts/doj_pr_lookup.py fetch \
        https://www.justice.gov/usao-sdny/pr/doctor-and-physical-therapist-found-guilty

    # By exact uuid
    python scripts/doj_pr_lookup.py uuid <uuid>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import requests

API_BASE = "https://www.justice.gov/api/v1/press_releases.json"
TIMEOUT_SEC = 30
HEADERS = {
    "User-Agent": "fraud-investigator/1.0",
    "Accept": "application/json",
}

# Local cache of healthcare-fraud press releases. Used for body-field
# searches because the DOJ API does not honor ``body=`` filtering (returns
# entire corpus regardless of query). Cache is built/refreshed by
# ``scripts/ingest/doj_press_releases.py``.
LOCAL_CACHE = Path(__file__).resolve().parent.parent / "data" / "source" / "doj_api" / "raw_press_releases.jsonl"


def _api(params: dict[str, Any]) -> dict:
    """Hit the DOJ API. Returns JSON or {error: ...}."""
    try:
        r = requests.get(API_BASE, params=params, headers=HEADERS, timeout=TIMEOUT_SEC)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        return {"_error": f"HTTP {e.response.status_code}",
                "_body": (e.response.text or "")[:300]}
    except requests.RequestException as e:
        return {"_error": str(e)}


def _html_to_text(html: str) -> str:
    """Strip HTML to plain text for agent-readable output."""
    if not html:
        return ""
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Decode entities
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'")
                .replace("&rsquo;", "'").replace("&lsquo;", "'")
                .replace("&ldquo;", '"').replace("&rdquo;", '"')
                .replace("&mdash;", "—").replace("&ndash;", "–")
                .replace("&#8217;", "'").replace("&#8220;", '"').replace("&#8221;", '"'))
    # Convert breaks
    text = re.sub(r"<(?:br|/p|/div|/li|/h[1-6])\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _format_record(r: dict, *, body_full: bool = False, body_grep: str | None = None) -> str:
    """Pretty-print one press-release record."""
    out: list[str] = []
    out.append(f"Title: {r.get('title', '?')}")
    out.append(f"Date:  {r.get('date', '?')} (created {r.get('created', '?')})")
    out.append(f"URL:   {r.get('url', '?')}")
    out.append(f"UUID:  {r.get('uuid', '?')}")
    out.append(f"Component: {r.get('component', '?')}")
    topics = r.get("topic")
    if topics and isinstance(topics, list):
        names = [t.get("name") if isinstance(t, dict) else str(t) for t in topics]
        out.append(f"Topics: {', '.join(filter(None, names))}")
    teaser = (r.get("teaser") or "").strip()
    if teaser:
        out.append(f"\nTeaser: {teaser[:400]}")

    body = _html_to_text(r.get("body") or "")
    if body_grep:
        pattern = re.compile(body_grep, flags=re.IGNORECASE | re.MULTILINE)
        lines = body.split("\n")
        matches: list[str] = []
        for i, line in enumerate(lines):
            if pattern.search(line):
                start, end = max(0, i - 1), min(len(lines), i + 2)
                matches.extend(lines[start:end])
                matches.append("---")
        if matches:
            out.append("\n--- BODY MATCHES ---")
            out.append("\n".join(matches))
        else:
            out.append("\n(no body matches)")
    elif body_full:
        out.append("\n--- FULL BODY ---")
        out.append(body)
    else:
        out.append(f"\nBody (first 800 chars):\n{body[:800]}")
        if len(body) > 800:
            out.append(f"... [{len(body)-800} more chars; pass --full or --grep PATTERN]")
    return "\n".join(out)


def _search_local_cache(query: str, *, limit: int,
                        date_after: str | None = None,
                        date_before: str | None = None,
                        component: str | None = None) -> list[dict]:
    """Grep the local cache (5,937 healthcare-fraud releases) for body matches.

    The DOJ API silently ignores ``body=`` filtering, so all body queries
    route here. Local-only search means we can't find non-healthcare-fraud
    releases this way — but that's the only DOJ-press subset the agents care
    about during fraud investigation.
    """
    if not LOCAL_CACHE.exists():
        return []
    pattern = re.compile(re.escape(query), flags=re.IGNORECASE)
    hits: list[dict] = []
    with LOCAL_CACHE.open() as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Apply API-equivalent filters first (cheap)
            if date_after and (r.get("date") or "") < date_after:
                continue
            if date_before and (r.get("date") or "") > date_before:
                continue
            if component:
                comp_str = json.dumps(r.get("component") or "")
                if component.lower() not in comp_str.lower():
                    continue
            # Substring match on body (the actual filter)
            body = r.get("body") or ""
            if pattern.search(body):
                hits.append(r)
                if len(hits) >= limit * 3:  # over-collect for relevance sort
                    break
    # Sort by date desc so most recent appears first (matches typical
    # investigator interest in fresh enforcement)
    hits.sort(key=lambda r: r.get("date") or "", reverse=True)
    return hits[:limit]


def cmd_search(args: argparse.Namespace) -> int:
    # Body queries: route to local cache (DOJ API doesn't honor body=)
    if args.field == "body":
        results = _search_local_cache(
            args.query, limit=args.limit,
            date_after=args.date_after, date_before=args.date_before,
            component=args.component,
        )
        print(f"DOJ press release search: field=body query='{args.query}' (LOCAL CACHE — hc-fraud only)")
        print(f"  showing {len(results)} hits from {LOCAL_CACHE.name}\n")
        if not results:
            print("  (no body matches in local cache)")
            print(f"  cache path: {LOCAL_CACHE}")
            print(f"  Note: the DOJ API does NOT support body-field filtering; the local")
            print(f"  cache only includes healthcare-fraud releases. To search the full")
            print(f"  DOJ corpus, use --field title or fetch by URL/UUID.")
            return 0
        for r in results:
            print(_format_record(r, body_full=args.full, body_grep=args.grep))
            print("---")
        return 0

    # Title queries: DOJ API (which honors title= correctly).
    # NOTE: the API SILENTLY IGNORES date_>=/date_<= (verified 2026-05-29 — it returns
    # the all-time corpus regardless, same as the body= bug). But it DOES honor
    # sort=date&direction=DESC. So for date-scoped queries we sort newest-first,
    # paginate, and filter client-side, stopping once results fall before date_after.
    import datetime as _dt
    def _to_unix(d: str) -> int:
        return int(_dt.datetime.strptime(d, "%Y-%m-%d")
                   .replace(tzinfo=_dt.timezone.utc).timestamp())
    after_u = _to_unix(args.date_after) if args.date_after else None
    before_u = _to_unix(args.date_before) if args.date_before else None

    base: dict[str, Any] = {"pagesize": 50, "title": args.query,
                            "sort": "date", "direction": "DESC"}
    if args.component: base["component"] = args.component
    if args.topic: base["topic"] = args.topic

    results: list[dict] = []
    total = None
    MAX_PAGES = 60          # safety cap (60*50 = 3000 newest PRs scanned); a
                            # date_after query stops at the cutoff well before this.
    page, stop = 0, False
    while len(results) < args.limit and page < MAX_PAGES and not stop:
        res = _api({**base, "page": page})
        if "_error" in res:
            print(f"ERROR: {res['_error']}", file=sys.stderr)
            if res.get("_body"):
                print(f"Body: {res['_body']}", file=sys.stderr)
            return 1
        if total is None:
            total = (res.get("metadata", {}).get("resultset", {}) or {}).get("count") or "?"
        batch = res.get("results", []) or []
        if not batch:
            break
        for r in batch:
            dt = int(r.get("date") or 0)
            if after_u is not None and dt < after_u:
                stop = True            # sorted DESC → everything beyond here is older
                break
            if before_u is not None and dt > before_u:
                continue               # newer than the upper bound; skip
            results.append(r)
            if len(results) >= args.limit:
                break
        page += 1

    _rng = (f" date>={args.date_after}" if args.date_after else "") + \
           (f" date<={args.date_before}" if args.date_before else "")
    print(f"DOJ press release search: field=title query='{args.query}'{_rng}")
    print(f"  corpus total (title match, unfiltered): {total}; "
          f"returning {len(results)} after client-side date filter (limit={args.limit})\n")
    for r in results:
        print(_format_record(r, body_full=args.full, body_grep=args.grep))
        print("---")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """Fetch by URL — extract slug, search title for it."""
    # The slug is the last path segment, e.g., 'doctor-and-physical-therapist-found-guilty'
    m = re.search(r"/pr/([^/?#]+)", args.url)
    if not m:
        print(f"Could not extract press-release slug from URL: {args.url}", file=sys.stderr)
        return 1
    slug = m.group(1)
    # Try the title field with words from the slug
    title_query = slug.replace("-", " ")[:60]  # take ~60 chars
    res = _api({"title": title_query, "pagesize": 50})
    if "_error" in res:
        print(f"ERROR: {res['_error']}", file=sys.stderr)
        return 1
    candidates = res.get("results", []) or []
    # Look for one whose url exactly matches
    match = next((r for r in candidates if (r.get("url") or "").rstrip("/") == args.url.rstrip("/")), None)
    if not match and candidates:
        # Take the closest title match
        match = candidates[0]
        print(f"NOTE: no exact URL match; closest title match:\n", file=sys.stderr)
    if not match:
        print(f"No press release found matching URL: {args.url}", file=sys.stderr)
        return 1
    print(_format_record(match, body_full=args.full, body_grep=args.grep))
    return 0


def cmd_uuid(args: argparse.Namespace) -> int:
    """Fetch by exact uuid."""
    res = _api({"uuid": args.uuid})
    if "_error" in res:
        print(f"ERROR: {res['_error']}", file=sys.stderr)
        return 1
    results = res.get("results", []) or []
    if not results:
        print(f"No press release with uuid {args.uuid}", file=sys.stderr)
        return 1
    print(_format_record(results[0], body_full=args.full, body_grep=args.grep))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_s = sub.add_parser("search", help="Search DOJ press releases")
    p_s.add_argument("query", type=str)
    p_s.add_argument("--field", choices=["title", "body"], default="title",
                     help="Search the title (default) or body text")
    p_s.add_argument("--component", type=str, help="e.g., USAO-CDCA, USAO-SDNY")
    p_s.add_argument("--topic", type=str, help="DOJ topic tag")
    p_s.add_argument("--date-after", type=str, help="YYYY-MM-DD")
    p_s.add_argument("--date-before", type=str, help="YYYY-MM-DD")
    p_s.add_argument("--limit", type=int, default=10)
    p_s.add_argument("--full", action="store_true", help="Print full body")
    p_s.add_argument("--grep", type=str, help="Regex to find in body")

    p_f = sub.add_parser("fetch", help="Fetch by justice.gov URL")
    p_f.add_argument("url", type=str)
    p_f.add_argument("--full", action="store_true")
    p_f.add_argument("--grep", type=str)

    p_u = sub.add_parser("uuid", help="Fetch by exact UUID")
    p_u.add_argument("uuid", type=str)
    p_u.add_argument("--full", action="store_true")
    p_u.add_argument("--grep", type=str)

    args = ap.parse_args(argv)
    dispatch = {"search": cmd_search, "fetch": cmd_fetch, "uuid": cmd_uuid}
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
