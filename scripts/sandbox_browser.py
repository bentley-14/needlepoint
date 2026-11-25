"""Point-in-time ("sandboxed") web access that cuts any content dated on/after a cutoff.

WHY: to evaluate the investigator loop on a *true-positive* case fairly, the agent must
not be able to read anything that only became public AFTER the fraud was discovered — the
DOJ press release announcing the charges, news coverage, the court docket, the exclusion
listing. Those are the answer. This tool gives the investigator a browser that only ever
returns content as it existed *before* a chosen timepoint (the case's discovery / charge
date), so the eval measures detection-from-pre-discovery-signals, not answer-reading.

Cutoff semantics: `--as-of YYYY-MM-DD` returns content strictly BEFORE the start of that
day. Entries dated on/after the cutoff are cut. (The DOJ release published ON the discovery
date is therefore excluded.)

Mechanism:
  - `fetch`  : returns the Wayback Machine snapshot <= cutoff (the contemporaneous page).
               If no snapshot exists before the cutoff, falls back to a live fetch ONLY if
               the page's own publish-date < cutoff; otherwise BLOCKS. So post-cutoff
               content can never leak through.
  - `search` : date-gated DOJ press-release search (delegates to doj_pr_lookup
               --date-before), cutting any release on/after the cutoff.
  - `check`  : report whether/when an archived snapshot exists before the cutoff.
  - `cutoff-for-npi` : look up a gold_set defendant's discovery date (outcome.date) to use
               as the cutoff.

Usage:
    python scripts/sandbox_browser.py check  <url> --as-of YYYY-MM-DD
    python scripts/sandbox_browser.py fetch  <url> --as-of YYYY-MM-DD --text
    python scripts/sandbox_browser.py search "<query>" --as-of YYYY-MM-DD
    python scripts/sandbox_browser.py cutoff-for-npi <NPI>      # gold_set lookup; not callable from a calibration workspace

Exit codes:  0 = content returned;  2 = BLOCKED (nothing available before cutoff);  1 = error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser_fetch import BROWSER_HEADERS, html_to_text  # reuse the WAF-bypass fetcher's helpers
from browser_fetch import fetch as raw_browser_fetch

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLD_SET = REPO_ROOT / "data/extracted/gold_set.jsonl"
DOJ_LOOKUP = Path(__file__).resolve().parent / "doj_pr_lookup.py"

WAYBACK_CDX = "http://web.archive.org/cdx/search/cdx"
WAYBACK_SNAP = "http://web.archive.org/web/{ts}id_/{url}"  # id_ = raw archived bytes, no banner
ARCHIVE_HEADERS = {"User-Agent": "fraud-investigator-sandbox/1.0"}
TIMEOUT = 45

_DATE_PATTERNS = [
    r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+name=["\'](?:datePublished|publishdate|pubdate|date)["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+itemprop=["\']datePublished["\'][^>]+content=["\']([^"\']+)',
    r'"datePublished"\s*:\s*"([^"]+)"',
    r'<time[^>]+datetime=["\']([^"\']+)',
]


def parse_date(s: str) -> date | None:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def extract_pubdate(html: str) -> date | None:
    for pat in _DATE_PATTERNS:
        m = re.search(pat, html, flags=re.IGNORECASE)
        if m:
            d = parse_date(m.group(1))
            if d:
                return d
    return None


def _archive_get(url: str, params: dict | None = None, tries: int = 3) -> requests.Response | None:
    """GET archive.org with retry/backoff (it rate-limits bursts). None if all attempts fail."""
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=ARCHIVE_HEADERS, timeout=TIMEOUT,
                             allow_redirects=True)
            if r.status_code == 200:
                return r
        except requests.RequestException:
            pass
        if i < tries - 1:
            time.sleep(1.5 * (i + 1))
    return None


def wayback_latest_before(url: str, cutoff: date) -> tuple[str, str] | None:
    """Most-recent archived snapshot with timestamp < cutoff. Returns (timestamp, original_url)."""
    cutoff_ts = cutoff.strftime("%Y%m%d") + "000000"  # start of cutoff day -> strictly before it
    r = _archive_get(WAYBACK_CDX, params={
        "url": url, "to": cutoff_ts, "output": "json",
        "fl": "timestamp,original,statuscode", "filter": "statuscode:200",
        "collapse": "digest", "limit": "-15",  # last 15 = most recent <= cutoff
    })
    if r is None:
        return None
    try:
        rows = r.json()
    except ValueError:
        return None
    data = rows[1:] if rows and rows[0] and rows[0][0] == "timestamp" else rows
    if not data:
        return None
    best = max(data, key=lambda row: row[0])  # max timestamp <= cutoff
    return best[0], best[1]


def fetch_archived(ts: str, original: str) -> tuple[int, str]:
    r = _archive_get(WAYBACK_SNAP.format(ts=ts, url=original))
    return (r.status_code, r.text) if r is not None else (0, "archive fetch failed after retries")


def _fmt_ts(ts: str) -> str:
    try:
        return datetime.strptime(ts[:8], "%Y%m%d").date().isoformat()
    except ValueError:
        return ts


def cmd_check(args: argparse.Namespace) -> int:
    cutoff = date.fromisoformat(args.as_of)
    snap = wayback_latest_before(args.url, cutoff)
    if snap:
        print(f"AVAILABLE: latest archived snapshot before {args.as_of} is {_fmt_ts(snap[0])} "
              f"(timestamp {snap[0]})")
        return 0
    print(f"BLOCKED: no Wayback snapshot before {args.as_of} for {args.url} "
          f"(page likely did not exist yet / would be post-discovery leakage)")
    return 2


def cmd_fetch(args: argparse.Namespace) -> int:
    cutoff = date.fromisoformat(args.as_of)
    snap = wayback_latest_before(args.url, cutoff)
    body, provenance = None, None
    if snap:
        status, html = fetch_archived(snap[0], snap[1])
        if status == 200 and html:
            body = html
            provenance = f"Wayback snapshot {_fmt_ts(snap[0])} (ts {snap[0]}), as-of < {args.as_of}"
    if body is None:
        if not args.allow_live_if_dated:
            print(f"BLOCKED: no Wayback snapshot before {args.as_of} for {args.url} "
                  f"(page likely didn't exist pre-cutoff). Pass --allow-live-if-dated to try a live "
                  f"fetch gated on the page's own publish date.", file=sys.stderr)
            return 2
        # Opt-in fallback: live fetch, released ONLY if the page's OWN publish date is < cutoff.
        # Weaker than Wayback — a live page can carry post-cutoff edits under an old publish date.
        status, ctype, live = raw_browser_fetch(args.url)
        if status != 200:
            print(f"BLOCKED: no snapshot before {args.as_of} and live fetch returned HTTP {status}.",
                  file=sys.stderr)
            return 2
        pub = extract_pubdate(live)
        if pub and pub < cutoff:
            body = live
            provenance = f"LIVE page (no archive); publish date {pub} < {args.as_of} [unverified — page may have changed since]"
        else:
            why = f"live publish date {pub} >= cutoff" if pub else "live page has no detectable publish date"
            print(f"BLOCKED_AFTER_CUTOFF: no archived snapshot before {args.as_of}, and {why}.",
                  file=sys.stderr)
            return 2

    print(f"SANDBOX [{provenance}]", file=sys.stderr)
    out = html_to_text(body) if args.text else body
    if args.grep:
        pat = re.compile(args.grep, flags=re.IGNORECASE | re.MULTILINE)
        out = "\n".join(l for l in out.split("\n") if pat.search(l)) or "(no matches)"
    if args.head:
        out = out[: args.head]
    print(out)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    cutoff = date.fromisoformat(args.as_of)
    # doj_pr_lookup --date-before is inclusive; subtract a day so the cutoff-day release is cut.
    doj_before = date.fromordinal(cutoff.toordinal() - 1).isoformat()
    cmd = [sys.executable, str(DOJ_LOOKUP), "search", args.query,
           "--field", args.field, "--date-before", doj_before, "--limit", str(args.limit)]
    print(f"SANDBOX: DOJ press releases strictly before {args.as_of} (<= {doj_before}); "
          f"anything announcing the case on/after the cutoff is cut.", file=sys.stderr)
    return subprocess.run(cmd).returncode


def cmd_cutoff_for_npi(args: argparse.Namespace) -> int:
    npi = args.npi.strip()
    if not GOLD_SET.exists():
        print(f"gold_set not found: {GOLD_SET}", file=sys.stderr)
        return 1
    with GOLD_SET.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if str(d.get("npi") or d.get("v4_npi") or "") == npi:
                oc = d.get("outcome") or {}
                years = [y for y in (d.get("all_active_years") or []) if isinstance(y, int)]
                max_y = max(years) if years else None
                conservative = f"{max_y + 1}-01-01" if max_y else None
                print(f"NPI {npi}  {d.get('defendant_name')}  case={d.get('case_id')}")
                print(f"  fraud-active years:     {d.get('all_active_years')}")
                print(f"  outcome (DISPOSITION):  {oc.get('date')}  ({oc.get('type')})")
                print(f"  source_url:             {d.get('source_url')}")
                print()
                print("  CUTOFF GUIDANCE — calibration must be blind to anything suggestive of the fraud:")
                if conservative:
                    print(f"    recommended (end of fraud, before charges):  --as-of {conservative}")
                print(f"    loosest upper bound (disposition date):       --as-of {oc.get('date')}")
                print(f"    strictest:                                    the actual investigation/charge date, if known")
                print()
                print("  WARNING: outcome.date is usually the plea/sentencing date — often months-to-years AFTER")
                print("  the case became public (indictment/arrest/raid), so it would leave the charge announcement")
                print("  VISIBLE. Prefer the charge date, or the recommended fraud-end cutoff above.")
                return 0
    print(f"NPI {npi} not found in gold_set", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch", help="point-in-time fetch (Wayback snapshot <= cutoff; else BLOCK)")
    p.add_argument("url"); p.add_argument("--as-of", required=True, help="YYYY-MM-DD")
    p.add_argument("--text", action="store_true"); p.add_argument("--grep"); p.add_argument("--head", type=int)
    p.add_argument("--allow-live-if-dated", action="store_true",
                   help="if no archive exists, fall back to a live fetch but release it only when the "
                        "page's own publish date < cutoff (weaker guarantee than Wayback)")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("check", help="report whether an archived snapshot exists before the cutoff")
    p.add_argument("url"); p.add_argument("--as-of", required=True, help="YYYY-MM-DD")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("search", help="date-gated DOJ press-release search (cuts releases on/after cutoff)")
    p.add_argument("query"); p.add_argument("--as-of", required=True, help="YYYY-MM-DD")
    p.add_argument("--field", choices=["title", "body"], default="title"); p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("cutoff-for-npi", help="suggest a cutoff from a gold_set defendant's outcome date")
    p.add_argument("npi")
    p.set_defaults(func=cmd_cutoff_for_npi)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
