"""precedent_cache.py — get/put roundtrip + clean miss (T3b).

Hermetic: every test points --jsonl-path / blobs_dir at tmp_path, never the
real data/cache/precedent_verifications.jsonl.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.precedent_cache import REPO, get_verification, main, put_verification


def test_clean_miss_returns_none(tmp_path):
    jsonl = tmp_path / "precedent_verifications.jsonl"
    assert get_verification("Nonexistent v. Case", jsonl_path=jsonl) is None


def test_put_then_get_roundtrip(tmp_path):
    jsonl = tmp_path / "precedent_verifications.jsonl"
    citation = "United States v. Mackby, 261 F.3d 821 (9th Cir. 2001)"
    put_verification(
        citation, verified=True, docket="97-15931",
        source_url="https://www.courtlistener.com/opinion/mackby/",
        reviewer="investigator", jsonl_path=jsonl,
    )
    rec = get_verification(citation, jsonl_path=jsonl)
    assert rec is not None
    assert rec["verified"] is True
    assert rec["docket"] == "97-15931"
    assert rec["source_url"] == "https://www.courtlistener.com/opinion/mackby/"


def test_get_is_case_and_whitespace_insensitive(tmp_path):
    jsonl = tmp_path / "precedent_verifications.jsonl"
    put_verification("United States v.  Mackby, 261 F.3d 821", verified=True, jsonl_path=jsonl)
    rec = get_verification("united states v. mackby, 261 f.3d 821", jsonl_path=jsonl)
    assert rec is not None


def test_quote_stored_as_blob_not_inlined(tmp_path):
    jsonl = tmp_path / "precedent_verifications.jsonl"
    blobs = tmp_path / "policy_texts"
    citation = "United States v. Escobar, 579 U.S. 176 (2016)"
    rec = put_verification(
        citation, verified=True, quote="the materiality standard is demanding...",
        jsonl_path=jsonl, blobs_dir=blobs,
    )
    assert "quote_path" in rec
    assert "the materiality standard" not in json.dumps(rec)   # not inlined into the record
    assert "the materiality standard" not in jsonl.read_text()  # nor the jsonl line
    blob_path = Path(rec["quote_path"])
    full = blob_path if blob_path.is_absolute() else REPO / blob_path
    assert full.exists()
    assert "the materiality standard" in full.read_text()


def test_reverification_supersedes_prior_record(tmp_path):
    """put is append-only; get returns the LAST (most recent) match."""
    jsonl = tmp_path / "precedent_verifications.jsonl"
    citation = "Universal Health Servs. v. United States ex rel. Escobar"
    put_verification(citation, verified=False, jsonl_path=jsonl)
    put_verification(citation, verified=True, docket="15-7", jsonl_path=jsonl)
    rec = get_verification(citation, jsonl_path=jsonl)
    assert rec["verified"] is True
    assert rec["docket"] == "15-7"


def test_corrupt_line_is_skipped_not_raised(tmp_path):
    jsonl = tmp_path / "precedent_verifications.jsonl"
    jsonl.write_text("{not json\n" + json.dumps({"citation": "Foo v. Bar", "verified": True}) + "\n")
    rec = get_verification("Foo v. Bar", jsonl_path=jsonl)
    assert rec is not None and rec["verified"] is True


# ── CLI ───────────────────────────────────────────────────────────────────

def test_cli_get_clean_miss_exit_code_and_message(tmp_path, capsys):
    rc = main(["--jsonl-path", str(tmp_path / "x.jsonl"), "get", "--citation", "Nope v. Nothing"])
    assert rc == 1
    assert "MISS" in capsys.readouterr().out


def test_cli_put_then_get_roundtrip(tmp_path, capsys):
    jsonl = str(tmp_path / "x.jsonl")
    rc = main(["--jsonl-path", jsonl, "put", "--citation", "Foo v. Bar",
              "--verified", "true", "--docket", "1:23-cv-456"])
    assert rc == 0
    capsys.readouterr()  # drain the put's stdout

    rc = main(["--jsonl-path", jsonl, "--format", "json", "get", "--citation", "Foo v. Bar"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hit"] is True
    assert payload["docket"] == "1:23-cv-456"


def test_cli_verified_false_parses_correctly(tmp_path, capsys):
    jsonl = str(tmp_path / "x.jsonl")
    main(["--jsonl-path", jsonl, "put", "--citation", "Weak Cite", "--verified", "false"])
    capsys.readouterr()
    main(["--jsonl-path", jsonl, "--format", "json", "get", "--citation", "Weak Cite"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["verified"] is False
