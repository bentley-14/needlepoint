"""Turn per-source extraction outputs into the unified policy-corpus schema.

Every fetcher calls :func:`to_record` (or iterates :func:`chunks_to_records`)
to produce JSONL entries with a stable schema. The schema is frozen in
``data/source/policy/policy_guidelines.schema.json`` — keep them in sync.

A record is one retrievable chunk. Long documents (PIM chapters, MedPAC
reports, OIG audit PDFs) produce many chunks that share ``doc_id_root``
so they can be re-aggregated at citation time.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from fraudlib.ingest.extract.chunking import Chunk

# All allowed ``source`` values — keep in sync with policy_search filters
# and policy_guidelines.schema.json.
ALLOWED_SOURCES = frozenset({
    "pim",            # Medicare Program Integrity Manual (Pub 100-08)
    "oig_workplan",
    "oig_sfa",        # Special Fraud Alerts
    "oig_ao",         # Advisory Opinions
    "oig_oas",        # Office of Audit Services reports
    "cert",           # Comprehensive Error Rate Testing
    "lcd",            # Local Coverage Determinations
    "medpac",
    "statute",        # FCA / AKS / Stark
})

_WHITESPACE_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class PolicyRecord:
    """One retrievable chunk.

    Fields match ``policy_guidelines.schema.json`` exactly. The schema
    is the contract; this dataclass enforces field presence but leaves
    values unvalidated (fetcher is responsible for filling sensibly).
    """

    doc_id: str
    source: str
    title: str
    section: str
    text: str
    url: str
    effective_date: str   # ISO yyyy-mm-dd or empty
    fetch_date: str       # ISO yyyy-mm-dd
    content_hash: str     # sha256 hex of ``text``, for silent-update detection
    applies_to_specialties: list[str]
    applies_to_codes: list[str]
    extra: dict           # free-form metadata (OIG category, LCD MACs, etc.)


def to_record(
    *,
    source: str,
    title: str,
    section: str,
    text: str,
    url: str,
    doc_id: str | None = None,
    effective_date: str = "",
    fetch_date: str,
    applies_to_specialties: list[str] | None = None,
    applies_to_codes: list[str] | None = None,
    extra: dict | None = None,
) -> PolicyRecord:
    """Build a :class:`PolicyRecord`, filling computed fields.

    Required: ``source``, ``title``, ``section``, ``text``, ``url``,
    ``fetch_date``. ``doc_id`` is derived from source/title/section when
    absent. All list/dict fields default to empty.
    """
    if source not in ALLOWED_SOURCES:
        raise ValueError(
            f"source={source!r} is not in ALLOWED_SOURCES. "
            f"Add it to ALLOWED_SOURCES (and the schema) or fix the fetcher."
        )
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        raise ValueError("Cannot record an empty text chunk.")

    if doc_id is None:
        doc_id = _make_doc_id(source, title, section)

    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    return PolicyRecord(
        doc_id=doc_id,
        source=source,
        title=title,
        section=section,
        text=text,
        url=url,
        effective_date=effective_date,
        fetch_date=fetch_date,
        content_hash=content_hash,
        applies_to_specialties=list(applies_to_specialties or []),
        applies_to_codes=list(applies_to_codes or []),
        extra=dict(extra or {}),
    )


def chunks_to_records(
    chunks: Iterable[Chunk],
    *,
    source: str,
    title: str,
    url: str,
    fetch_date: str,
    doc_id_root: str,
    effective_date: str = "",
    applies_to_specialties: list[str] | None = None,
    applies_to_codes: list[str] | None = None,
    extra: dict | None = None,
) -> list[PolicyRecord]:
    """Yield one record per chunk, with doc_id = ``<root>_<chunk_idx>``."""
    out: list[PolicyRecord] = []
    for i, ch in enumerate(chunks):
        if not ch.text.strip():
            continue
        out.append(to_record(
            source=source,
            title=title,
            section=ch.section,
            text=ch.text,
            url=url,
            doc_id=f"{doc_id_root}_{i:04d}",
            effective_date=effective_date,
            fetch_date=fetch_date,
            applies_to_specialties=applies_to_specialties,
            applies_to_codes=applies_to_codes,
            extra=extra,
        ))
    return out


def write_jsonl(records: Iterable[PolicyRecord], path: Path, append: bool = True) -> int:
    """Append records to ``path`` as JSONL. Returns the number written.

    If ``append`` is False the file is truncated first — useful for
    full rebuilds during development.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    n = 0
    with open(path, mode, encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
            n += 1
    return n


def _make_doc_id(source: str, title: str, section: str) -> str:
    """Produce a stable, filesystem-safe doc_id from source + title + section."""
    slug = f"{source}__{title}__{section}".lower()
    slug = _SLUG_RE.sub("-", slug).strip("-")
    return slug[:200]  # cap for filesystem sanity
