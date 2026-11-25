"""Fetch the HHS OIG Work Plan — current + historical items (CMS-filtered).

One fetcher, one canonical data source: the OIG publishes a CSV export
at ``/workplan/export-workplan/`` containing every active + completed
Work Plan project (and series) with title, narrative, status, agencies,
component (OAS = Office of Audit Services, OEI = Office of Evaluation &
Inspections), dates, and issue-area tags.

This is the single most valuable signal source for an Investigator —
OIG telegraphs its enforcement priorities here. The narrative fields
describe *what OIG is actively reviewing or has reviewed*, which is
exactly the "pattern under suspicion" context a post-payment investigator
needs.

We filter to CMS / Medicare / Medicaid items (the rest are NIH, HRSA,
CDC, ACF grants — low fraud signal for our use case). We respect
robots.txt: only the CSV export is fetched, no archive-page scraping.

Run from the repo root:

    /opt/anaconda3/bin/python -m fraudlib.ingest.policy.fetch_oig
    /opt/anaconda3/bin/python -m fraudlib.ingest.policy.fetch_oig --force
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from html import unescape
from pathlib import Path

import requests

from fraudlib.ingest.extract.normalize import PolicyRecord, to_record, write_jsonl

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "oig"
CSV_PATH = RAW_DIR / "workplan.csv"
CORPUS_PATH = PROJECT_ROOT / "data" / "ground_truth" / "policy_guidelines.jsonl"
MANIFEST_PATH = PROJECT_ROOT / "data" / "ground_truth" / "policy_manifest.json"

CSV_EXPORT_URL = "https://oig.hhs.gov/workplan/export-workplan/"
WORKPLAN_LANDING = (
    "https://oig.hhs.gov/reports/work-plan/browse-work-plan-projects/"
)
_USER_AGENT = "fraudlib-ingest/0.1 (research corpus; healthcare-fraud-detection)"
_HTTP_TIMEOUT_S = 60

# Agencies whose Work Plan items we keep. All strings matched
# case-insensitively against the ``Agencies`` field (pipe-separated in
# the raw CSV). CMS covers Medicare, Medicaid, DMEPOS, Part D — the
# core of our fraud-detection surface.
_CMS_AGENCY_TOKENS = frozenset({"cms", "medicare", "medicaid"})

# Minimum expected rows after filtering — abort if the export is
# mysteriously short. ~250 active + completed CMS items today; 100 is
# a defensive floor that still surfaces a corpus change.
_MIN_ROWS = 100

_TAG_SEP_RE = re.compile(r"[|,]")
_TAG_WS_RE = re.compile(r"\s+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class _Row:
    number: str
    title: str
    type_: str
    series_number: str
    series_title: str
    narrative: str
    report_number: str
    component: str
    agencies: str
    status: str
    announced_date: str
    financial_tags: str
    issue_tags: str
    target_tags: str


def _download_csv(*, force: bool = False) -> str:
    """Return the raw CSV text, caching under ``data/raw/oig/``.

    The OIG export is UTF-8 with leading BOM(s), but its HTTP response
    doesn't advertise a charset — requests would fall back to
    ISO-8859-1 and mojibake the file. Decode the raw bytes with
    ``utf-8-sig`` ourselves to strip any leading BOMs cleanly.
    """
    if CSV_PATH.exists() and not force:
        return CSV_PATH.read_text(encoding="utf-8-sig")

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(
        CSV_EXPORT_URL,
        headers={"User-Agent": _USER_AGENT},
        timeout=_HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    text = resp.content.decode("utf-8-sig")
    CSV_PATH.write_text(text, encoding="utf-8")
    return text


def _iter_rows(csv_text: str) -> list[_Row]:
    """Parse the OIG CSV export; the header has leading BOM(s) to strip."""
    # The OIG export ships with one or two leading BOM characters on the
    # first header cell — csv.DictReader doesn't strip them by default.
    # Normalize by hand so downstream key lookups work.
    reader = csv.reader(csv_text.splitlines())
    header = [h.lstrip("\ufeff").strip() for h in next(reader)]

    def idx(name: str) -> int:
        try:
            return header.index(name)
        except ValueError as e:
            raise RuntimeError(f"OIG CSV missing column {name!r}; header={header}") from e

    cols = {c: idx(c) for c in (
        "Number", "Title", "Type", "Series Number", "Series Title",
        "Narrative", "Report Number", "Component", "Agencies",
        "Status", "Announced Date", "Financial Group Tags",
        "Issue Area Tags", "Target Group Tags",
    )}

    rows: list[_Row] = []
    for raw in reader:
        if not raw:
            continue
        # csv.reader preserves short rows; pad with empty strings so we
        # can index uniformly.
        padded = raw + [""] * (len(header) - len(raw))
        rows.append(_Row(
            number=padded[cols["Number"]].strip(),
            title=padded[cols["Title"]].strip(),
            type_=padded[cols["Type"]].strip(),
            series_number=padded[cols["Series Number"]].strip(),
            series_title=padded[cols["Series Title"]].strip(),
            narrative=padded[cols["Narrative"]],
            report_number=padded[cols["Report Number"]].strip(),
            component=padded[cols["Component"]].strip(),
            agencies=padded[cols["Agencies"]].strip(),
            status=padded[cols["Status"]].strip(),
            announced_date=padded[cols["Announced Date"]].strip(),
            financial_tags=padded[cols["Financial Group Tags"]].strip(),
            issue_tags=padded[cols["Issue Area Tags"]].strip(),
            target_tags=padded[cols["Target Group Tags"]].strip(),
        ))
    return rows


def _is_cms_row(row: _Row) -> bool:
    tokens = {t.lower().strip() for t in _TAG_SEP_RE.split(row.agencies) if t.strip()}
    return bool(tokens & _CMS_AGENCY_TOKENS)


def _strip_html(text: str) -> str:
    """Narrative fields come as escaped HTML (``<p data-block-key=...>``).

    Strip tags, unescape entities, collapse whitespace.
    """
    if not text:
        return ""
    stripped = _HTML_TAG_RE.sub(" ", text)
    return _TAG_WS_RE.sub(" ", unescape(stripped)).strip()


def _parse_date(raw: str) -> str:
    """Normalize ``Announced Date`` → ``yyyy-mm-dd``.

    OIG exports as ``yyyy-mm-dd`` already; we defensively accept empty.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    # Accept the export's native format only; never guess.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    return ""  # unknown format → leave empty rather than misdate a record


def _parse_tags(raw: str) -> list[str]:
    """Split pipe-or-comma-separated tag string into a clean list."""
    if not raw:
        return []
    parts = [p.strip() for p in _TAG_SEP_RE.split(raw)]
    return [p for p in parts if p]


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:120]


def _doc_id(row: _Row) -> str:
    """Stable per-row id. Uses the OIG project Number when present."""
    if row.number:
        return f"oig_wp_{_slug(row.number)}"
    return f"oig_wp_{_slug(row.title)}"


def _detail_url(row: _Row) -> str:
    """Best-effort URL for a single Work Plan item.

    Active items live at ``.../browse-work-plan-projects/{title-slug}/``.
    For completed items the detail page is gone after archival; we fall
    back to the landing page (always valid) and record the suspected
    slug in ``extra.detail_slug`` for later re-lookup.
    """
    return WORKPLAN_LANDING


def _row_to_record(row: _Row, *, fetch_date: str) -> PolicyRecord | None:
    """Build one PolicyRecord from a Work Plan row. Returns None if unusable."""
    narrative = _strip_html(row.narrative)
    if len(narrative) < 40:
        # A narrative this short usually means a placeholder row or a
        # parent-series entry with no project-specific body.
        return None

    title = row.title or row.series_title or row.number
    if not title:
        return None

    # Compose a short section label — Component + Status gives the
    # agent useful routing cues ("OEI active study" vs "OAS completed
    # audit"). Empty component falls back to the OIG general bucket.
    component = row.component or "OIG"
    section = f"{component} / {row.status or 'UNKNOWN'}"

    applies_to_specialties = _parse_tags(row.target_tags)

    extra: dict = {
        "component": row.component,
        "status": row.status,
        "agencies": _parse_tags(row.agencies),
        "issue_area_tags": _parse_tags(row.issue_tags),
        "financial_group_tags": _parse_tags(row.financial_tags),
        "type": row.type_,
        "project_number": row.number,
        "series_number": row.series_number,
        "report_number": row.report_number,
        "detail_slug": _slug(title),
    }

    return to_record(
        source="oig_workplan",
        title=title,
        section=section,
        text=narrative,
        url=_detail_url(row),
        doc_id=_doc_id(row),
        effective_date=_parse_date(row.announced_date),
        fetch_date=fetch_date,
        applies_to_specialties=applies_to_specialties,
        applies_to_codes=[],
        extra=extra,
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


def _existing_corpus_hashes() -> set[str]:
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


def _append_to_manifest(manifest: dict, records: list[PolicyRecord]) -> None:
    bucket = manifest.setdefault("sources", {}).setdefault("oig_workplan", [])
    for r in records:
        bucket.append({
            "doc_id": r.doc_id,
            "url": r.url,
            "fetch_date": r.fetch_date,
            "content_hash": r.content_hash,
            "effective_date": r.effective_date,
            "chunks": 1,
            "status": r.extra.get("status"),
        })


def run(*, force: bool = False, fetch_date: str | None = None) -> dict:
    """Fetch the OIG Work Plan CSV, filter to CMS items, append new records."""
    fetch_date = fetch_date or dt.date.today().isoformat()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    csv_text = _download_csv(force=force)
    rows = _iter_rows(csv_text)
    cms_rows = [r for r in rows if _is_cms_row(r)]

    if len(cms_rows) < _MIN_ROWS:
        raise RuntimeError(
            f"OIG Work Plan filter produced {len(cms_rows)} CMS rows "
            f"(< minimum {_MIN_ROWS}). Export URL or filter likely "
            f"changed — inspect {CSV_PATH}."
        )

    manifest = _load_manifest()
    already = _existing_corpus_hashes()

    new_records: list[PolicyRecord] = []
    skipped_short = 0
    skipped_dupe = 0
    for r in cms_rows:
        rec = _row_to_record(r, fetch_date=fetch_date)
        if rec is None:
            skipped_short += 1
            continue
        if rec.content_hash in already:
            skipped_dupe += 1
            continue
        new_records.append(rec)
        already.add(rec.content_hash)

    written = 0
    if new_records:
        written = write_jsonl(new_records, CORPUS_PATH, append=True)
        _append_to_manifest(manifest, new_records)
        _save_manifest(manifest)

    return {
        "status": "ok",
        "source": "oig_workplan",
        "fetch_date": fetch_date,
        "rows_total": len(rows),
        "rows_cms": len(cms_rows),
        "records_written": written,
        "skipped_short_narrative": skipped_short,
        "skipped_duplicate": skipped_dupe,
        "corpus_path": str(CORPUS_PATH),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download the CSV even if cached.",
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
        print(f"fetch_oig FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
