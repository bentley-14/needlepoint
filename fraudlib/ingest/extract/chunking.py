"""Section-aware text splitting for policy documents.

Two strategies:

* :func:`split_by_heading` — preserves document structure using markdown
  or plain-text heading patterns. Used for PIM, CERT, MedPAC (post
  pymupdf4llm → markdown).

* :func:`split_by_window` — fixed-window fallback with overlap when no
  heading structure is detectable (OIG audit-report PDF body text, some
  LCD narrative fields).

The policy corpus budget is tight (k=5 retrieval × ~600 tokens per chunk
≤ 3 KB). We target ~500–800 word chunks (roughly 700–1100 tokens), with
128-token overlap for the window splitter so cross-section sentences
don't fall into a retrieval gap.

Tokens are word-approximated — we don't ship a tokenizer here. The
``words_per_chunk`` parameter is sized to yield ~700–1100 BPE tokens
under typical prose; callers tune if they see retrieval drift.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Heading patterns in priority order:
#   1. Markdown ATX headings (``#`` … ``####``)
#   2. Numbered section headings at line start (``3.2.1 Section Title``)
# A stricter regex on #1 keeps us from false-positiving on in-body ``#``
# characters.
_MD_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$", re.MULTILINE)
_NUMBERED_RE = re.compile(
    r"^(\d+(?:\.\d+){0,3})\s+([A-Z][^\n]{2,120})\s*$", re.MULTILINE
)

_WORD_RE = re.compile(r"\S+")


@dataclass
class Chunk:
    """A single retrievable chunk.

    Attributes
    ----------
    text
        The chunk body.
    section
        Heading path (e.g., ``"2.3.1 Medical Review"``) or
        ``"<window-N>"`` for windowed splits.
    section_number
        Numeric prefix (``"2.3.1"``) if the heading was numbered, else
        empty string.
    """

    text: str
    section: str
    section_number: str


def split_by_heading(
    text: str,
    min_words: int = 80,
    max_words: int = 1200,
) -> list[Chunk]:
    """Split ``text`` at markdown or numbered headings.

    Sections shorter than ``min_words`` are merged into the previous
    chunk; sections longer than ``max_words`` fall through to
    :func:`split_by_window` (keeping the heading as the section label on
    the first sub-chunk).

    Returns an empty list if no heading pattern matches at all — the
    caller should then try :func:`split_by_window` on the whole text.
    """
    headings = _find_headings(text)
    if not headings:
        return []

    chunks: list[Chunk] = []
    for i, (start, number, title) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        body = text[start:end].strip()
        if not body:
            continue

        words = _WORD_RE.findall(body)
        section_label = f"{number} {title}".strip() if number else title

        if len(words) > max_words:
            # Sub-split oversized sections; preserve the heading label on
            # the first sub-chunk only so readers can still locate it.
            sub = split_by_window(body, words_per_chunk=max_words // 2, overlap_words=100)
            for j, s in enumerate(sub):
                chunks.append(Chunk(
                    text=s.text,
                    section=section_label if j == 0 else f"{section_label} (cont.)",
                    section_number=number,
                ))
        elif len(words) < min_words and chunks:
            # Merge into previous chunk — too short to stand alone.
            prev = chunks[-1]
            chunks[-1] = Chunk(
                text=f"{prev.text}\n\n{body}",
                section=prev.section,
                section_number=prev.section_number,
            )
        else:
            chunks.append(Chunk(
                text=body,
                section=section_label,
                section_number=number,
            ))

    return chunks


def split_by_window(
    text: str,
    words_per_chunk: int = 700,
    overlap_words: int = 128,
) -> list[Chunk]:
    """Fixed-window split with overlap. Use when heading structure is absent.

    Overlap bleeds 128 words of the previous window into the next so
    cross-window sentences are retrievable from either side.
    """
    words = _WORD_RE.findall(text)
    if not words:
        return []

    step = max(1, words_per_chunk - overlap_words)
    chunks: list[Chunk] = []
    i = 0
    n = 0
    while i < len(words):
        window = words[i:i + words_per_chunk]
        if not window:
            break
        chunks.append(Chunk(
            text=" ".join(window),
            section=f"<window-{n}>",
            section_number="",
        ))
        i += step
        n += 1
        if i + words_per_chunk >= len(words) and window[-1] != words[-1]:
            # Final tail window — emit everything remaining and stop.
            tail = words[i:]
            if tail:
                chunks.append(Chunk(
                    text=" ".join(tail),
                    section=f"<window-{n}>",
                    section_number="",
                ))
            break
    return chunks


def _find_headings(text: str) -> list[tuple[int, str, str]]:
    """Return a list of (offset, number, title) tuples in document order.

    Prefers markdown headings; falls back to numbered-section headings
    when no markdown is present. Mixed-mode documents just use markdown.
    """
    md = list(_MD_HEADING_RE.finditer(text))
    if md:
        # Heading "number" for markdown = depth as dotted zeros (# → "",
        # ## → "", etc.); we leave it empty and let the title carry it.
        return [(m.start(), "", m.group(2)) for m in md]

    numbered = list(_NUMBERED_RE.finditer(text))
    return [(m.start(), m.group(1), m.group(2)) for m in numbered]
