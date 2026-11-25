"""Fetch FCA / AKS / Stark statutes from GovInfo USCODE.

Three statutes are the core legal context for FCA opinions in this
project. The text is short (a few KB each), stable year-to-year, and
public-domain (US Government Publishing Office). We fetch each section
as a single chunk — the Counsel / Investigator usually needs the whole
section when matching elements.

Sources (USCODE 2023 edition):

* 31 USC §3729 — False Claims Act, civil liability.
* 42 USC §1320a-7b — Anti-Kickback Statute, criminal penalties.
* 42 USC §1395nn — Stark Law, physician self-referral.

The fetcher is idempotent: raw HTML lands under
``data/raw/statutes/<slug>.html`` and the resulting :class:`PolicyRecord`
only gets appended to ``policy_guidelines.jsonl`` when its content hash
doesn't already appear in ``policy_manifest.json``. Re-running is safe.

Run from the repo root:

    /opt/anaconda3/bin/python -m fraudlib.ingest.policy.fetch_statutes

Use ``--force`` to re-download even if the raw HTML is cached.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

from fraudlib.ingest.extract.html_extract import extract_html_to_markdown
from fraudlib.ingest.extract.normalize import PolicyRecord, to_record, write_jsonl

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "statutes"
CORPUS_PATH = PROJECT_ROOT / "data" / "ground_truth" / "policy_guidelines.jsonl"
MANIFEST_PATH = PROJECT_ROOT / "data" / "ground_truth" / "policy_manifest.json"

_USER_AGENT = "fraudlib-ingest/0.1 (research corpus; healthcare-fraud-detection)"
_HTTP_TIMEOUT_S = 30


@dataclass(frozen=True)
class StatuteSpec:
    """One USCODE section we want in the corpus."""

    doc_id: str           # stable id, e.g. "usc_31_3729"
    short_name: str       # "False Claims Act (31 USC 3729)"
    title_num: int        # USCODE title, e.g. 31
    section: str          # "§3729"
    url: str              # govinfo content URL
    effective_date: str   # ISO yyyy-mm-dd — last substantive amendment we care about


# USCODE 2023 edition URLs (verified 2026-04-21).
STATUTES: list[StatuteSpec] = [
    StatuteSpec(
        doc_id="usc_31_3729",
        short_name="False Claims Act (31 USC 3729)",
        title_num=31,
        section="§3729",
        url=(
            "https://www.govinfo.gov/content/pkg/USCODE-2023-title31/html/"
            "USCODE-2023-title31-subtitleIII-chap37-subchapIII-sec3729.htm"
        ),
        effective_date="2023-01-01",
    ),
    StatuteSpec(
        doc_id="usc_42_1320a_7b",
        short_name="Anti-Kickback Statute (42 USC 1320a-7b)",
        title_num=42,
        section="§1320a-7b",
        url=(
            "https://www.govinfo.gov/content/pkg/USCODE-2023-title42/html/"
            "USCODE-2023-title42-chap7-subchapXI-partA-sec1320a-7b.htm"
        ),
        effective_date="2023-01-01",
    ),
    StatuteSpec(
        doc_id="usc_42_1395nn",
        short_name="Stark Law (42 USC 1395nn)",
        title_num=42,
        section="§1395nn",
        url=(
            "https://www.govinfo.gov/content/pkg/USCODE-2023-title42/html/"
            "USCODE-2023-title42-chap7-subchapXVIII-partE-sec1395nn.htm"
        ),
        effective_date="2023-01-01",
    ),
]


def _raw_html_path(spec: StatuteSpec) -> Path:
    return RAW_DIR / f"{spec.doc_id}.html"


def _download(spec: StatuteSpec, *, force: bool = False) -> str:
    """Return the raw HTML for ``spec``, caching under ``data/raw/statutes/``."""
    path = _raw_html_path(spec)
    if path.exists() and not force:
        return path.read_text(encoding="utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(
        spec.url,
        headers={"User-Agent": _USER_AGENT},
        timeout=_HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    html = resp.text
    path.write_text(html, encoding="utf-8")
    return html


def _extract_record(spec: StatuteSpec, html: str, *, fetch_date: str) -> PolicyRecord:
    """HTML → markdown → one PolicyRecord. One chunk per statute (short)."""
    extract = extract_html_to_markdown(html, url=spec.url)
    text = extract.text.strip()
    if not text:
        # Fallback: strip tags naively; trafilatura occasionally returns
        # empty on heavily-styled GovInfo pages.
        from html import unescape
        import re as _re
        text = _re.sub(r"<[^>]+>", " ", html)
        text = unescape(text)
        text = _re.sub(r"\s+", " ", text).strip()
    if not text:
        raise RuntimeError(f"could not extract statute text from {spec.url}")

    return to_record(
        source="statute",
        title=spec.short_name,
        section=spec.section,
        text=text,
        url=spec.url,
        doc_id=spec.doc_id,
        effective_date=spec.effective_date,
        fetch_date=fetch_date,
        extra={
            "uscode_title": spec.title_num,
            "uscode_edition": "2023",
        },
    )


def _load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"version": 1, "sources": {}}
    try:
        return json.loads(MANIFEST_PATH.read_text())
    except json.JSONDecodeError:
        return {"version": 1, "sources": {}}


def _save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")


def _existing_hashes_for_source(manifest: dict, source: str) -> set[str]:
    entries = manifest.get("sources", {}).get(source, [])
    return {e.get("content_hash", "") for e in entries if e.get("content_hash")}


def _append_to_manifest(manifest: dict, source: str, records: list[PolicyRecord]) -> None:
    bucket = manifest.setdefault("sources", {}).setdefault(source, [])
    for r in records:
        bucket.append({
            "doc_id": r.doc_id,
            "url": r.url,
            "fetch_date": r.fetch_date,
            "content_hash": r.content_hash,
            "effective_date": r.effective_date,
            "chunks": 1,
        })


def _existing_corpus_hashes() -> set[str]:
    """Every content_hash already in policy_guidelines.jsonl."""
    if not CORPUS_PATH.exists():
        return set()
    hashes: set[str] = set()
    for line in CORPUS_PATH.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        h = rec.get("content_hash") or ""
        if h:
            hashes.add(h)
    return hashes


def run(*, force: bool = False, fetch_date: str | None = None) -> dict:
    """Fetch all three statutes, append new records to the corpus.

    Returns a summary dict for the CLI / tests.
    """
    fetch_date = fetch_date or dt.date.today().isoformat()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    manifest = _load_manifest()
    already = _existing_corpus_hashes()

    new_records: list[PolicyRecord] = []
    per_doc: list[dict] = []
    for spec in STATUTES:
        html = _download(spec, force=force)
        rec = _extract_record(spec, html, fetch_date=fetch_date)
        per_doc.append({
            "doc_id": rec.doc_id,
            "bytes": len(rec.text),
            "content_hash": rec.content_hash,
            "new": rec.content_hash not in already,
        })
        if rec.content_hash not in already:
            new_records.append(rec)
            already.add(rec.content_hash)

    if len(new_records) < 3 and not any(d["new"] for d in per_doc):
        # All three already present → no-op rerun is fine.
        pass

    # Fail loud if extraction is degenerate — catches silent upstream breakage.
    for d in per_doc:
        if d["bytes"] < 500:
            raise RuntimeError(
                f"statute extraction degraded: {d['doc_id']} produced only "
                f"{d['bytes']} bytes — check raw HTML at {_raw_html_path(STATUTES[0]).parent}"
            )

    written = 0
    if new_records:
        written = write_jsonl(new_records, CORPUS_PATH, append=True)
        _append_to_manifest(manifest, "statute", new_records)
        _save_manifest(manifest)

    return {
        "status": "ok",
        "fetch_date": fetch_date,
        "source": "statute",
        "docs": per_doc,
        "records_written": written,
        "corpus_path": str(CORPUS_PATH),
        "manifest_path": str(MANIFEST_PATH),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download raw HTML even if cached.",
    )
    parser.add_argument(
        "--fetch-date",
        default=None,
        help="Override fetch_date (ISO yyyy-mm-dd). Default: today.",
    )
    args = parser.parse_args(argv)

    try:
        summary = run(force=args.force, fetch_date=args.fetch_date)
    except Exception as e:
        print(f"fetch_statutes FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
