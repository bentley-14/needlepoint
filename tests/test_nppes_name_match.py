"""Fuzzy first-name matching in scripts/nppes_lookup._first_name_strength.

Loaded by file path since scripts/ isn't an importable package.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "nppes_lookup", Path(__file__).resolve().parents[1] / "scripts" / "nppes_lookup.py"
)
nppes = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(nppes)
strength = nppes._first_name_strength


@pytest.mark.parametrize("query,cand,expected", [
    ("Emily", "Emily", "exact"),
    ("Robert", "Rob", "prefix"),      # nickname (query longer)
    ("Emil", "Emilee", "prefix"),     # abbreviation (candidate longer)
    ("William", "Will", "prefix"),
    ("Emily", "Emilee", "fuzzy"),     # the observed case — the whole point
    ("Michael", "Micheal", "fuzzy"),  # transposition typo
    ("Steven", "Stephen", "fuzzy"),
    ("Jon", "Ron", None),             # different people
    ("Kathryn", "Catherine", None),
    ("Emily", "Emma", None),
    ("Emily", "", None),
    ("", "Emily", None),
])
def test_first_name_strength(query, cand, expected):
    assert strength(query, cand) == expected


@pytest.mark.parametrize("query,cand", [("John", "Joan"), ("Mary", "Gary")])
def test_known_fuzzy_false_positives_are_flagged_not_exact(query, cand):
    # No string metric separates these from real variants; they DO match
    # fuzzily — the safety is that "fuzzy" is surfaced/flagged, never "exact",
    # and last-name(exact)+state narrow the query in practice.
    assert strength(query, cand) == "fuzzy"
