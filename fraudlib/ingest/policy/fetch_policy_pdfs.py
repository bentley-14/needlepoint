"""Fetch Medicare Program Integrity Manual (Pub 100-08) chapters.

The PIM is the CMS manual that governs Medicare Administrative
Contractor post-payment review, medical-review methodology, and the
Comprehensive Error Rate Testing program. Every chapter codifies how
investigators frame suspicion: what triggers a review, how to construct
a probe sample, what documentation evidences medical necessity, when
a denial is defensible. This is exactly the "novel-fraud" context the
Investigator needs — human-review heuristics, not auto-edits.

Source: CMS IOM landing page
``https://www.cms.gov/regulations-and-guidance/guidance/manuals/internet-only-manuals-ioms-items/cms019033``
links out to 15 chapter PDFs under
``https://www.cms.gov/regulations-and-guidance/guidance/manuals/downloads/pim83cNN.pdf``.

We skip:

* **Chapter 14** — labeled "Reserved for Future Use" (empty).
* **855 attachments + Exhibits** — procedural form-filling guides, not
  investigator reasoning context.

Each chapter is extracted to markdown via ``pymupdf4llm``, split at
section headings by :mod:`fraudlib.ingest.extract.chunking`, and emitted as one
:class:`PolicyRecord` per chunk with ``doc_id`` root ``pim_chNN``.

Run from the repo root:

    /opt/anaconda3/bin/python -m fraudlib.ingest.policy.fetch_policy_pdfs
    /opt/anaconda3/bin/python -m fraudlib.ingest.policy.fetch_policy_pdfs --force
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
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "cms_pim"
CORPUS_PATH = PROJECT_ROOT / "data" / "ground_truth" / "policy_guidelines.jsonl"
MANIFEST_PATH = PROJECT_ROOT / "data" / "ground_truth" / "policy_manifest.json"

_USER_AGENT = "fraudlib-ingest/0.1 (research corpus; healthcare-fraud-detection)"
_HTTP_TIMEOUT_S = 120
_BASE = "https://www.cms.gov/regulations-and-guidance/guidance/manuals/downloads"


@dataclass(frozen=True)
class ChapterSpec:
    """One PIM chapter PDF we want in the corpus."""

    chapter: int
    title: str
    filename: str  # e.g. "pim83c03.pdf"
    min_pages: int = 2  # abort if the downloaded PDF is shorter than this

    @property
    def url(self) -> str:
        return f"{_BASE}/{self.filename}"

    @property
    def doc_id_root(self) -> str:
        return f"pim_ch{self.chapter:02d}"


# Effective for the current PIM edition. CMS updates chapters via
# transmittals; we capture ``fetch_date`` per record so retrieval can
# still surface the version we ingested even after silent updates.
# Chapter 14 is a stub ("Reserved for Future Use") — skip.
PIM_CHAPTERS: list[ChapterSpec] = [
    ChapterSpec(1, "Overview of Medical Review and Program Integrity Programs", "pim83c01.pdf"),
    ChapterSpec(2, "Data Analysis", "pim83c02.pdf"),
    ChapterSpec(3, "Verifying Potential Errors and Taking Corrective Actions", "pim83c03.pdf"),
    ChapterSpec(4, "Program Integrity", "pim83c04.pdf"),
    ChapterSpec(5, "Items and Services Having Special DME Review Considerations", "pim83c05.pdf"),
    ChapterSpec(6, "Medicare Contractor Medical Review Guidelines for Specific Services", "pim83c06.pdf"),
    ChapterSpec(7, "MR Reports", "pim83c07.pdf"),
    ChapterSpec(8, "Administrative Actions and Statistical Sampling for Overpayment Estimates", "pim83c08.pdf"),
    ChapterSpec(9, "The Medicare Fee-for-Service Recovery Audit Program", "pim83c09.pdf"),
    ChapterSpec(10, "Medicare Enrollment", "pim83c10.pdf"),
    ChapterSpec(11, "Fiscal Administration", "pim83c11.pdf"),
    ChapterSpec(12, "The Comprehensive Error Rate Testing Program", "pim83c12.pdf"),
    ChapterSpec(13, "Local Coverage Determinations", "pim83c13.pdf"),
    ChapterSpec(15, "Medicare Enrollment (current)", "pim83c15.pdf"),
]

# If any chapter produces fewer chunks than this the extraction is
# almost certainly broken (blank PDF, wrong file, scanned image).
_MIN_CHUNKS_PER_CHAPTER = 3


def _raw_pdf_path(spec: ChapterSpec) -> Path:
    return RAW_DIR / spec.filename


def _download_pdf(spec: ChapterSpec, *, force: bool = False) -> Path:
    path = _raw_pdf_path(spec)
    if path.exists() and not force and path.stat().st_size > 10_000:
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


def _chapter_to_chunks(spec: ChapterSpec, pdf_path: Path) -> list[Chunk]:
    """PDF → markdown → section-aware chunks, with a window fallback."""
    markdown = extract_pdf_to_markdown(pdf_path)
    chunks = split_by_heading(markdown)
    if not chunks:
        # No detectable headings — emit windowed chunks so retrieval
        # can still surface the content. Chapter-level section label.
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


def _append_to_manifest(
    manifest: dict,
    spec: ChapterSpec,
    records: list[PolicyRecord],
    pdf_path: Path,
) -> None:
    bucket = manifest.setdefault("sources", {}).setdefault("pim", [])
    bucket.append({
        "chapter": spec.chapter,
        "title": spec.title,
        "url": spec.url,
        "pdf_local": str(pdf_path.relative_to(PROJECT_ROOT)),
        "pdf_bytes": pdf_path.stat().st_size,
        "pdf_pages": page_count(pdf_path),
        "fetch_date": records[0].fetch_date if records else "",
        "chunks": len(records),
        "doc_id_root": spec.doc_id_root,
        "first_chunk_hash": records[0].content_hash if records else "",
    })


def run(*, force: bool = False, fetch_date: str | None = None) -> dict:
    """Fetch + extract + chunk every PIM chapter; append new records."""
    fetch_date = fetch_date or dt.date.today().isoformat()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    manifest = _load_manifest()
    already = _existing_corpus_hashes()

    summary_per_chapter: list[dict] = []
    all_new_records: list[PolicyRecord] = []

    for spec in PIM_CHAPTERS:
        pdf_path = _download_pdf(spec, force=force)
        pages = page_count(pdf_path)
        if pages < spec.min_pages:
            raise RuntimeError(
                f"PIM chapter {spec.chapter} ({spec.filename}) has "
                f"{pages} pages — expected ≥ {spec.min_pages}. "
                f"Download likely corrupted; inspect {pdf_path}."
            )

        chunks = _chapter_to_chunks(spec, pdf_path)
        if len(chunks) < _MIN_CHUNKS_PER_CHAPTER:
            raise RuntimeError(
                f"PIM chapter {spec.chapter} extraction produced only "
                f"{len(chunks)} chunks — expected ≥ {_MIN_CHUNKS_PER_CHAPTER}. "
                f"Check pymupdf4llm output for {pdf_path}."
            )

        chapter_records = chunks_to_records(
            chunks,
            source="pim",
            title=f"PIM Chapter {spec.chapter}: {spec.title}",
            url=spec.url,
            fetch_date=fetch_date,
            doc_id_root=spec.doc_id_root,
            effective_date="",  # PIM chapters don't carry a single date
            extra={"chapter": spec.chapter, "pub": "100-08"},
        )

        new = [r for r in chapter_records if r.content_hash not in already]
        for r in new:
            already.add(r.content_hash)

        summary_per_chapter.append({
            "chapter": spec.chapter,
            "title": spec.title,
            "pages": pages,
            "chunks": len(chunks),
            "new_records": len(new),
            "dupe_records": len(chapter_records) - len(new),
        })

        if new:
            all_new_records.extend(new)
            # Update manifest with the chapter summary (one manifest
            # entry per fetch-run — duplicates are fine; the audit
            # script collapses by (doc_id_root, fetch_date)).
            _append_to_manifest(manifest, spec, new, pdf_path)

    written = 0
    if all_new_records:
        written = write_jsonl(all_new_records, CORPUS_PATH, append=True)
        _save_manifest(manifest)

    return {
        "status": "ok",
        "source": "pim",
        "fetch_date": fetch_date,
        "chapters": summary_per_chapter,
        "records_written": written,
        "corpus_path": str(CORPUS_PATH),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download PDFs even if cached.",
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
        print(f"fetch_policy_pdfs FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
