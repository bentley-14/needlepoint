"""
Raw archive helpers for the case-corpus rebuild.

Design principles:
- IMMUTABLE: once a file is written, it is never modified. Re-fetches create
  a new file with a new fetch timestamp.
- PROVENANCE: every raw HTML/text file has a sibling .meta.json with
  source URL, fetch timestamp, HTTP status, content hash, and scrape method.
- IDEMPOTENT: the same source URL fetched twice produces identical output
  (modulo fetch timestamp) — safe to re-run scrapers without duplicating.
- SEPARATION: this module knows nothing about parsing, classification,
  or case schema. It only stores bytes from sources.

Layout per source:
    data/raw/{source}/{YYYY}/{slug}.html        # raw HTML
    data/raw/{source}/{YYYY}/{slug}.txt          # text-extracted
    data/raw/{source}/{YYYY}/{slug}.meta.json    # provenance
    data/raw/_index/{source}.jsonl               # append-only index of all fetches

Where {slug} is derived from the source URL deterministically.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Repo root resolution: this file lives at scripts/scrape/raw_archive.py
REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = REPO_ROOT / "data" / "raw"
INDEX_ROOT = RAW_ROOT / "_index"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class FetchMeta:
    """Provenance record for a single raw-archive entry."""

    source: str  # logical source name, e.g. "doj_press", "oig_enforcement"
    url: str  # canonical URL fetched
    fetch_timestamp_utc: str  # ISO 8601 UTC
    http_status: int
    content_length: int
    content_sha256: str
    final_url: str  # after redirects
    scrape_method: str  # "requests" | "webfetch" | "cloudscraper" | etc.
    user_agent: str
    redirect_chain: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # source-specific bonus fields

    def to_dict(self) -> dict:
        return asdict(self)


def url_slug(url: str, max_len: int = 120) -> str:
    """
    Deterministic URL → filename-safe slug.

    Strips scheme + host, lowercases, replaces non-alphanumeric with '-',
    collapses repeats, trims to max_len, appends 8-char hash to disambiguate
    truncated URLs.
    """
    no_scheme = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    no_query = re.sub(r"[?#].*$", "", no_scheme)
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", no_query).strip("-").lower()

    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    if len(cleaned) > max_len - 9:
        cleaned = cleaned[: max_len - 9].rstrip("-")
    return f"{cleaned}-{h}"


def archive_paths(
    source: str, year: int, slug: str
) -> tuple[Path, Path, Path]:
    """Return (html_path, text_path, meta_path) for a given source/year/slug."""
    base = RAW_ROOT / source / f"{year:04d}"
    return (
        base / f"{slug}.html",
        base / f"{slug}.txt",
        base / f"{slug}.meta.json",
    )


def already_archived(source: str, year: int, slug: str) -> bool:
    """Return True iff all three artifacts exist."""
    h, t, m = archive_paths(source, year, slug)
    return h.exists() and t.exists() and m.exists()


def html_to_text(raw_html: str) -> str:
    """
    Strip tags + entities to produce a plain-text version of an HTML page.

    Intentionally simple — no external dependencies. Stage 2 LLM extraction
    can re-parse the raw HTML if it needs structure; this is a quick-look
    text view.
    """
    # Drop script/style content entirely
    cleaned = re.sub(
        r"<(script|style)[^>]*>.*?</\1>",
        " ",
        raw_html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Convert <br>, </p>, </div> to newlines for readability
    cleaned = re.sub(
        r"<\s*(br|/p|/div|/li|/h[1-6])\s*/?\s*>",
        "\n",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Strip remaining tags
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    # Decode entities
    cleaned = html.unescape(cleaned)
    # Collapse whitespace, keep paragraph breaks
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n[ \t]*", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def write_archive(
    source: str,
    url: str,
    raw_html: str,
    http_status: int,
    final_url: str,
    scrape_method: str,
    year: Optional[int] = None,
    redirect_chain: Optional[list[str]] = None,
    extra: Optional[dict] = None,
) -> tuple[Path, Path, Path, FetchMeta]:
    """
    Write raw HTML + text + meta to the archive.

    If year is None, attempts to extract a 4-digit year from the URL path;
    falls back to current UTC year.

    Returns paths to the three written files plus the FetchMeta record.
    Raises FileExistsError if the slug is already archived (immutable).
    """
    if year is None:
        m = re.search(r"/(\d{4})/", url)
        year = int(m.group(1)) if m else datetime.now(timezone.utc).year

    slug = url_slug(url)
    html_path, text_path, meta_path = archive_paths(source, year, slug)

    if already_archived(source, year, slug):
        raise FileExistsError(
            f"Already archived: {source}/{year}/{slug} — refusing to overwrite "
            "(archive is immutable; delete + re-fetch if intentional)"
        )

    html_path.parent.mkdir(parents=True, exist_ok=True)

    raw_bytes = raw_html.encode("utf-8")
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    meta = FetchMeta(
        source=source,
        url=url,
        fetch_timestamp_utc=datetime.now(timezone.utc).isoformat(),
        http_status=http_status,
        content_length=len(raw_bytes),
        content_sha256=sha256,
        final_url=final_url,
        scrape_method=scrape_method,
        user_agent=USER_AGENT,
        redirect_chain=redirect_chain or [],
        extra=extra or {},
    )

    # Write atomically: html → text → meta last (meta presence = success marker)
    html_path.write_bytes(raw_bytes)
    text_path.write_text(html_to_text(raw_html), encoding="utf-8")
    meta_path.write_text(json.dumps(meta.to_dict(), indent=2), encoding="utf-8")

    # Append to source index
    INDEX_ROOT.mkdir(parents=True, exist_ok=True)
    index_path = INDEX_ROOT / f"{source}.jsonl"
    index_entry = {
        "slug": slug,
        "year": year,
        "url": url,
        "fetch_timestamp_utc": meta.fetch_timestamp_utc,
        "http_status": http_status,
        "content_sha256": sha256,
    }
    with index_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(index_entry) + "\n")

    return html_path, text_path, meta_path, meta


def load_index(source: str) -> list[dict]:
    """Load the append-only index for a source. Returns [] if missing."""
    index_path = INDEX_ROOT / f"{source}.jsonl"
    if not index_path.exists():
        return []
    return [json.loads(line) for line in index_path.read_text().splitlines() if line.strip()]


def archived_urls(source: str) -> set[str]:
    """Set of canonical URLs already archived for a source — for skip checks."""
    return {entry["url"] for entry in load_index(source)}


def archive_stats(source: str) -> dict:
    """Quick stats on a source's archive."""
    entries = load_index(source)
    if not entries:
        return {"source": source, "count": 0}
    by_year = {}
    by_status = {}
    for e in entries:
        by_year[e["year"]] = by_year.get(e["year"], 0) + 1
        by_status[e["http_status"]] = by_status.get(e["http_status"], 0) + 1
    return {
        "source": source,
        "count": len(entries),
        "by_year": dict(sorted(by_year.items())),
        "by_status": by_status,
        "first_fetch": min(e["fetch_timestamp_utc"] for e in entries),
        "last_fetch": max(e["fetch_timestamp_utc"] for e in entries),
    }
