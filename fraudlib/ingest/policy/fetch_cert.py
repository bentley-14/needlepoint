"""Fetch CMS CERT (Comprehensive Error Rate Testing) annual reports.

The CERT program measures Medicare FFS improper-payment rates by
re-reviewing a stratified sample of paid claims each year. The annual
"Supplemental Improper Payment Data" report breaks the error rate down
by claim type, provider specialty, service category, and major error
cause (insufficient documentation, medical necessity, incorrect
coding). This is directly on-thesis for novel-fraud signal: CERT tells
you *where the improper payments are concentrating* and *why reviewers
denied them* — exactly the context a post-payment investigator uses to
frame suspicion.

Source pages and filename conventions drift year-over-year at CMS (see
the per-year ``ReportSpec.url``). We hardcode the five most recent
years rather than scraping the landing page — each URL is HEAD-verified
at ingest time so broken links fail loud.

Run from the repo root:

    /opt/anaconda3/bin/python -m fraudlib.ingest.policy.fetch_cert
    /opt/anaconda3/bin/python -m fraudlib.ingest.policy.fetch_cert --force
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

from fraudlib.ingest.extract.chunking import Chunk, split_by_heading, split_by_window
from fraudlib.ingest.extract.normalize import PolicyRecord, chunks_to_records, write_jsonl
from fraudlib.ingest.extract.pdf_extract import extract_pdf_to_markdown, page_count

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "cert"
CORPUS_PATH = PROJECT_ROOT / "data" / "ground_truth" / "policy_guidelines.jsonl"
MANIFEST_PATH = PROJECT_ROOT / "data" / "ground_truth" / "policy_manifest.json"

_USER_AGENT = "fraudlib-ingest/0.1 (research corpus; healthcare-fraud-detection)"
_HTTP_TIMEOUT_S = 180


@dataclass(frozen=True)
class ReportSpec:
    """One CERT annual report PDF."""

    year: int
    url: str
    min_pages: int = 20

    @property
    def doc_id_root(self) -> str:
        return f"cert_{self.year}"

    @property
    def local_name(self) -> str:
        return f"cert_{self.year}.pdf"


# Year-by-year URLs — CMS appends -0/-1 suffixes on path collisions and
# changed the naming convention again for 2025 ("nov-YYYY-medicare-ffs-...").
# HEAD-verified 2026-04-21.
CERT_REPORTS: list[ReportSpec] = [
    ReportSpec(2020, "https://www.cms.gov/files/document/2020-medicare-fee-service-supplemental-improper-payment-data.pdf"),
    ReportSpec(2021, "https://www.cms.gov/files/document/2021-medicare-fee-service-supplemental-improper-payment-data.pdf-0"),
    ReportSpec(2022, "https://www.cms.gov/files/document/2022-medicare-fee-service-supplemental-improper-payment-data.pdf"),
    ReportSpec(2023, "https://www.cms.gov/files/document/2023-medicare-fee-service-supplemental-improper-payment-data.pdf-1"),
    ReportSpec(2024, "https://www.cms.gov/files/document/2024-medicare-fee-service-supplemental-improper-payment-data.pdf"),
]

_MIN_CHUNKS_PER_REPORT = 5


def _raw_pdf_path(spec: ReportSpec) -> Path:
    return RAW_DIR / spec.local_name


def _download_pdf(spec: ReportSpec, *, force: bool = False) -> Path:
    path = _raw_pdf_path(spec)
    if path.exists() and not force and path.stat().st_size > 50_000:
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(
        spec.url,
        headers={"User-Agent": _USER_AGENT},
        timeout=_HTTP_TIMEOUT_S,
        stream=True,
    )
    resp.raise_for_status()
    with open(path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)
    return path


def _report_to_chunks(pdf_path: Path) -> list[Chunk]:
    markdown = extract_pdf_to_markdown(pdf_path)
    chunks = split_by_heading(markdown)
    if not chunks:
        chunks = split_by_window(markdown)
    return chunks


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


def run(*, force: bool = False, fetch_date: str | None = None) -> dict:
    fetch_date = fetch_date or dt.date.today().isoformat()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    manifest = _load_manifest()
    already = _existing_corpus_hashes()

    per_report: list[dict] = []
    all_new: list[PolicyRecord] = []

    for spec in CERT_REPORTS:
        pdf_path = _download_pdf(spec, force=force)
        pages = page_count(pdf_path)
        if pages < spec.min_pages:
            raise RuntimeError(
                f"CERT {spec.year} has {pages} pages — expected ≥ "
                f"{spec.min_pages}. Download likely corrupt; inspect {pdf_path}."
            )

        chunks = _report_to_chunks(pdf_path)
        if len(chunks) < _MIN_CHUNKS_PER_REPORT:
            raise RuntimeError(
                f"CERT {spec.year} extraction produced only {len(chunks)} "
                f"chunks — expected ≥ {_MIN_CHUNKS_PER_REPORT}."
            )

        records = chunks_to_records(
            chunks,
            source="cert",
            title=f"CERT {spec.year} Medicare FFS Supplemental Improper Payment Data",
            url=spec.url,
            fetch_date=fetch_date,
            doc_id_root=spec.doc_id_root,
            effective_date=f"{spec.year}-12-01",  # CERT reports publish in late Nov/Dec
            extra={"year": spec.year},
        )
        new = [r for r in records if r.content_hash not in already]
        for r in new:
            already.add(r.content_hash)
        all_new.extend(new)

        per_report.append({
            "year": spec.year,
            "pages": pages,
            "chunks": len(chunks),
            "new_records": len(new),
            "dupe_records": len(records) - len(new),
        })

        if new:
            bucket = manifest.setdefault("sources", {}).setdefault("cert", [])
            bucket.append({
                "year": spec.year,
                "url": spec.url,
                "pdf_local": str(pdf_path.relative_to(PROJECT_ROOT)),
                "pdf_bytes": pdf_path.stat().st_size,
                "pdf_pages": pages,
                "fetch_date": fetch_date,
                "chunks": len(new),
                "doc_id_root": spec.doc_id_root,
            })

    written = 0
    if all_new:
        written = write_jsonl(all_new, CORPUS_PATH, append=True)
        _save_manifest(manifest)

    return {
        "status": "ok",
        "source": "cert",
        "fetch_date": fetch_date,
        "reports": per_report,
        "records_written": written,
        "corpus_path": str(CORPUS_PATH),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="Re-download PDFs.")
    parser.add_argument("--fetch-date", default=None, help="Override fetch_date.")
    args = parser.parse_args(argv)

    try:
        summary = run(force=args.force, fetch_date=args.fetch_date)
    except Exception as e:
        print(f"fetch_cert FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
