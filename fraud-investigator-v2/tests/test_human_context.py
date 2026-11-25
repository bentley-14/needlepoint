"""Tests for the mid-run human-context inbox
(scripts/inject_context.py + fraud_investigator.human_context).

Covers: (a) the CLI writes a valid JSONL line and rejects an unknown case
without creating it; (b) the merge helper converts inbox entries to
human_attested EvidenceItems and dedupes on re-run; (c) an absent/empty
inbox file is a clean no-op.

Run: pytest fraud-investigator-v2/tests/test_human_context.py -v
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import pytest

import inject_context  # noqa: E402
from fraud_investigator.human_context import (  # noqa: E402
    human_context_path,
    new_human_evidence,
)
from fraud_investigator.schemas import CaseState, EvidenceTier  # noqa: E402

CID = "CASE-HC-TEST-2026"


def _case_state(case_id: str = CID, all_evidence_ids: list[str] | None = None) -> CaseState:
    return CaseState(
        case_id=case_id,
        subject="Dr X, O.D. -- Optometry -- NPI 1900000017",
        start_time=datetime.utcnow(),
        last_updated=datetime.utcnow(),
        all_evidence_ids=list(all_evidence_ids or []),
    )


# ─────────────────────────────────────────────────────────────────────────────
# (a) inject_context CLI
# ─────────────────────────────────────────────────────────────────────────────


def test_inject_writes_valid_line(tmp_path):
    (tmp_path / CID).mkdir(parents=True)

    info = inject_context.inject(
        CID, "Confirmed: optometrist sees mostly established patients.",
        source="phone call", actor="human", state_root=tmp_path,
    )

    assert info["id"].startswith("hc-") and len(info["id"]) == len("hc-") + 8
    inbox = tmp_path / CID / "human_context.jsonl"
    assert inbox.exists()
    lines = inbox.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert set(row.keys()) == {"id", "ts", "actor", "text", "source"}
    assert row["id"] == info["id"]
    assert row["actor"] == "human"
    assert row["source"] == "phone call"
    assert row["text"] == "Confirmed: optometrist sees mostly established patients."
    # ISO timestamp parses cleanly
    datetime.fromisoformat(row["ts"])


def test_inject_appends_multiple_lines(tmp_path):
    (tmp_path / CID).mkdir(parents=True)
    inject_context.inject(CID, "first note", state_root=tmp_path)
    inject_context.inject(CID, "second note", state_root=tmp_path)

    lines = (tmp_path / CID / "human_context.jsonl").read_text().splitlines()
    assert len(lines) == 2
    ids = {json.loads(l)["id"] for l in lines}
    assert len(ids) == 2  # distinct ids, no collision


def test_inject_rejects_unknown_case_without_creating_it(tmp_path):
    with pytest.raises(SystemExit):
        inject_context.inject(CID, "should fail", state_root=tmp_path)
    assert not (tmp_path / CID).exists()


def test_inject_rejects_empty_text(tmp_path):
    (tmp_path / CID).mkdir(parents=True)
    with pytest.raises(SystemExit):
        inject_context.inject(CID, "   ", state_root=tmp_path)


def test_inject_rejects_too_short_text(tmp_path):
    """Mirrors EvidenceItem.fact's min_length=10 (schemas.py) — fail at
    injection time, not deep in the orchestrator's next merge."""
    (tmp_path / CID).mkdir(parents=True)
    with pytest.raises(SystemExit):
        inject_context.inject(CID, "too short", state_root=tmp_path)
    assert not (tmp_path / CID / "human_context.jsonl").exists()


def test_inject_default_source_is_operator(tmp_path):
    (tmp_path / CID).mkdir(parents=True)
    info = inject_context.inject(CID, "note with no explicit source", state_root=tmp_path)
    assert info["source"] == "operator"


# ─────────────────────────────────────────────────────────────────────────────
# (b) merge helper — converts to human_attested EvidenceItems + dedupes
# ─────────────────────────────────────────────────────────────────────────────


def test_new_human_evidence_converts_entries(tmp_path):
    (tmp_path / CID).mkdir(parents=True)
    inject_context.inject(
        CID, "Confirmed: normal annual-exam billing for this specialty.",
        source="compliance officer", state_root=tmp_path,
    )
    state = _case_state()

    items = new_human_evidence(state, tmp_path)

    assert len(items) == 1
    item = items[0]
    assert item.id.startswith("ev-human-hc-")
    assert item.tier == EvidenceTier.CONFIRMED_FACT
    assert item.provenance == "human_attested"
    assert item.fact == "Confirmed: normal annual-exam billing for this specialty."
    assert item.citation == "human context injection: compliance officer"
    assert item.confidence == 0.95


def test_new_human_evidence_dedupes_on_rerun(tmp_path):
    (tmp_path / CID).mkdir(parents=True)
    inject_context.inject(CID, "a durable note", state_root=tmp_path)
    state = _case_state()

    first = new_human_evidence(state, tmp_path)
    assert len(first) == 1

    # Simulate the orchestrator's merge: the id lands in all_evidence_ids.
    state.all_evidence_ids.append(first[0].id)

    second = new_human_evidence(state, tmp_path)
    assert second == []


def test_new_human_evidence_only_returns_unmerged_entries(tmp_path):
    """Two notes injected across 'iterations'; only the new one comes back
    once the first has already been merged into all_evidence_ids."""
    (tmp_path / CID).mkdir(parents=True)
    inject_context.inject(CID, "this is note one", state_root=tmp_path)
    state = _case_state()
    first_batch = new_human_evidence(state, tmp_path)
    state.all_evidence_ids.append(first_batch[0].id)

    inject_context.inject(CID, "this is note two", state_root=tmp_path)
    second_batch = new_human_evidence(state, tmp_path)

    assert len(second_batch) == 1
    assert second_batch[0].fact == "this is note two"


def test_new_human_evidence_skips_malformed_lines(tmp_path):
    case_dir = tmp_path / CID
    case_dir.mkdir(parents=True)
    inbox = case_dir / "human_context.jsonl"
    inbox.write_text(
        "not json at all\n"
        '{"id": "hc-aaaaaaaa", "text": "well-formed note", "actor": "human", '
        '"ts": "2026-07-01T00:00:00+00:00", "source": "op"}\n'
        '{"id": "hc-bbbbbbbb"}\n'  # missing text — dropped
        '{"text": "missing id"}\n'  # missing id — dropped
    )
    state = _case_state()

    items = new_human_evidence(state, tmp_path)

    assert len(items) == 1
    assert items[0].fact == "well-formed note"


# ─────────────────────────────────────────────────────────────────────────────
# (c) absent / empty file — clean no-op
# ─────────────────────────────────────────────────────────────────────────────


def test_absent_inbox_file_is_noop(tmp_path):
    (tmp_path / CID).mkdir(parents=True)
    state = _case_state()

    assert not human_context_path(CID, tmp_path).exists()
    assert new_human_evidence(state, tmp_path) == []


def test_empty_inbox_file_is_noop(tmp_path):
    (tmp_path / CID).mkdir(parents=True)
    (tmp_path / CID / "human_context.jsonl").write_text("")
    state = _case_state()

    assert new_human_evidence(state, tmp_path) == []


def test_missing_case_dir_is_noop_not_an_error(tmp_path):
    """new_human_evidence must not raise even if the case dir was never
    created (defensive — the orchestrator seam always has a real case dir,
    but the helper itself should not assume it). Returns [] either way."""
    state = _case_state(case_id="CASE-NEVER-CREATED-2026")
    assert new_human_evidence(state, tmp_path) == []
    assert new_human_evidence(state, tmp_path) == []  # still a no-op on a 2nd call
