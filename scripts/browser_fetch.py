"""HTTP fetcher with browser-fingerprint mimicking for WAF/Akamai bypass.

Many federal sites (justice.gov, courts.gov, ecfr.gov, some state board
sites) sit behind Akamai or similar WAFs that return 403 to bare GETs.
This script wraps `requests` with a full browser-like header set, cookie
persistence, and HTTP/2 simulation that gets past most WAF challenges.

Heavily-protected JS-challenge sites (sealed-doc PACER, ECF login pages)
will still fail — those need a real headless browser (Playwright). For
public press releases, OIG enforcement pages, state board search pages
(GET-form), and most .gov landing pages, this works.

Output: returns clean HTML/text/JSON to stdout. The agent can pipe into
grep/python for downstream processing.

Exit codes:
    0  OK — content returned successfully
    3  BLOCKED — 403/429 persisted after retries (WAF/anti-bot); absence of
       content does NOT mean absence of the underlying fact; try an alternate source
    4  NOT_FOUND — 404
    5  ERROR — timeout, DNS failure, or other non-HTTP error

Usage:
    # Plain HTML fetch
    python scripts/browser_fetch.py "https://www.justice.gov/usao-sdny/pr/..."

    # Extract text content only (strip HTML)
    python scripts/browser_fetch.py URL --text

    # Quick reachability check (HEAD / cheap GET)
    python scripts/browser_fetch.py URL --head

    # Search the fetched content for a pattern
    python scripts/browser_fetch.py URL --grep "Between [0-9]{4}"

    # Save to file (for large responses)
    python scripts/browser_fetch.py URL --output /tmp/fetched.html

    # JSON response (parses + pretty-prints)
    python scripts/browser_fetch.py URL --json
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

# Cookie jar persisted across calls (helps with sites that issue tracking
# cookies on first visit and require them on subsequent requests).
COOKIE_JAR = Path.home() / ".fraud_investigator_cookies.txt"

# Realistic Chrome on macOS headers. WAFs fingerprint on these.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "DNT": "1",
}

TIMEOUT_SEC = 30

# Statuses that warrant a retry (WAF challenges often clear on second attempt
# with a warmed session/cookies; 5xx are transient server errors).
_RETRY_STATUSES = {403, 429, 500, 502, 503, 504}

# Backoff delays (seconds) between attempts. Jitter ±0.2s added at runtime.
_BACKOFF = [0.5, 1.5, 3.0]

# Max attempts = 1 initial + len(_BACKOFF) retries
_MAX_ATTEMPTS = 1 + len(_BACKOFF)


def _load_cookies(session: requests.Session) -> None:
    """Load cookies from disk if available."""
    if COOKIE_JAR.exists():
        try:
            data = json.loads(COOKIE_JAR.read_text())
            for k, v in data.items():
                session.cookies.set(k, v)
        except Exception:
            pass


def _save_cookies(session: requests.Session) -> None:
    """Persist cookies for next call."""
    try:
        data = {k: v for k, v in session.cookies.get_dict().items()}
        COOKIE_JAR.write_text(json.dumps(data))
    except Exception:
        pass


def _origin_referer(url: str) -> str:
    """Return the origin (scheme + host) as a Referer header value."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def fetch(url: str, *, json_response: bool = False) -> tuple[int, str, str]:
    """Return (status_code, content_type, body_text).

    Retries up to 3 times with exponential backoff + jitter on 403/429/5xx/timeout.
    On persistent block, status_code is the last HTTP status received (403/429/etc.).
    On network failure, status_code is 0 and body_text begins with 'REQUEST FAILED: '.
    Signature is backward-compatible with the original.
    """
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    # Referer + adjusted Sec-Fetch-Site make the request look like a same-origin
    # navigation, which clears many Akamai 403s.
    s.headers.update({
        "Referer": _origin_referer(url),
        "Sec-Fetch-Site": "same-origin",
    })
    _load_cookies(s)

    last_status: int = 0
    last_ctype: str = ""
    last_body: str = ""

    for attempt in range(_MAX_ATTEMPTS):
        if attempt > 0:
            delay = _BACKOFF[attempt - 1] + random.uniform(-0.2, 0.2)
            time.sleep(max(0.0, delay))

        try:
            r = s.get(url, timeout=TIMEOUT_SEC, allow_redirects=True)
            _save_cookies(s)
            last_status = r.status_code
            last_ctype = r.headers.get("Content-Type", "")
            last_body = r.text

            if last_status not in _RETRY_STATUSES:
                return last_status, last_ctype, last_body
            # Else: fall through to retry
        except requests.Timeout as e:
            last_status = 0
            last_ctype = ""
            last_body = f"REQUEST FAILED: timeout — {e}"
            # Timeout is retryable
        except requests.RequestException as e:
            # Non-retryable network error
            return 0, "", f"REQUEST FAILED: {e}"

    return last_status, last_ctype, last_body


def html_to_text(html: str) -> str:
    """Quick-and-dirty HTML → text. For agent-readable output, not perfect rendering."""
    # Strip script/style blocks
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Strip head section (full HTML head — usually noise)
    html = re.sub(r"<head[^>]*>.*?</head>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Decode common entities
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'"))
    # Replace <br>, </p>, </div>, </li> with newlines
    html = re.sub(r"<(?:br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", html, flags=re.IGNORECASE)
    # Strip remaining tags
    html = re.sub(r"<[^>]+>", "", html)
    # Collapse whitespace
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


_STRUCTURED_API_HINTS = {
    "justice.gov":            "scripts/doj_pr_lookup.py — DOJ public API; full press-release JSON without Akamai",
    "npiregistry.cms.hhs.gov": "scripts/nppes_lookup.py — NPPES live API",
    "courtlistener.com":      "scripts/courtlistener_search.py — auth-token CL REST v4",
}


def _hint_for_url(url: str) -> str | None:
    for host_substr, hint in _STRUCTURED_API_HINTS.items():
        if host_substr in url:
            return hint
    return None


def _status_line(status: int, *, blocked: bool = False, detail: str = "") -> str:
    """Return the canonical STATUS: … line printed before any body output."""
    if blocked or status in (403, 429):
        return (
            f"STATUS: BLOCKED {status} "
            "(anti-bot/WAF — absence of content here does NOT mean absence of "
            "the underlying fact; try an alternate source)"
        )
    if status == 404:
        return f"STATUS: NOT_FOUND 404"
    if status == 200:
        return f"STATUS: OK 200"
    if status == 0:
        return f"STATUS: ERROR {detail or 'network failure'}"
    if status >= 400:
        return f"STATUS: NOT_FOUND {status}"
    if status >= 500:
        return f"STATUS: ERROR {status}"
    return f"STATUS: OK {status}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url", type=str)
    ap.add_argument("--text", action="store_true", help="Strip HTML, return text only")
    ap.add_argument("--json", dest="as_json", action="store_true", help="Parse + pretty-print JSON")
    ap.add_argument("--grep", type=str, help="Regex; return matching lines (with surrounding context)")
    ap.add_argument("--output", "-o", type=Path, help="Save body to file")
    ap.add_argument(
        "--head",
        nargs="?",
        const=True,
        default=None,
        help=(
            "Quick reachability check: HEAD/cheap GET, print STATUS line only and exit. "
            "Optionally pass an integer N to return first N lines/chars of body instead."
        ),
    )
    args = ap.parse_args(argv)

    hint = _hint_for_url(args.url)
    if hint:
        print(f"WARNING: STRUCTURED API AVAILABLE — prefer: {hint}", file=sys.stderr)
        print(f"  (continuing with HTML fetch, but the API is faster + bypasses Akamai)", file=sys.stderr)

    # --head with no N: quick reachability triage — just print STATUS and exit
    if args.head is True:
        status, ctype, body = fetch(args.url)
        blocked = status in _RETRY_STATUSES
        detail = body if status == 0 else ""
        print(_status_line(status, blocked=blocked, detail=detail))
        if status == 0 or status in (403, 429):
            return 3 if status in (0, 403, 429) else 5
        if status == 404:
            return 4
        if status >= 400:
            return 4
        if status >= 500:
            return 5
        return 0

    status, ctype, body = fetch(args.url)

    # Determine if we ended up blocked after all retries
    blocked = status in _RETRY_STATUSES

    # Print STATUS line first so agents always see it regardless of --text/--output
    if status == 0:
        detail = body.replace("REQUEST FAILED: ", "")
        print(_status_line(0, detail=detail))
        print(body, file=sys.stderr)
        return 5

    if blocked:
        print(_status_line(status, blocked=True))
        print(f"Content-Type: {ctype}", file=sys.stderr)
        print(f"\nBody preview:\n{body[:500]}", file=sys.stderr)
        return 3

    if status == 404:
        print(_status_line(404))
        print(f"HTTP {status} from {args.url}", file=sys.stderr)
        return 4

    if status >= 400:
        print(_status_line(status))
        print(f"HTTP {status} from {args.url}", file=sys.stderr)
        print(f"Content-Type: {ctype}", file=sys.stderr)
        print(f"\nBody preview:\n{body[:500]}", file=sys.stderr)
        return 4

    if status >= 500:
        print(_status_line(status))
        print(f"HTTP {status} from {args.url}", file=sys.stderr)
        return 5

    print(_status_line(status))
    print(f"HTTP {status} · {ctype} · {len(body)} bytes", file=sys.stderr)

    # JSON pretty-print
    if args.as_json:
        try:
            d = json.loads(body)
            out = json.dumps(d, indent=2)
        except Exception as e:
            print(f"Not valid JSON: {e}", file=sys.stderr)
            out = body
    elif args.text:
        out = html_to_text(body)
    else:
        out = body

    # Grep filter
    if args.grep:
        pattern = re.compile(args.grep, flags=re.IGNORECASE | re.MULTILINE)
        lines = out.split("\n")
        kept: list[str] = []
        for i, line in enumerate(lines):
            if pattern.search(line):
                # 1-line context before/after
                start = max(0, i - 1)
                end = min(len(lines), i + 2)
                kept.extend(lines[start:end])
                kept.append("---")
        out = "\n".join(kept) if kept else "(no matches)"

    # --head N (integer) truncation
    if args.head is not None and args.head is not True:
        n = int(args.head)
        if "\n" in out[:1000]:  # line-based
            out = "\n".join(out.split("\n")[:n])
        else:  # char-based
            out = out[:n]

    if args.output:
        args.output.write_text(out)
        print(f"Wrote {len(out)} bytes to {args.output}", file=sys.stderr)
    else:
        print(out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
