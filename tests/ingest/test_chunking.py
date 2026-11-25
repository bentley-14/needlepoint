"""Tests for fraudlib.ingest.extract.chunking.

Run from the repo root with:

    /opt/anaconda3/bin/python -m pytest tests/test_chunking.py -v
"""
from __future__ import annotations

from fraudlib.ingest.extract.chunking import (
    Chunk,
    split_by_heading,
    split_by_window,
)


def test_split_by_heading_markdown() -> None:
    body_a = " ".join(["alpha"] * 100)
    body_b = " ".join(["beta"] * 100)
    body_c = " ".join(["gamma"] * 100)
    text = (
        f"# Doc Title\n{body_a}\n\n"
        f"## 1. First Section\n{body_b}\n\n"
        f"## 2. Second Section\n{body_c}\n"
    )
    chunks = split_by_heading(text)
    assert len(chunks) == 3
    sections = [c.section for c in chunks]
    assert any("First Section" in s for s in sections)
    assert any("Second Section" in s for s in sections)


def test_split_by_heading_numbered_sections() -> None:
    body_a = " ".join(["contractors"] * 100)
    body_b = " ".join(["postpayment"] * 100)
    text = (
        f"3.2.1 Medical Review\n{body_a}\n\n"
        f"3.2.2 Post Payment Review\n{body_b}\n"
    )
    chunks = split_by_heading(text)
    assert len(chunks) == 2
    assert chunks[0].section_number == "3.2.1"
    assert chunks[1].section_number == "3.2.2"


def test_split_by_window_respects_size_and_overlap() -> None:
    words = " ".join(f"w{i}" for i in range(2000))
    chunks = split_by_window(words, words_per_chunk=500, overlap_words=50)
    assert len(chunks) >= 3
    for c in chunks:
        assert len(c.text.split()) <= 500
    tail = chunks[0].text.split()[-50:]
    head = chunks[1].text.split()[:50]
    assert tail == head


def test_split_by_window_empty() -> None:
    assert split_by_window("") == []


def test_split_by_heading_returns_empty_when_no_headings() -> None:
    """When no heading pattern matches, return [] — caller falls back to window."""
    text = "plain paragraph with no headings at all " * 200
    chunks = split_by_heading(text)
    assert chunks == []
