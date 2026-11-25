"""Append-only JSONL event writer for the operationalized pipeline.

Used by the orchestrator (and downstream stage agents) to emit structured
events that the Streamlit UI can tail in near-real-time. Each event is one
JSON object per line.

Event protocol
--------------
Every event has the shape::

    {
      "ts": "2026-05-19T14:22:01.234Z",   # ISO 8601 UTC
      "stage": "rule_derivation",          # stage name (see STAGES)
      "type": "STAGE_TOOL_USE",            # see EVENT_TYPES
      "payload": { ... }                    # type-specific payload
    }

Stages (the 4-stage pipeline)::

    rule_derivation -> ml_training -> triage -> investigator

Event types::

    STAGE_START         — stage began (payload usually empty or {"subject": ...})
    STAGE_PROGRESS      — progress update (payload includes "pct": 0-100)
    STAGE_LOG_LINE      — human-readable log line (payload: {"level", "msg"})
    STAGE_TOOL_USE      — agent invoked a tool (payload: {"name", "input", "id"})
    STAGE_TOOL_RESULT   — tool returned a result (payload: {"tool_use_id", "content"})
    STAGE_TEXT_TURN     — model emitted assistant text (payload: {"text"})
    STAGE_METRIC        — numeric metric (payload: {"name", "value", ...})
    STAGE_COMPLETE      — stage succeeded (payload usually has output path/refs)
    STAGE_FAILED        — stage errored (payload: {"error", "traceback"})

Files
-----
The sink writes to ``state/runs/{run_id}/events.jsonl``. It is append-only;
no partial writes mid-line (open in line-buffered mode and write a full
line per emit).

This module deliberately has zero dependencies beyond the stdlib so it can
be imported from the orchestrator, agent drivers, and the walk-forward
training script without dragging in heavy ML imports.

Thread-safety: a single sink instance is intended to be used from one
process at a time. Multiple processes writing to the same events.jsonl
should each call ``EventSink.append`` which opens-writes-closes per event
(safe for concurrent appends on POSIX, where ``open(...,"a")`` + a single
``write(line+"\\n")`` is atomic if ≤ PIPE_BUF bytes).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# Canonical stage names — code that emits events should use these constants.
STAGE_RULE_DERIVATION = "rule_derivation"
STAGE_ML_TRAINING = "ml_training"
STAGE_TRIAGE = "triage"          # Stage 3 (renamed from "rerank" 2026-05-31; canonical triage agent)
STAGE_INVESTIGATOR = "investigator"

# Per-agent main-floor stages — emitted by Orchestrator._stage() rotation
# (commit d912cbe) so the Firm UI can light up only the currently-executing
# agent's sprite instead of all 8 main-floor sprites simultaneously.
STAGE_COUNSEL_BUILD = "counsel_build"
STAGE_COUNSEL_FIRST_CHAIR_REVIEW = "counsel_first_chair_review"
STAGE_COUNSEL_REBUTTAL = "counsel_rebuttal"
STAGE_COUNSEL_DISPOSITION = "counsel_disposition"
STAGE_DEFENSE = "defense"
STAGE_LEGAL_ADV_RESPONSE = "legal_adv_response"
STAGE_REBUTTAL_INVESTIGATOR = "rebuttal_investigator"
# Guardrail-3 innocent-explanation foreclosure check (PR #247) — runs after
# the adversarial loop, before case_writer. Added to STAGES 2026-06-12 after
# the orchestrator's _stage("foreclosure_check") crashed every run that
# reached it (the constant existed on the orchestrator side but was never
# registered here — see test_event_sink_stage_coverage).
STAGE_FORECLOSURE_CHECK = "foreclosure_check"
STAGE_CASE_WRITER = "case_writer"
# Two-pass investigator (discovery-mode prompts, 2026-05-21):
# Pass A = investigator_discover (open exploration, fires once at iter-1)
# Pass B = investigator (existing rigorous verification, continues across iters)
STAGE_INVESTIGATOR_DISCOVER = "investigator_discover"

STAGES = (
    STAGE_RULE_DERIVATION, STAGE_ML_TRAINING, STAGE_TRIAGE, STAGE_INVESTIGATOR,
    STAGE_INVESTIGATOR_DISCOVER,
    # Main-floor per-agent stages
    STAGE_COUNSEL_BUILD, STAGE_COUNSEL_FIRST_CHAIR_REVIEW,
    STAGE_COUNSEL_REBUTTAL, STAGE_COUNSEL_DISPOSITION,
    STAGE_DEFENSE, STAGE_LEGAL_ADV_RESPONSE, STAGE_REBUTTAL_INVESTIGATOR,
    STAGE_FORECLOSURE_CHECK,
    STAGE_CASE_WRITER,
)

# Canonical event types.
EVENT_STAGE_START = "STAGE_START"
EVENT_STAGE_PROGRESS = "STAGE_PROGRESS"
EVENT_STAGE_LOG_LINE = "STAGE_LOG_LINE"
EVENT_STAGE_TOOL_USE = "STAGE_TOOL_USE"
EVENT_STAGE_TOOL_RESULT = "STAGE_TOOL_RESULT"
EVENT_STAGE_TEXT_TURN = "STAGE_TEXT_TURN"
EVENT_STAGE_METRIC = "STAGE_METRIC"
EVENT_STAGE_COMPLETE = "STAGE_COMPLETE"
EVENT_STAGE_FAILED = "STAGE_FAILED"

EVENT_TYPES = (
    EVENT_STAGE_START,
    EVENT_STAGE_PROGRESS,
    EVENT_STAGE_LOG_LINE,
    EVENT_STAGE_TOOL_USE,
    EVENT_STAGE_TOOL_RESULT,
    EVENT_STAGE_TEXT_TURN,
    EVENT_STAGE_METRIC,
    EVENT_STAGE_COMPLETE,
    EVENT_STAGE_FAILED,
)


def _iso_now() -> str:
    """ISO 8601 UTC timestamp, e.g. '2026-05-19T14:22:01.234Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
           f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


class EventSink:
    """Append-only writer for ``events.jsonl``.

    Usage::

        sink = EventSink(Path("state/runs/run_001/events.jsonl"))
        sink.emit(STAGE_RULE_DERIVATION, EVENT_STAGE_START, {})
        sink.emit(STAGE_RULE_DERIVATION, EVENT_STAGE_TOOL_USE,
                  {"name": "Read", "input": {"file_path": "..."}})
        sink.emit(STAGE_RULE_DERIVATION, EVENT_STAGE_COMPLETE,
                  {"filter_path": "..."})

    The file's parent dir is created on first emit. The sink does NOT
    auto-rotate, archive, or trim the file — that's the caller's concern.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        # Ensure parent dir exists at construction (cheap)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, stage: str, type_: str, payload: dict[str, Any] | None = None) -> None:
        """Append one event. Validates the stage + type are in the canonical sets."""
        if stage not in STAGES and not stage.startswith("agent_"):
            # Allow stage="agent_<name>" for supporting agents (not in the 4 main stages)
            raise ValueError(f"Unknown stage: {stage!r}. Expected one of {STAGES} or 'agent_*'.")
        if type_ not in EVENT_TYPES:
            raise ValueError(f"Unknown event type: {type_!r}. Expected one of {EVENT_TYPES}.")
        event = {
            "ts": _iso_now(),
            "stage": stage,
            "type": type_,
            "payload": payload or {},
        }
        line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        # POSIX: open(..., "a") + single write of <PIPE_BUF (typically 4KB) is
        # atomic with respect to concurrent appends. Most lines are well under
        # PIPE_BUF; long tool-use payloads may exceed but rarely 4KB.
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)

    def emit_progress(self, stage: str, pct: float, msg: str = "") -> None:
        """Convenience: emit a STAGE_PROGRESS event."""
        self.emit(stage, EVENT_STAGE_PROGRESS, {"pct": round(pct, 2), "msg": msg})

    def emit_log(self, stage: str, level: str, msg: str) -> None:
        """Convenience: emit a STAGE_LOG_LINE event."""
        self.emit(stage, EVENT_STAGE_LOG_LINE, {"level": level, "msg": msg})

    def emit_metric(self, stage: str, name: str, value: float, **extra: Any) -> None:
        """Convenience: emit a STAGE_METRIC event."""
        payload = {"name": name, "value": value}
        payload.update(extra)
        self.emit(stage, EVENT_STAGE_METRIC, payload)

    def append_raw_line(self, line: str) -> None:
        """Append a pre-serialized JSON event line verbatim (no parsing,
        no re-stamping).

        Used by the streaming layer's cache-hit replay so the original
        capture's events keep their original timestamps when brought
        forward into a resume run's ``events.jsonl``. Chronological
        ordering across runs depends on those original timestamps.

        Line should be a single complete event JSON. A trailing newline
        is added if missing. Caller is responsible for line being a
        single line (no embedded newlines).
        """
        if not line.endswith("\n"):
            line = line + "\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)


class TeeEventSink:
    """EventSink-compatible adapter that forwards each emit to N sinks.

    Used by ``invoke_agent_streaming`` so live agent events go to BOTH
    the run-level ``events.jsonl`` (for the GUI tail + the bundle's
    execution_trace) AND a per-step ``{step}.events.jsonl`` in the case
    dir (so future resumes can replay them on cache-hit — the persisted
    half of the cache-hit-replay loop, a.k.a. "Option 2").

    Quacks like ``EventSink``: same ``emit*`` methods, exposes
    ``.path`` (the run-level sink's path; some call sites read it).
    """

    def __init__(self, *sinks: "EventSink"):
        if not sinks:
            raise ValueError("TeeEventSink requires at least one sink")
        self.sinks = sinks
        # Expose the first (run-level) sink's path; the orchestrator's
        # ``_collect_execution_trace`` reads this to locate the run dir.
        self.path = sinks[0].path

    def emit(self, stage: str, type_: str, payload: dict[str, Any] | None = None) -> None:
        for s in self.sinks:
            s.emit(stage, type_, payload)

    def emit_progress(self, stage: str, pct: float, msg: str = "") -> None:
        for s in self.sinks:
            s.emit_progress(stage, pct, msg)

    def emit_log(self, stage: str, level: str, msg: str) -> None:
        for s in self.sinks:
            s.emit_log(stage, level, msg)

    def emit_metric(self, stage: str, name: str, value: float, **extra: Any) -> None:
        for s in self.sinks:
            s.emit_metric(stage, name, value, **extra)

    def append_raw_line(self, line: str) -> None:
        for s in self.sinks:
            s.append_raw_line(line)


def tail_events(path: Path | str, since_offset: int = 0) -> tuple[list[dict], int]:
    """Read new events from ``path`` since the given byte offset.

    Returns ``(new_events, new_offset)`` — the new_offset can be passed back
    in the next call to continue tailing.

    Used by the Streamlit UI to incrementally show events as they're written.
    """
    path = Path(path)
    if not path.exists():
        return ([], 0)
    new_events: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        f.seek(since_offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                new_events.append(json.loads(line))
            except json.JSONDecodeError:
                # Skip malformed lines (partial writes mid-rotate, etc.)
                continue
        new_offset = f.tell()
    return (new_events, new_offset)


def read_all_events(path: Path | str) -> list[dict]:
    """Read every event from ``path`` (no offset bookkeeping). Returns list."""
    events, _ = tail_events(path, since_offset=0)
    return events


def iter_events(path: Path | str) -> Iterator[dict]:
    """Generator variant of ``read_all_events`` — yields events one at a time."""
    path = Path(path)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
