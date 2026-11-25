#!/usr/bin/env python3
"""BM25 retrieval over the FCA doctrine-card corpus (``docs/legal_corpus/``).

Doctrine retrieval for pleading time. Escobar/Schutte-class
doctrine currently lands LATE in a case run (adversarial rounds) and shaves
25-30% discounts or kills theories at iter-3, purely because counsel_build and
defense have no pleading-time surface for the full doctrine narrative. This
tool gives them one — full-text retrieval over long-form doctrine cards, not
just a structured defense-mode/circuit lookup.

This is DELIBERATELY separate from ``scripts/precedent_lookup.py`` (the
existing R2-hosted, defense-mode x circuit structured JSONL corpus used as
counsel_build's only scoped Bash tool). The two are complementary, not
competing:
  - ``precedent_lookup.py``  -> terse, structured, keyed by defense_mode x
    circuit; the runtime-scoped tool already wired into 5 agent prompts.
  - ``doctrine_lookup.py``   -> long-form narrative cards (holding, our
    application rule, common rebuttals) retrieved by free-text BM25 query;
    meant for reading full doctrine context BEFORE drafting a count or an
    attack, not for the terse per-defense-mode lookup precedent_lookup already
    serves.

Retrieval: tokenize (lowercase, strip punctuation, drop stopwords), rank with
``rank_bm25.BM25Okapi`` when available, else fall back to sklearn TF-IDF +
cosine similarity. No network calls; corpus is read straight off disk.

Usage:
    python scripts/doctrine_lookup.py --query "government kept paying claims for years"
    python scripts/doctrine_lookup.py --doctrine escobar_materiality
    python scripts/doctrine_lookup.py --query "prior qui tam pending" --format json
    python scripts/doctrine_lookup.py --list
"""
from __future__ import annotations

import argparse
import json
import re
import string
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
CORPUS_DIR = REPO / "docs" / "legal_corpus"

# Frontmatter fields we parse. ``elements`` and ``common_rebuttals`` are lists
# (YAML block-sequence style, one ``- "..."`` per line); everything else is a
# scalar. This is a hand-rolled parser (not PyYAML) so this tool adds no new
# required dependency — the card frontmatter schema is small and fixed.
_LIST_FIELDS = frozenset({"elements", "common_rebuttals"})
_SCALAR_FIELDS = frozenset({
    "doctrine", "citation", "court_year", "our_application_rule",
    "verification_status",
})

_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "but",
    "by", "for", "from", "had", "has", "have", "he", "her", "him", "his",
    "i", "in", "into", "is", "it", "its", "itself", "me", "my", "no",
    "nor", "not", "of", "off", "on", "once", "only", "or", "other", "our",
    "out", "over", "own", "same", "she", "so", "some", "such", "than",
    "that", "the", "their", "them", "there", "these", "they", "this",
    "those", "through", "to", "too", "under", "up", "very", "was", "we",
    "were", "what", "when", "where", "which", "while", "who", "whom",
    "why", "will", "with", "would", "you", "your",
})
_PUNCT_RE = re.compile(rf"[{re.escape(string.punctuation)}]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, whitespace-split, drop stopwords."""
    if not text:
        return []
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    return [t for t in text.split() if t and t not in _STOPWORDS]


def _parse_card(path: Path) -> dict[str, Any]:
    """Parse one doctrine card: YAML-ish frontmatter (between ``---`` markers)
    plus the markdown body. Multi-line scalars (``field: >`` block-folded
    style) and simple ``- "quoted item"`` list entries are supported — the
    only styles actually used in this corpus's frontmatter.
    """
    text = path.read_text()
    if not text.startswith("---"):
        raise ValueError(f"{path}: missing frontmatter delimiter")
    end = text.index("\n---", 3)
    fm_text = text[3:end]
    body = text[end + 4:].lstrip("\n")

    meta: dict[str, Any] = {}
    lines = fm_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = re.match(r"^(\w+):\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2).strip()

        if key in _LIST_FIELDS:
            items: list[str] = []
            i += 1
            while i < len(lines) and re.match(r"^\s*-\s", lines[i]):
                item = re.sub(r"^\s*-\s*", "", lines[i]).strip()
                item = item.strip('"')
                items.append(item)
                i += 1
            meta[key] = items
            continue

        if rest == ">":
            # Block-folded scalar: consecutive indented lines join with " "
            # until a blank line or a new top-level ``key:`` line.
            folded: list[str] = []
            i += 1
            while i < len(lines) and (lines[i].startswith("  ") or not lines[i].strip()):
                if lines[i].strip():
                    folded.append(lines[i].strip())
                i += 1
            meta[key] = " ".join(folded)
            continue

        # Plain scalar, optionally quoted.
        meta[key] = rest.strip('"')
        i += 1

    slug = path.stem
    return {
        "slug": slug,
        "doctrine": meta.get("doctrine", ""),
        "citation": meta.get("citation", ""),
        "court_year": meta.get("court_year", ""),
        "elements": meta.get("elements", []),
        "our_application_rule": meta.get("our_application_rule", ""),
        "common_rebuttals": meta.get("common_rebuttals", []),
        "verification_status": meta.get("verification_status", ""),
        "body": body,
        "path": str(path.relative_to(REPO)),
    }


def _document_text(card: dict) -> str:
    """Concatenate the fields BM25 should see for this card."""
    parts = [
        card["slug"].replace("_", " "),
        card["doctrine"].replace("_", " "),
        card["citation"],
        card["our_application_rule"],
        " ".join(card["elements"]),
        " ".join(card["common_rebuttals"]),
        card["body"],
    ]
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# DoctrineIndex
# ---------------------------------------------------------------------------

class DoctrineIndex:
    """Lazy BM25/TF-IDF index over the doctrine-card corpus."""

    def __init__(self, corpus_dir: Path = CORPUS_DIR) -> None:
        self.corpus_dir = Path(corpus_dir)
        self._cards: list[dict] = []
        self._tokens: list[list[str]] = []
        self._bm25: Any = None
        self._tfidf_vec: Any = None
        self._tfidf_mat: Any = None
        self._backend: str = ""
        self._load_and_index()

    def _load_and_index(self) -> None:
        if not self.corpus_dir.exists():
            return
        cards = []
        for p in sorted(self.corpus_dir.glob("*.md")):
            try:
                cards.append(_parse_card(p))
            except ValueError:
                continue
        self._cards = cards
        self._tokens = [_tokenize(_document_text(c)) for c in cards]

        try:
            from rank_bm25 import BM25Okapi

            if any(self._tokens):
                self._bm25 = BM25Okapi(self._tokens)
                self._backend = "bm25"
                return
        except ImportError:
            pass

        from sklearn.feature_extraction.text import TfidfVectorizer

        corpus = [" ".join(toks) for toks in self._tokens]
        self._tfidf_vec = TfidfVectorizer(analyzer="whitespace", norm="l2")
        if any(corpus):
            self._tfidf_mat = self._tfidf_vec.fit_transform(corpus)
        self._backend = "tfidf"

    def _score_all(self, query_tokens: list[str]) -> list[float]:
        if not self._cards:
            return []
        if self._backend == "bm25" and self._bm25 is not None:
            return [float(s) for s in self._bm25.get_scores(query_tokens)]
        if self._backend == "tfidf" and self._tfidf_mat is not None:
            from sklearn.metrics.pairwise import cosine_similarity

            q = self._tfidf_vec.transform([" ".join(query_tokens)])
            sims = cosine_similarity(q, self._tfidf_mat).ravel()
            return [float(s) for s in sims]
        return [0.0] * len(self._cards)

    def search(self, query: str, k: int = 5) -> list[dict]:
        """Return the top-``k`` cards ranked by BM25 (or TF-IDF cosine).

        Empty query returns the first ``k`` cards in corpus (alphabetical
        by slug) with score 0.0.
        """
        query_tokens = _tokenize((query or "").strip())
        if not query_tokens:
            scored = [(i, 0.0) for i in range(len(self._cards))]
        else:
            all_scores = self._score_all(query_tokens)
            scored = sorted(
                enumerate(all_scores), key=lambda t: t[1], reverse=True
            )
        out = []
        for i, score in scored[:k]:
            c = self._cards[i]
            out.append(_card_result(c, score))
        return out

    def get(self, slug: str) -> dict | None:
        """Exact-slug lookup (``--doctrine <slug>``)."""
        for c in self._cards:
            if c["slug"] == slug:
                return _card_result(c, None)
        return None

    def list_slugs(self) -> list[str]:
        return [c["slug"] for c in self._cards]

    @property
    def backend(self) -> str:
        return self._backend

    def __len__(self) -> int:
        return len(self._cards)


def _card_result(c: dict, score: float | None) -> dict:
    result = {
        "slug": c["slug"],
        "doctrine": c["doctrine"],
        "citation": c["citation"],
        "court_year": c["court_year"],
        "elements": c["elements"],
        "our_application_rule": c["our_application_rule"],
        "common_rebuttals": c["common_rebuttals"],
        "verification_status": c["verification_status"],
        "body": c["body"],
        "path": c["path"],
    }
    if score is not None:
        result["score"] = score
    return result


# ---------------------------------------------------------------------------
# Module-level cached singleton (mirrors case_search.py)
# ---------------------------------------------------------------------------

_DEFAULT_INDEX: DoctrineIndex | None = None


def get_default_index(corpus_dir: Path = CORPUS_DIR) -> DoctrineIndex:
    global _DEFAULT_INDEX
    if _DEFAULT_INDEX is None or _DEFAULT_INDEX.corpus_dir != Path(corpus_dir):
        _DEFAULT_INDEX = DoctrineIndex(corpus_dir)
    return _DEFAULT_INDEX


def reset_default_index() -> None:
    global _DEFAULT_INDEX
    _DEFAULT_INDEX = None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_text(cards: list[dict]) -> None:
    if not cards:
        print("(no matching doctrine cards — try --list or a broader --query)")
        return
    for c in cards:
        score_str = f" | score={c['score']:.3f}" if "score" in c else ""
        print(f"\n### {c['slug']} — {c['citation']}")
        print(f"  doctrine={c['doctrine']} | court_year={c['court_year']} | "
              f"status={c['verification_status']}{score_str}")
        if c["elements"]:
            print("  ELEMENTS:")
            for e in c["elements"]:
                print(f"    - {e}")
        if c["our_application_rule"]:
            print(f"  OUR APPLICATION: {c['our_application_rule']}")
        if c["common_rebuttals"]:
            print("  COMMON REBUTTALS:")
            for r in c["common_rebuttals"]:
                print(f"    - {r}")
    print(f"\n[{len(cards)} card{'s' if len(cards) != 1 else ''}] — "
          f"cite from ``our_application_rule``/``elements``; re-verify any quote "
          f"against the source opinion before filing.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--query", help="free-text issue/fact pattern to retrieve doctrine for")
    ap.add_argument("--doctrine", help="exact card slug, e.g. escobar_materiality")
    ap.add_argument("-k", "--top-k", type=int, default=5, help="max cards to return (default 5)")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--list", action="store_true", help="list all card slugs in the corpus")
    ap.add_argument("--corpus-dir", default=str(CORPUS_DIR), help="override corpus directory (tests)")
    args = ap.parse_args(argv)

    index = get_default_index(Path(args.corpus_dir))

    if args.list:
        slugs = index.list_slugs()
        if args.format == "json":
            print(json.dumps(slugs, indent=2))
        else:
            for s in slugs:
                print(s)
        return 0

    if args.doctrine:
        card = index.get(args.doctrine)
        cards = [card] if card else []
        if not cards:
            print(f"(no card with slug '{args.doctrine}' — try --list)", file=sys.stderr)
            if args.format == "json":
                print("[]")
            return 1
    else:
        cards = index.search(args.query or "", k=args.top_k)

    if args.format == "json":
        print(json.dumps(cards, indent=2))
    else:
        _print_text(cards)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
