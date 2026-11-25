"""Sequential paragraph numbering for an assembled qui tam complaint.

Real qui tam complaints number paragraphs sequentially across the entire
document (e.g., the Cerebral complaint runs ¶¶ 1–89 across 18 pages with no
per-section reset). This module assigns those numbers to a document that's
been assembled as a flat list of paragraph dicts.

A paragraph dict has two relevant keys:

- ``kind``: ``numbered`` (gets a number), or ``unnumbered``/``subsection``/
  ``prayer`` (does not)
- ``text``: the prose; left untouched

After ``assign_sequential_numbers()``, each numbered paragraph gains a
``number`` key (integer). Unnumbered paragraphs are left as-is.
"""
from __future__ import annotations

from typing import Any

P = dict[str, Any]


def assign_sequential_numbers(paragraphs: list[P], start: int = 1) -> list[P]:
    """Assign sequential numbers to all paragraphs whose ``kind`` is
    ``numbered``. Returns a new list (does not mutate input).

    Subsection headings, unnumbered blocks (preamble, jury demand, place of
    trial), and prayer items are skipped — they don't get numbers.

    >>> ps = [{"text": "intro", "kind": "numbered"},
    ...       {"text": "A. Sub", "kind": "subsection"},
    ...       {"text": "more", "kind": "numbered"}]
    >>> [p.get("number") for p in assign_sequential_numbers(ps)]
    [1, None, 2]
    """
    out: list[P] = []
    n = start
    for p in paragraphs:
        q = dict(p)
        if q.get("kind") == "numbered":
            q["number"] = n
            n += 1
        out.append(q)
    return out


def count_numbered(paragraphs: list[P]) -> int:
    """Count paragraphs that would receive a number."""
    return sum(1 for p in paragraphs if p.get("kind") == "numbered")
