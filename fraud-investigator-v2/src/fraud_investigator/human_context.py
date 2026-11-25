"""Mid-run human-context inbox — merge operator notes into the evidence stream.

An operator watching a live run can append a note to
``state/{case_id}/human_context.jsonl`` (one JSON object per line, via
``scripts/inject_context.py``) at any point while the loop is in flight. This
module is the read side: a pure function that turns any NOT-yet-merged lines
into ``human_attested`` ``EvidenceItem`` objects, which the orchestrator then
folds into the SAME evidence list every agent's evidence already flows
through (``inv_out.evidence`` → ``_merge_evidence`` → ``_evidence_payload`` /
``_all_evidence_for_scoring``) — no prompt-template changes required; every
downstream investigator / counsel_build / counsel_disposition prompt sees
the note exactly like any other confirmed-fact evidence item.

Idempotent by construction: each line's derived evidence id is
``ev-human-<line id>``; a line already present in ``all_evidence_ids`` is
skipped on every subsequent call, so re-running (or resuming) the loop never
duplicates a note.
"""
from __future__ import annotations

import json
from pathlib import Path

from .schemas import CaseState, EvidenceItem, EvidenceTier
from .state import case_dir, STATE_ROOT_DEFAULT

HUMAN_CONTEXT_FILENAME = "human_context.jsonl"


def human_context_path(case_id: str, state_root: Path = STATE_ROOT_DEFAULT) -> Path:
    """Path to the case's human-context inbox (may not exist yet)."""
    return case_dir(case_id, state_root) / HUMAN_CONTEXT_FILENAME


def _read_entries(path: Path) -> list[dict]:
    """Parse a human_context.jsonl file into a list of dicts.

    Absent or empty file → ``[]`` (clean no-op). A line that fails to parse
    as JSON is skipped rather than raising — a malformed line must not take
    down the run.
    """
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _to_evidence_item(entry: dict) -> EvidenceItem | None:
    """Convert one human_context.jsonl entry to a human_attested EvidenceItem.

    Returns ``None`` for an entry missing the required ``id``/``text`` fields,
    or one whose text otherwise fails EvidenceItem's own validation (e.g. a
    hand-edited line under the 10-char fact floor) — malformed, skipped, not
    fatal. ``inject_context.py`` already enforces the 10-char floor at write
    time; this is defense-in-depth for a file edited by another means.
    """
    hc_id = entry.get("id")
    text = entry.get("text")
    if not hc_id or not text:
        return None
    source = entry.get("source") or "operator"
    try:
        return EvidenceItem(
            id=f"ev-human-{hc_id}",
            tier=EvidenceTier.CONFIRMED_FACT,
            fact=text,
            citation=f"human context injection: {source}",
            confidence=0.95,
            provenance="human_attested",
        )
    except Exception:
        return None


def new_human_evidence(state: CaseState, state_root: Path = STATE_ROOT_DEFAULT) -> list[EvidenceItem]:
    """Return the ``EvidenceItem``s for every human_context.jsonl entry NOT
    already present in ``state.all_evidence_ids``.

    Pure with respect to ``state`` — does not mutate it or write to disk;
    the caller (the orchestrator) is responsible for merging the returned
    items into an iteration's evidence and persisting the result, exactly
    like any other agent-produced evidence.
    """
    entries = _read_entries(human_context_path(state.case_id, state_root))
    if not entries:
        return []
    seen = set(state.all_evidence_ids)
    out: list[EvidenceItem] = []
    for entry in entries:
        item = _to_evidence_item(entry)
        if item is None or item.id in seen:
            continue
        out.append(item)
        seen.add(item.id)  # guard against duplicate ids within the same file
    return out
