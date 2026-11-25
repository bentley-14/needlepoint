"""CourtListener token accessibility for handoff.

Pins two things a fresh-clone teammate depends on:
  1. scripts/courtlistener_search.py accepts EITHER COURTLISTENER_TOKEN or the
     older COURTLISTENER_API_TOKEN (so whichever name they set in .env works).
  2. scripts/doctor.py reports the token as set/missing from env OR .env.

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


# ── doctor.py courtlistener_configured ───────────────────────────────────────

def test_doctor_configured_from_env(monkeypatch):
    import doctor
    assert doctor.courtlistener_configured(
        env={"COURTLISTENER_TOKEN": "t"}, dotenv_path=Path("/does/not/exist")) is True


def test_doctor_configured_from_legacy_env(monkeypatch):
    import doctor
    assert doctor.courtlistener_configured(
        env={"COURTLISTENER_API_TOKEN": "t"}, dotenv_path=Path("/does/not/exist")) is True


def test_doctor_unconfigured(monkeypatch):
    import doctor
    assert doctor.courtlistener_configured(
        env={}, dotenv_path=Path("/does/not/exist")) is False


def test_doctor_configured_from_dotenv(tmp_path):
    import doctor
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=1\nCOURTLISTENER_TOKEN=from-dotenv\n")
    assert doctor.courtlistener_configured(env={}, dotenv_path=env_file) is True


def test_doctor_dotenv_blank_value_is_unconfigured(tmp_path):
    import doctor
    env_file = tmp_path / ".env"
    env_file.write_text("COURTLISTENER_TOKEN=\n")  # commented-out / empty
    assert doctor.courtlistener_configured(env={}, dotenv_path=env_file) is False
