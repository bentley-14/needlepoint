"""PARKED-case registry (M3) — orchestrator hook + re-trigger scanner.

The hook (`Orchestrator._maybe_park_case`) writes a sidecar record under
<state_root>/parked/ when a case is abandoned ONLY because convergence-floor
slot 3 was empty (PASS_NO_SLOT3, no FAIL counts). The scanner
(scripts/check_parked_cases.py) checks watch NPIs against regulatory-refresh
sources. Both are exercised dry/synthetically — no claude -p, no repo data.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd

from fraud_investigator.orchestrator import Orchestrator

REPO = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "check_parked_cases", REPO / "scripts" / "check_parked_cases.py")
cpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cpc)

SUBJECT = "Dr. T (NPI 1234567893) x"
REFUTATION = "Considered and refuted: volumes are inconsistent with any legitimate panel size."


def _fb(count_id, *, anchors=(), regulatory=(), convergent=("e2", "e3")):
    return {
        "count_id": count_id,
        "extremity_evidence_ids": ["e1"],
        "convergent_signal_evidence_ids": list(convergent),
        "falsity_anchor_evidence_ids": list(anchors),
        "regulatory_corroboration_evidence_ids": list(regulatory),
        "innocent_explanations_considered": ["new-practice ramp"],
        "innocent_explanations_refuted_narrative": REFUTATION,
    }


class _StubBuild:
    """Minimal object whose .model_dump() returns a counsel_build-shaped dict."""

    def __init__(self, factual_bases):
        self._dump = {
            "counts_pled": [{"count_id": fb["count_id"]} for fb in factual_bases],
            "factual_basis_per_count": factual_bases,
            "estimated_aggregate_damages_usd": 1_000_000.0,
        }

    def model_dump(self):
        return self._dump


def _orch(tmp_path):
    return Orchestrator(subject=SUBJECT, state_dir=tmp_path, dry_run=True)


def _parked_file(orch, tmp_path) -> Path:
    return tmp_path / "parked" / f"{orch.state.case_id}.json"


# ── _maybe_park_case ─────────────────────────────────────────────────────────

def test_pass_no_slot3_parks(tmp_path):
    orch = _orch(tmp_path)
    build = _StubBuild([_fb("C1")])  # slots 1/2/4/5 filled, slot 3 empty both ways
    orch._maybe_park_case(build, None)
    f = _parked_file(orch, tmp_path)
    assert f.exists()
    rec = json.loads(f.read_text())
    assert rec["case_id"] == orch.state.case_id
    assert rec["subject"] == SUBJECT
    assert rec["npi"] == "1234567893"
    assert rec["watch_npis"] == ["1234567893"]
    assert rec["missing"] == "slot3: no falsity anchor and no regulatory corroboration"
    assert rec["count_floors"] == {"C1": "PASS_NO_SLOT3"}
    assert orch.state.case_id in rec["resume_cmd"]
    assert "python3 -m fraud_investigator" in rec["resume_cmd"]


def test_fail_count_does_not_park(tmp_path):
    orch = _orch(tmp_path)
    # One PASS_NO_SLOT3 count + one FAIL count (only 1 convergent id) → no park.
    build = _StubBuild([_fb("C1"), _fb("C2", convergent=("e2",))])
    orch._maybe_park_case(build, None)
    assert not _parked_file(orch, tmp_path).exists()


def test_clean_pass_does_not_park(tmp_path):
    orch = _orch(tmp_path)
    build = _StubBuild([_fb("C1", anchors=("e9",))])  # slot 3 filled → PASS
    orch._maybe_park_case(build, None)
    assert not _parked_file(orch, tmp_path).exists()


def test_cobillers_from_evidence_pack_are_watched(tmp_path):
    orch = _orch(tmp_path)
    case_dir = tmp_path / orch.state.case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "evidence_pack.json").write_text(json.dumps(
        {"npi": "1234567893", "cobiller_npis": ["1111111111", "2222222222"]}))
    orch._maybe_park_case(_StubBuild([_fb("C1")]), None)
    rec = json.loads(_parked_file(orch, tmp_path).read_text())
    assert rec["watch_npis"] == ["1234567893", "1111111111", "2222222222"]


def test_park_never_raises_into_abandon_path(tmp_path):
    orch = _orch(tmp_path)

    class _Broken:
        def model_dump(self):
            raise RuntimeError("boom")

    orch._maybe_park_case(_Broken(), None)  # must not raise
    assert not _parked_file(orch, tmp_path).exists()


# ── scripts/check_parked_cases.py ────────────────────────────────────────────

def _seed_parked(state_root: Path, watch_npis):
    parked = state_root / "parked"
    parked.mkdir(parents=True, exist_ok=True)
    rec = {
        "case_id": "TEST-CASE-001",
        "subject": SUBJECT,
        "parked_at": "2026-06-12T00:00:00Z",
        "npi": watch_npis[0],
        "watch_npis": list(watch_npis),
        "missing": "slot3: no falsity anchor and no regulatory corroboration",
        "count_floors": {"C1": "PASS_NO_SLOT3"},
        "resume_cmd": ("PYTHONPATH=fraud-investigator-v2/src python3 -m "
                       f'fraud_investigator "{SUBJECT}" --case-id TEST-CASE-001'),
    }
    (parked / "TEST-CASE-001.json").write_text(json.dumps(rec))


def test_scanner_reports_revocation_hit(tmp_path):
    state_root = tmp_path / "state"
    data_root = tmp_path / "data"
    _seed_parked(state_root, ["1234567893", "1111111111"])
    proc = data_root / "processed"
    proc.mkdir(parents=True)
    pd.DataFrame({
        "NPI": ["1111111111", "9999999999"],
        "LAST_NAME": ["COBILLER", "OTHER"],
        "REVOCATION_RSN": ["Felony conviction", "Address"],
    }).to_parquet(proc / "revoked_medicare_providers.parquet")
    # leie / ca_si_list / gold_set absent → skipped cleanly.

    results = cpc.scan(state_root, data_root)
    assert len(results) == 1
    r = results[0]
    assert r["case_id"] == "TEST-CASE-001"
    hits = r["hits"]
    assert len(hits) == 1
    assert hits[0]["source"] == "revoked_medicare_providers.parquet"
    assert hits[0]["npi"] == "1111111111"
    assert hits[0]["row"]["LAST_NAME"] == "COBILLER"
    assert "--case-id TEST-CASE-001" in r["resume_cmd"]


def test_scanner_no_hit_and_gold_set_hit(tmp_path):
    state_root = tmp_path / "state"
    data_root = tmp_path / "data"
    _seed_parked(state_root, ["1234567893"])
    results = cpc.scan(state_root, data_root)  # no sources at all → no hits
    assert results[0]["hits"] == []

    extracted = data_root / "extracted"
    extracted.mkdir(parents=True)
    (extracted / "gold_set.jsonl").write_text(
        json.dumps({"npi": "1234567893", "case_id": "doj-test"}) + "\n")
    results = cpc.scan(state_root, data_root)
    assert [h["source"] for h in results[0]["hits"]] == ["gold_set.jsonl"]


def test_scanner_cli_exits_zero_and_quiet_json(tmp_path, capsys):
    state_root = tmp_path / "state"
    data_root = tmp_path / "data"
    _seed_parked(state_root, ["1234567893"])
    rc = cpc.main(["--state-root", str(state_root), "--data-root", str(data_root),
                   "--format", "json", "--quiet"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []  # no hits → quiet json is empty
