"""CourtListener token accessibility for handoff.

scripts/courtlistener_search.py accepts EITHER COURTLISTENER_TOKEN or the older
COURTLISTENER_API_TOKEN, so whichever name is set in .env works.

All hermetic — no network, no os.environ mutation that leaks.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))


# ── courtlistener_search.py token fallback (_resolve_token, env-injected) ────
import courtlistener_search as CL  # noqa: E402


def test_primary_token_name():
    assert CL._resolve_token({"COURTLISTENER_TOKEN": "primary-abc"}) == "primary-abc"


def test_legacy_api_token_name_accepted():
    assert CL._resolve_token({"COURTLISTENER_API_TOKEN": "legacy-xyz"}) == "legacy-xyz"


def test_primary_wins_over_legacy():
    assert CL._resolve_token({"COURTLISTENER_TOKEN": "primary",
                              "COURTLISTENER_API_TOKEN": "legacy"}) == "primary"


def test_no_token_is_empty():
    assert CL._resolve_token({}) == ""


def test_token_is_stripped():
    assert CL._resolve_token({"COURTLISTENER_TOKEN": "  spaced  "}) == "spaced"
