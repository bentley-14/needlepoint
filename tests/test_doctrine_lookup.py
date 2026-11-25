"""Tests for scripts/doctrine_lookup.py — the FCA doctrine-card BM25 retriever.

Hermetic — no network. Runs against the real docs/legal_corpus/ corpus so a
future card that breaks frontmatter parsing (or a query that stops resolving
to the right card) fails CI, not just a manual smoke test.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORPUS_DIR = REPO / "docs" / "legal_corpus"
sys.path.insert(0, str(REPO / "scripts"))

import doctrine_lookup as dl  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Corpus parses
# ─────────────────────────────────────────────────────────────────────────────

def test_corpus_dir_exists_and_nonempty():
    assert CORPUS_DIR.exists()
    cards = list(CORPUS_DIR.glob("*.md"))
    assert len(cards) >= 12, f"expected >=12 doctrine cards, found {len(cards)}"


def test_every_card_parses_with_required_frontmatter():
    for path in sorted(CORPUS_DIR.glob("*.md")):
        card = dl._parse_card(path)
        assert card["slug"], path
        assert card["doctrine"], f"{path} missing doctrine field"
        assert card["citation"], f"{path} missing citation field"
        assert card["court_year"], f"{path} missing court_year field"
        assert card["our_application_rule"], f"{path} missing our_application_rule field"
        assert isinstance(card["elements"], list) and card["elements"], (
            f"{path} missing/empty elements list"
        )
        assert isinstance(card["common_rebuttals"], list) and card["common_rebuttals"], (
            f"{path} missing/empty common_rebuttals list"
        )
        assert card["body"].strip(), f"{path} has empty body"


def test_index_loads_all_cards():
    dl.reset_default_index()
    index = dl.DoctrineIndex(CORPUS_DIR)
    assert len(index) == len(list(CORPUS_DIR.glob("*.md")))
    assert index.backend in ("bm25", "tfidf")


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval quality — the two examples named in the Wave-2 spec
# ─────────────────────────────────────────────────────────────────────────────

def test_query_continued_payment_returns_escobar():
    index = dl.DoctrineIndex(CORPUS_DIR)
    results = index.search("government kept paying claims for years", k=3)
    assert results, "expected at least one hit"
    top_slugs = [r["slug"] for r in results]
    assert "escobar_materiality" in top_slugs
    # Escobar should be the single best match for this exact continued-payment
    # fact pattern, not merely present somewhere in the top-k.
    assert results[0]["slug"] == "escobar_materiality"


def test_query_prior_qui_tam_pending_returns_first_to_file():
    index = dl.DoctrineIndex(CORPUS_DIR)
    results = index.search("prior qui tam pending", k=3)
    assert results, "expected at least one hit"
    top_slugs = [r["slug"] for r in results]
    assert "first_to_file_kbr_carter" in top_slugs
    assert results[0]["slug"] == "first_to_file_kbr_carter"


def test_exact_slug_lookup():
    index = dl.DoctrineIndex(CORPUS_DIR)
    card = index.get("escobar_materiality")
    assert card is not None
    assert card["slug"] == "escobar_materiality"
    assert "Escobar" in card["citation"]


def test_exact_slug_lookup_miss_returns_none():
    index = dl.DoctrineIndex(CORPUS_DIR)
    assert index.get("does_not_exist_slug") is None


def test_list_slugs_matches_corpus_files():
    index = dl.DoctrineIndex(CORPUS_DIR)
    slugs = set(index.list_slugs())
    expected = {p.stem for p in CORPUS_DIR.glob("*.md")}
    assert slugs == expected


def test_empty_query_returns_k_cards_without_error():
    index = dl.DoctrineIndex(CORPUS_DIR)
    results = index.search("", k=4)
    assert len(results) == 4


# ─────────────────────────────────────────────────────────────────────────────
# JSON shape stability (agent-consumed contract)
# ─────────────────────────────────────────────────────────────────────────────

_EXPECTED_KEYS = {
    "slug", "doctrine", "citation", "court_year", "elements",
    "our_application_rule", "common_rebuttals", "verification_status",
    "body", "path",
}


def test_search_result_json_shape():
    index = dl.DoctrineIndex(CORPUS_DIR)
    results = index.search("materiality", k=2)
    for r in results:
        assert _EXPECTED_KEYS.issubset(r.keys())
        assert "score" in r
        assert isinstance(r["score"], float)
        assert isinstance(r["elements"], list)
        assert isinstance(r["common_rebuttals"], list)


def test_get_result_json_shape_no_score():
    index = dl.DoctrineIndex(CORPUS_DIR)
    card = index.get("escobar_materiality")
    assert _EXPECTED_KEYS.issubset(card.keys())
    assert "score" not in card


# ─────────────────────────────────────────────────────────────────────────────
# CLI smoke tests (subprocess, format=json/text, --list, --doctrine)
# ─────────────────────────────────────────────────────────────────────────────

def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "doctrine_lookup.py"), *args],
        capture_output=True, text=True, timeout=30,
    )


def test_cli_query_json_format():
    proc = _run_cli("--query", "prior qui tam pending", "--format", "json")
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert isinstance(data, list) and data
    assert data[0]["slug"] == "first_to_file_kbr_carter"


def test_cli_doctrine_slug_text_format():
    proc = _run_cli("--doctrine", "schutte_safeco_scienter")
    assert proc.returncode == 0
    assert "Schutte" in proc.stdout
    assert "SuperValu" in proc.stdout


def test_cli_list():
    proc = _run_cli("--list")
    assert proc.returncode == 0
    slugs = proc.stdout.strip().splitlines()
    assert "escobar_materiality" in slugs
    assert "twombly_parallel_conduct" in slugs
    assert len(slugs) >= 12


def test_cli_unknown_doctrine_nonzero_exit():
    proc = _run_cli("--doctrine", "not_a_real_slug", "--format", "json")
    assert proc.returncode == 1
    assert json.loads(proc.stdout) == []
