"""Team launcher — pure-function tests (no claude/node/network)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.np_launch import build_investigate_cmd
from scripts.orchestrate_pipeline import _build_subject_for_npi, _find_npi_in_queues


# ── np_launch command construction ───────────────────────────────────────────

def test_build_investigate_cmd_shape():
    cmd = build_investigate_cmd("1900000016", rounds=4, resume=None,
                                budget=False, python="py")
    assert cmd == ["py", "-m", "scripts.orchestrate_pipeline",
                   "--invocation-mode", "investigate", "--npi", "1900000016",
                   "--max-adversarial", "4"]


def test_build_investigate_cmd_options():
    cmd = build_investigate_cmd("1234567893", rounds=2, resume="CASE-X",
                                budget=True, python="py")
    assert cmd[cmd.index("--resume-case") + 1] == "CASE-X"
    assert "--budget-mode" in cmd


def test_build_investigate_cmd_rejects_bad_npi():
    # NPIs are EXACTLY 10 digits starting with 1 or 2 — no zfill leniency
    # (a 5-digit typo once zfilled its way into actually launching a run).
    for bad in ("12345678901", "not-an-npi", "", "12345", "123456789",
                "0123456789", "9123456789"):
        with pytest.raises(ValueError):
            build_investigate_cmd(bad, rounds=4, resume=None, budget=False)


# ── orchestrate_pipeline: queue discovery + subject fallback ─────────────────

def test_find_npi_in_queues(tmp_path):
    qdir = tmp_path / "validation" / "triage"
    qdir.mkdir(parents=True)
    (qdir / "2023_top1-500.json").write_text(json.dumps(
        {"score_year": 2023, "k_range": [1, 2],
         "items": [{"npi": "1900000016"}, {"npi": "0000000001"}],
         "batch_summary": "x"}))
    (qdir / "broken.json").write_text("{not json")
    assert _find_npi_in_queues("1900000016", tmp_path) is not None
    assert _find_npi_in_queues("9999999999", tmp_path) is None


def test_subject_fallback_partb_panel(tmp_path):
    proc = tmp_path / "data" / "processed"
    proc.mkdir(parents=True)
    pd.DataFrame([{
        "Rndrng_NPI": "1900000016", "Rndrng_Prvdr_First_Name": "MAHENDRA",
        "Rndrng_Prvdr_Last_Org_Name": "MAPLE", "Rndrng_Prvdr_Crdntls": "M.D.",
        "Rndrng_Prvdr_Type": "Pain Management", "Rndrng_Prvdr_City": "FLORISSANT",
        "Rndrng_Prvdr_State_Abrvtn": "MO", "Tot_Benes": 700, "Tot_Srvcs": 21000,
        "Tot_Mdcr_Pymt_Amt": 1_500_000.0,
    }]).to_parquet(proc / "cms_partb_2023.parquet")
    subject, source = _build_subject_for_npi("1900000016", tmp_path)
    assert "MAPLE" in subject and "1900000016" in subject
    assert "Pain Management" in subject and "$1,500,000" in subject
    assert source == "partb_panel:cms_partb_2023.parquet"


def test_subject_fallback_bare(tmp_path):
    subject, source = _build_subject_for_npi("1900000016", tmp_path)
    assert source == "bare"
    assert "1900000016" in subject and "ad-hoc" in subject


# ── end-to-end seam: the exact make-built argv through the REAL parser ───────

def test_make_cmd_parses_and_dispatches(tmp_path, monkeypatch):
    """The regression that would have caught the `-v` bug: drive the real
    orchestrate_pipeline argparse + dispatch with the exact command
    `make investigate` constructs, stubbing only the launch seam."""
    import scripts.orchestrate_pipeline as op

    cmd = build_investigate_cmd("1900000016", rounds=4, resume=None,
                                budget=False, python="py")
    argv = cmd[3:]  # strip "<python> -m scripts.orchestrate_pipeline"

    launched = {}
    monkeypatch.setattr(op, "run_stage4_investigator",
                        lambda **kw: launched.update(kw) or 0)
    monkeypatch.setattr(sys, "argv", ["orchestrate_pipeline.py"] + argv)
    assert op.main() == 0
    assert "1900000016" in launched.get("subject", "")
    assert launched.get("max_adversarial") == 4
