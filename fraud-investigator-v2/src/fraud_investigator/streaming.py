"""Streaming variant of ``invoke_agent`` — forwards live events to an EventSink.

The existing ``claude_p.invoke_agent`` is one-shot: spawn ``claude -p
--output-format json``, wait for completion, parse the final wrapper JSON.
That's fine for headless orchestration but gives no UI visibility into
what the agent is doing while it runs.

``invoke_agent_streaming`` does the same job but:
  - Uses ``--output-format stream-json --verbose`` so claude emits per-turn
    JSON events to stdout as they happen
  - Reads stdout line-by-line in a background thread
  - For each event, forwards a corresponding event to the provided
    EventSink (STAGE_TOOL_USE, STAGE_TEXT_TURN, STAGE_TOOL_RESULT, etc.)
  - At completion, extracts the final assistant text from the "result"
    event and validates against the caller's Pydantic schema (same retry
    logic as invoke_agent)
  - Returns the validated model

This lets the Streamlit UI render tool calls + assistant text turns as
they happen, while preserving the structured-output contract.

Used by:
  - scripts/orchestrate_pipeline.py — wraps Stage 1 (rule_derivation)
    and Stage 3 (triage) agents
  - fraud_investigator.orchestrator.Orchestrator (when event_sink is
    provided) — wraps the Stage 4 investigator/counsel/defense/writer
    agents
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .claude_p import (
    DEFAULT_CLAUDE_BIN,
    DEFAULT_HEADLESS_ARGS,
    DEFAULT_TIMEOUT_SEC,
    _apply_safe_perms,
    DISPLAY_PARSED_JSON,
    MAX_PARSE_RETRIES,
    is_transient_api_failure,
    is_hard_stop_failure,
    transient_backoff_sec,
    TURN_BUDGET_BY_STEP,
    _resolve_effort_for_step,
    DEFAULT_TURN_BUDGET,
    ClaudePInvocationError,
    ClaudePHardStopError,
    _hard_stop_message,
    _DEFAULT_CWD,
    _display_parsed_json,
    _extract_response_text,
    _resolve_model_for_step,
    _retry_prompt,
    _repair_prompt,
    _finalize_prompt,
    _strip_code_fence,
    _load_cached_parsed,
    _coerce_and_persist,
    _resolve_continuation,
    _step_meta_payload,
)
from .event_sink import (
    EVENT_STAGE_PROGRESS,
    EventSink,
    TeeEventSink,
    EVENT_STAGE_LOG_LINE,
    EVENT_STAGE_TEXT_TURN,
    EVENT_STAGE_TOOL_RESULT,
    EVENT_STAGE_TOOL_USE,
)

LOG = logging.getLogger("fraud_investigator.streaming")
T = TypeVar("T", bound=BaseModel)


def _replay_step_events(step_events_path: "Path", target_sink: "EventSink") -> int:
    """Replay a per-step events.jsonl into ``target_sink`` verbatim.

    Each line is appended directly (timestamps preserved) so the resume
    run's run-level events.jsonl carries the original capture's tool
    calls / reasoning turns. The bundle's ``execution_trace[]`` then
    contains the full record across resumes without depending on the
    collector's cross-run discovery.

    Returns the count of lines replayed (0 if the file is missing or
    empty). Safe on missing files — used in the cache-hit path.
    """
    if not step_events_path.exists():
        return 0
    n = 0
    try:
        for line in step_events_path.read_text().splitlines():
            if line.strip():
                target_sink.append_raw_line(line)
                n += 1
    except Exception as e:
        LOG.warning(f"replay of {step_events_path.name} failed mid-stream: {e}")
    return n


# Thinking-heartbeat throttle: per-stage monotonic timestamp of the last
# emitted heartbeat. Module-level is fine — one orchestrator process runs one
# agent stage at a time, and a stray cross-run carryover only delays the
# first heartbeat by <window.
THINKING_HEARTBEAT_SEC = 15.0
_THINKING_HEARTBEAT: dict[str, float] = {}


def _forward_stream_event(
    event: dict[str, Any],
    sink: EventSink,
    stage: str,
) -> str | None:
    """Map a single claude-stream-json event to an EventSink emit.

    Returns the final assistant text if the event is a 'result' event, else None.
    """
    etype = event.get("type")

    if etype == "assistant":
        msg = event.get("message") or {}
        content = msg.get("content") or []
        for block in content:
            btype = block.get("type")
            if btype == "tool_use":
                sink.emit(stage, EVENT_STAGE_TOOL_USE, {
                    "name": block.get("name", ""),
                    "input": block.get("input", {}),
                    "id": block.get("id", ""),
                })
            elif btype == "text":
                text = block.get("text", "")
                if text.strip():
                    sink.emit(stage, EVENT_STAGE_TEXT_TURN, {"text": text})
        return None

    if etype == "user":
        msg = event.get("message") or {}
        content = msg.get("content") or []
        for block in content:
            if block.get("type") == "tool_result":
                payload_content = block.get("content", "")
                # Tool results can be string or list-of-blocks
                if isinstance(payload_content, list):
                    payload_content = "\n".join(
                        b.get("text", "") if isinstance(b, dict) else str(b)
                        for b in payload_content
                    )
                # Truncate very large tool results so the UI doesn't choke
                if isinstance(payload_content, str) and len(payload_content) > 4096:
                    payload_content = payload_content[:4096] + f"... (truncated; {len(payload_content)} bytes total)"
                sink.emit(stage, EVENT_STAGE_TOOL_RESULT, {
                    "tool_use_id": block.get("tool_use_id", ""),
                    "content": payload_content,
                })
        return None

    if etype == "result":
        # Final wrapper event — contains the agent's full assistant text
        # in the "result" field.
        result_text = event.get("result", "")
        if not result_text and "message" in event:
            # Older protocol: result text in event["message"]["content"][...]["text"]
            msg = event.get("message") or {}
            for block in msg.get("content") or []:
                if block.get("type") == "text":
                    result_text += block.get("text", "")
        return result_text or None

    if etype == "system":
        # init/start events — emit as LOG_LINE for diagnostic context.
        # EXCEPTION: thinking_tokens fires once per extended-thinking CHUNK —
        # on an xhigh agent that's hundreds of identical lines per step,
        # which (a) clogged the console's live watch and (b) was the bulk of
        # events.jsonl (re-parsed every 2.5s poll). Throttle to one
        # STAGE_PROGRESS heartbeat per window: liveness preserved, noise gone.
        subtype = event.get("subtype", "")
        if subtype == "thinking_tokens":
            now = time.monotonic()
            last = _THINKING_HEARTBEAT.get(stage)
            # First chunk for a stage ALWAYS emits (last is None) — a 0.0
            # default would suppress early heartbeats when the monotonic
            # clock is small (fresh boot); caught by the unit test.
            if last is None or now - last >= THINKING_HEARTBEAT_SEC:
                _THINKING_HEARTBEAT[stage] = now
                sink.emit(stage, EVENT_STAGE_PROGRESS, {"kind": "thinking"})
            return None
        if subtype:
            sink.emit(stage, EVENT_STAGE_LOG_LINE, {
                "level": "info",
                "msg": f"system/{subtype}",
            })
        return None

    return None


def _stream_reader_thread(
    proc: subprocess.Popen,
    sink: EventSink,
    stage: str,
    raw_lines: list[str],
    result_text_holder: list[str],
) -> None:
    """Background thread: read proc.stdout line-by-line, forward events."""
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip("\n\r")
            if not line:
                continue
            raw_lines.append(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # Non-JSON line; capture as a log entry
                sink.emit(stage, EVENT_STAGE_LOG_LINE, {
                    "level": "warn",
                    "msg": f"non-json stdout: {line[:200]}",
                })
                continue
            text = _forward_stream_event(event, sink, stage)
            if text is not None:
                result_text_holder.append(text)
    except Exception as e:
        sink.emit(stage, EVENT_STAGE_LOG_LINE, {
            "level": "error",
            "msg": f"stream reader exception: {e!r}",
        })


# Stall guard (T2a) — a silently-hung child (no stream event at all, e.g. a
# wedged tool call or a dropped connection claude -p never surfaces as an
# error) previously ran until the wall-clock ``timeout_sec`` (which can be
# large, or effectively unbounded for calibration's extended timeouts) before
# the retry ladder ever got a chance to respawn it. This adds an INDEPENDENT,
# much shorter liveness check: no new stream-json line for
# ``FRAUD_INV_STREAM_STALL_S`` seconds (default 480 = 8min) while the child is
# still alive -> kill it and classify the attempt as ``stall_timeout`` so the
# EXISTING retry ladder (below) respawns it, same as any other recoverable
# failure. Does not otherwise change retry semantics or the timeout_sec path.
STREAM_STALL_ENV = "FRAUD_INV_STREAM_STALL_S"
DEFAULT_STREAM_STALL_SEC = 480.0


def _stall_watcher_thread(
    proc: subprocess.Popen,
    raw_lines: list[str],
    stall_sec: float,
    stalled: list[bool],
    stop_event: threading.Event,
) -> None:
    """Background thread: kill ``proc`` if ``raw_lines`` hasn't grown (i.e. no
    stream event arrived) for ``stall_sec`` seconds while the child is still
    alive. Sets ``stalled[0] = True`` so the caller can distinguish this from
    a normal exit or the existing wall-clock ``TimeoutExpired`` path. Polls
    ``raw_lines`` from the OUTSIDE (never touches ``_stream_reader_thread``'s
    signature/contract) so it works unmodified against any reader — real or
    test double. Stops as soon as ``stop_event`` is set (the caller's
    ``finally`` block, once this attempt is done one way or another).
    """
    poll_sec = max(0.05, min(5.0, stall_sec / 4))
    last_len = len(raw_lines)
    last_change = time.monotonic()
    while not stop_event.wait(poll_sec):
        try:
            alive = proc.poll() is None
        except Exception:
            alive = True   # can't tell — err toward still-watching, not toward a false stall
        if not alive:
            return
        n = len(raw_lines)
        if n != last_len:
            last_len = n
            last_change = time.monotonic()
            continue
        if time.monotonic() - last_change >= stall_sec:
            stalled[0] = True
            try:
                proc.kill()
            except Exception:
                pass
            return


def invoke_agent_streaming(
    *,
    prompt: str,
    schema: type[T] | None,
    out_dir: Path,
    step_name: str,
    event_sink: EventSink,
    stage_name: str,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    claude_bin: str = DEFAULT_CLAUDE_BIN,
    extra_args: list[str] | None = None,
    session_id: str | None = None,
    is_continuation: bool = False,
) -> T:
    """Invoke ``claude -p`` with streaming events forwarded to ``event_sink``.

    Same API contract as ``claude_p.invoke_agent`` except:
      - Forwards live tool-use / text-turn / tool-result events to ``event_sink``
        under the given ``stage_name`` (e.g., "rule_derivation").
      - Persists prompt, raw stream lines, parsed model, and meta to ``out_dir``.
      - Retries on parse/validation errors up to ``MAX_PARSE_RETRIES``.

    Parameters
    ----------
    prompt
        Rendered prompt text (will be passed as stdin to claude -p).
    schema
        Pydantic model to validate the final agent output against.
    out_dir
        Directory to persist prompt.md, raw.jsonl (stream lines), parsed.json,
        and meta.json. Created if missing.
    step_name
        Logical step name, e.g., "rule_derivation" or "triage_batch_07".
        Used for filenames, model selection (TURN_BUDGET_BY_STEP), and meta.
    event_sink
        Where to emit STAGE_* events as they happen.
    stage_name
        Canonical pipeline stage for the event payloads
        (one of ``rule_derivation``, ``ml_training``, ``triage``,
        ``investigator``, or ``agent_<name>`` for supporting agents).
    timeout_sec
        Per-invocation timeout. Defaults to ``DEFAULT_TIMEOUT_SEC``.
    claude_bin
        Path to claude binary; defaults to PATH resolution.
    extra_args
        Additional flags appended to the claude command line. Common uses:
        ``["--disallowedTools", "WebSearch,WebFetch"]`` (Stage 3 triage).

    Returns
    -------
    Validated Pydantic model instance of type ``T``.
    """
    from . import claude_p as _cp
    if _cp.EXEC_BACKEND == "api":
        # Provider-agnostic backend; events still flow to the sink.
        from . import api_backend
        return api_backend.run_step(
            prompt=prompt, schema=schema, out_dir=out_dir, step_name=step_name,
            timeout_sec=timeout_sec, extra_args=extra_args, session_id=session_id,
            is_continuation=is_continuation, event_sink=event_sink, stage_name=stage_name,
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = out_dir / f"{step_name}.prompt.md"
    raw_path = out_dir / f"{step_name}.raw.jsonl"
    stderr_path = out_dir / f"{step_name}.stderr.log"
    parsed_path = out_dir / f"{step_name}.parsed.json"
    # Per-step events file lives next to parsed.json in the case dir.
    # On fresh agent invocation, every STAGE_* event for this step is
    # tee'd here (in addition to the run-level events.jsonl) so future
    # resume-runs can replay these events on cache-hit. Persists the
    # full tool-by-tool record alongside the agent's structured output
    # — "Option 2" in the bundle-completeness design.
    step_events_path = out_dir / f"{step_name}.events.jsonl"

    # ── Checkpoint resume ─────────────────────────────────────────────────
    # If a prior run wrote out a valid parsed.json for this exact step,
    # load it instead of re-invoking claude -p. Saves time + cost on
    # resume of a failed run. Schema-validates the loaded text so a
    # corrupted file falls through to a normal claude -p call.
    cached = _load_cached_parsed(schema, parsed_path)
    if cached is not None:
        LOG.info(
            f"[{step_name}] checkpoint hit — loaded existing "
            f"{parsed_path.relative_to(parsed_path.parents[2]) if len(parsed_path.parents) >= 2 else parsed_path.name}, "
            f"skipping claude -p call"
        )
        # Replay the original capture's events into the resume run's
        # events.jsonl so the bundle's execution_trace stays whole.
        # Otherwise cache-loaded steps look silent in the bundle
        # (the bug PR #189 worked around at the collector level by
        # cross-linking runs; here we close it at the source).
        n_replayed = _replay_step_events(step_events_path, event_sink)
        if n_replayed > 0:
            LOG.info(
                f"[{step_name}] replayed {n_replayed} cached events "
                f"from {step_events_path.name} into run-level sink"
            )
        # Emit a synthetic complete event so the Firm/Active-Run pages
        # still tick the step forward visually on resume.
        event_sink.emit(stage_name, EVENT_STAGE_LOG_LINE, {
            "level": "info",
            "msg": f"[{step_name}] resumed from checkpoint",
            "checkpoint": True,
            "replayed_events": n_replayed,
        })
        return cached
    meta_path = out_dir / f"{step_name}.meta.json"

    # Pre-flight: if the caller said "fresh session" (is_continuation=False)
    # but a session file already exists on disk for this deterministic UUID,
    # upgrade to is_continuation=True. Handles the orchestrator-restart case
    # where the same case_id + role would otherwise re-create a session that
    # claude-code already knows about (→ "Session ID is already in use").
    is_continuation = _resolve_continuation(session_id, is_continuation, step_name)

    current_prompt = prompt
    attempts: list[dict] = []

    # Fresh agent invocation — truncate any stale per-step events file
    # from a prior run whose parsed.json failed validation (we're about
    # to overwrite that parsed.json, so the prior events are stale too).
    # On retries WITHIN this invocation, events accumulate naturally
    # (the tee is shared across attempts).
    try:
        if step_events_path.exists():
            step_events_path.unlink()
    except Exception as e:
        LOG.warning(f"[{step_name}] could not clear stale {step_events_path.name}: {e}")
    # Build the tee: every stream event goes to BOTH the run-level
    # events.jsonl (for live UI tail + bundle execution_trace) AND
    # the per-step events.jsonl in the case dir (for future cache-hit
    # replay — Option 2).
    step_sink = EventSink(step_events_path)
    tee_sink = TeeEventSink(event_sink, step_sink)

    for attempt in range(1, MAX_PARSE_RETRIES + 1):
        prompt_path.write_text(current_prompt)

        cmd = [claude_bin, "-p", "--output-format", "stream-json", "--verbose"] + list(DEFAULT_HEADLESS_ARGS)
        max_turns = TURN_BUDGET_BY_STEP.get(step_name, DEFAULT_TURN_BUDGET)
        cmd.extend(["--max-turns", str(max_turns)])
        chosen_model = _resolve_model_for_step(step_name)
        if chosen_model:
            cmd.extend(["--model", chosen_model])
        chosen_effort = _resolve_effort_for_step(step_name)
        if chosen_effort:
            cmd.extend(["--effort", chosen_effort])
        # Session continuation — see claude_p.invoke_agent for the rationale.
        #
        # Retry resumption: on attempt 2+, the session was already created
        # during attempt 1 (claude-p registers the session as soon as
        # --session-id is consumed, even if the subprocess later exits 1).
        # If we keep passing --session-id on retries, Anthropic responds
        # "Session ID is already in use" and the retry fails before the
        # model even runs. Switch to --resume on attempt 2+ so the prior
        # turn history is honored and the retry actually executes.
        effective_continuation = is_continuation or attempt > 1
        if session_id:
            if effective_continuation:
                cmd.extend(["--resume", session_id])
            else:
                cmd.extend(["--session-id", session_id])
        if extra_args:
            cmd.extend(extra_args)

        # SAFE-PERMS production mode (default-mode + broad allowlist) so a
        # streamed production run is launchable from inside a Claude Code
        # session. No-op unless FRAUD_INV_SAFE_PERMS=1. Calibration owns its own
        # flags and does not use this streaming path, so no guard is needed here.
        cmd = _apply_safe_perms(cmd)

        model_tag = f"model={chosen_model}" if chosen_model else "model=<inherit>"
        effort_tag = f"effort={chosen_effort}" if chosen_effort else "effort=<inherit>"
        session_tag = (
            f"resume={session_id[:8]}" if (session_id and effective_continuation)
            else f"new-session={session_id[:8]}" if session_id
            else "no-session"
        )
        LOG.info(f"[{step_name}] streaming attempt {attempt}/{MAX_PARSE_RETRIES} "
                 f"({model_tag}, {effort_tag}, {session_tag}, max_turns={max_turns}) "
                 f"— spawning: {' '.join(cmd)}")

        started = datetime.utcnow()
        effective_cwd = _DEFAULT_CWD

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(effective_cwd) if effective_cwd else None,
            )
        except FileNotFoundError as e:
            raise ClaudePInvocationError(f"[{step_name}] claude binary not found: {claude_bin}") from e

        # Feed the prompt then close stdin so claude knows we're done
        assert proc.stdin is not None
        try:
            proc.stdin.write(current_prompt)
            proc.stdin.close()
        except BrokenPipeError:
            pass

        # Spawn reader thread to forward events to the tee (which
        # writes to both the run-level events.jsonl and the per-step
        # events.jsonl in the case dir).
        raw_lines: list[str] = []
        result_text_holder: list[str] = []
        reader = threading.Thread(
            target=_stream_reader_thread,
            args=(proc, tee_sink, stage_name, raw_lines, result_text_holder),
            daemon=True,
        )
        reader.start()

        # Stall guard (T2a) — independent of timeout_sec; see _stall_watcher_thread.
        stall_sec = float(os.environ.get(STREAM_STALL_ENV, DEFAULT_STREAM_STALL_SEC))
        stalled = [False]
        stall_stop = threading.Event()
        stall_watcher = threading.Thread(
            target=_stall_watcher_thread,
            args=(proc, raw_lines, stall_sec, stalled, stall_stop),
            daemon=True,
        )
        stall_watcher.start()

        # stderr MUST be drained on a thread. A bare `proc.stderr.read()` here
        # blocks to EOF — i.e. until the child exits — so the
        # `proc.wait(timeout=timeout_sec)` below could never fire and
        # timeout_sec was DEAD CODE. Measured 2026-08-02 (CALIBRATION-B3C661FF,
        # adv-r2/legal_adv_response, timeout_sec=1800): attempt 1 ran 15,603s
        # (4.3h) and attempt 2 ran 12,836s (3.6h), both returning rc=1
        # "transient API/network" rather than wall_clock_timeout; attempt 3 then
        # did the same work in 200s. 7.9h burned on a 30-minute budget. The
        # output-based stall guard (T2a) cannot catch this because `claude`
        # keeps emitting stream events while it retries internally.
        stderr_chunks: list[str] = []

        def _drain_stderr() -> None:
            try:
                stderr_chunks.append(proc.stderr.read() if proc.stderr else "")
            except Exception:                      # pipe closed by kill()
                pass

        stderr_reader = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_reader.start()

        try:
            exit_code = proc.wait(timeout=timeout_sec)
            stderr_reader.join(timeout=5)
            stderr_text = "".join(stderr_chunks)
        except subprocess.TimeoutExpired:
            proc.kill()
            reader.join(timeout=5)
            stderr_reader.join(timeout=5)
            stderr_text = "".join(stderr_chunks)
            # Audit (no-loss): persist the timed-out attempt's partial stream
            # (incl. whatever tool calls it made before the kill).
            (out_dir / f"{step_name}.raw.attempt{attempt}.jsonl").write_text(
                "\n".join(raw_lines) + "\n" if raw_lines else "")
            err = f"timeout after {timeout_sec}s"
            rec = {"attempt": attempt, "started": started.isoformat(), "error": err}
            # A wall-clock timeout means the agent was still working (extended
            # thinking + many tool turns), NOT a bad prompt. Mirror invoke_agent:
            # resume the SAME session once and tell it to stop exploring and emit
            # its final JSON from work-so-far, rather than failing the whole run
            # (the 2026-06 ~18h-stall class). Bounded by MAX_PARSE_RETRIES; the
            # resumed attempt uses --resume (attempt > 1) so it finalizes, not restarts.
            if session_id and attempt < MAX_PARSE_RETRIES:
                rec["error"] = f"{err} — resuming to finalize"
                rec["retry_reason"] = "wall_clock_timeout"
                attempts.append(rec)
                meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
                LOG.warning(f"[{step_name}] {err}; resuming session to finalize with "
                            f"work-so-far (attempt {attempt} → {attempt + 1})")
                current_prompt = _finalize_prompt()
                continue  # `finally` joins the reader thread before the next attempt
            attempts.append(rec)
            meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
            raise ClaudePInvocationError(f"[{step_name}] {err}")
        finally:
            stall_stop.set()
            stall_watcher.join(timeout=5)
            reader.join(timeout=10)

        finished = datetime.utcnow()
        stderr_path.write_text(stderr_text or "")
        stream_text = "\n".join(raw_lines) + "\n" if raw_lines else ""
        raw_path.write_text(stream_text)
        # Audit (no-loss): preserve EVERY attempt's stream — including all tool
        # calls — not just the last. raw_path keeps the final attempt for
        # back-compat; the per-attempt files are the lossless record (a retry
        # would otherwise overwrite a tool-bearing earlier attempt).
        (out_dir / f"{step_name}.raw.attempt{attempt}.jsonl").write_text(stream_text)

        attempt_record = {
            "attempt": attempt,
            "started": started.isoformat(),
            "finished": finished.isoformat(),
            "duration_sec": (finished - started).total_seconds(),
            "exit_code": exit_code,
            "n_stream_lines": len(raw_lines),
            "stderr_bytes": len(stderr_text or ""),
            # Resolved routing (parity with the plain invoke path's meta.json).
            "model": chosen_model,
            "effort": chosen_effort,
        }

        # Stall guard (T2a) — checked BEFORE the generic exit-code handling
        # below so a stall-induced kill (SIGKILL -> some nonzero/negative
        # exit_code) is classified correctly instead of falling into a
        # misleading generic-failure or hard-stop branch. Same retry ladder
        # as every other recoverable branch: append + continue while attempts
        # remain, else write meta + raise once exhausted.
        if stalled[0]:
            attempt_record["error"] = f"stall_timeout: no stream event for >={stall_sec:.0f}s while the child was alive"
            attempt_record["retry_reason"] = "stall_timeout"
            attempts.append(attempt_record)
            if attempt < MAX_PARSE_RETRIES:
                meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
                LOG.warning(f"[{step_name}] stalled (no stream event for >={stall_sec:.0f}s) — "
                            f"killed + retrying (attempt {attempt + 1}/{MAX_PARSE_RETRIES})")
                current_prompt = _finalize_prompt() if session_id else prompt
                continue
            meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
            raise ClaudePInvocationError(
                f"[{step_name}] stalled: no stream event for >={stall_sec:.0f}s "
                f"(exhausted {MAX_PARSE_RETRIES} attempts)")

        if exit_code != 0:
            # Detect error_max_turns / error_during_execution from the last
            # 'result' event in the raw stream — claude -p exits 1 on these
            # but they're RECOVERABLE via a focused-retry prompt. Without
            # this branch the previous behavior raised fatally on the first
            # attempt and burned 3-5 minutes of agent work (CASE-6A0CD54F-
            # 2026 Hemlock adv-r1 rebuttal_investigator burned 261s + $0.55
            # before the un-retried max_turns abort).
            terminal_subtype = ""
            try:
                for ln in reversed(raw_lines):
                    ev = json.loads(ln)
                    if ev.get("type") == "result":
                        terminal_subtype = ev.get("subtype", "") or ""
                        break
            except Exception:
                terminal_subtype = ""

            # Hard stop (billing wall) — fail FAST, before BOTH retry branches.
            # The org-spend-limit kill surfaces as a result event with
            # subtype="error_during_execution" (which is in recoverable_subtypes
            # below) AND isn't caught by the transient detector, so without this
            # guard the run burned all 3 retries re-hitting the same cap before
            # failing (Bradley CASE-6A2D49E8-2026 adv-r3 defense, 2026-06-13).
            if is_hard_stop_failure(stderr_text, stream_text):
                attempt_record["error"] = f"exit {exit_code} (ORG_SPEND_LIMIT — hard stop)"
                attempts.append(attempt_record)
                meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
                LOG.error(_hard_stop_message(step_name))
                raise ClaudePHardStopError(_hard_stop_message(step_name))

            recoverable_subtypes = {"error_max_turns", "error_during_execution"}
            if terminal_subtype in recoverable_subtypes and attempt < MAX_PARSE_RETRIES:
                attempt_record["error"] = f"exit {exit_code} ({terminal_subtype})"
                attempt_record["retry_reason"] = terminal_subtype
                attempts.append(attempt_record)
                LOG.warning(
                    f"[{step_name}] claude -p exited {exit_code} with "
                    f"terminal_subtype={terminal_subtype!r} — retrying with a "
                    f"focused prompt (attempt {attempt+1}/{MAX_PARSE_RETRIES})"
                )
                current_prompt = _retry_prompt(
                    prompt,
                    f"The previous attempt terminated with {terminal_subtype} "
                    "(ran out of tool-use turns before producing the required "
                    "structured output). Skip exploratory searches; use the "
                    "evidence already in your prior reasoning to produce the "
                    "structured output directly. Do not start new web searches "
                    "or filesystem probes unless strictly necessary to fill a "
                    "named schema field."
                )
                continue

            # Transient API/network failure (socket drop, overloaded, rate-limit):
            # retryable by resuming the session after a backoff — NOT a fatal
            # error. (Maple 2026-06-12: a counsel_rebuttal socket-close
            # hard-raised and lost ~1h40m. The session resumes on attempt>1.)
            if (is_transient_api_failure(stderr_text, stream_text, terminal_subtype)
                    and attempt < MAX_PARSE_RETRIES):
                wait = transient_backoff_sec(attempt)
                attempt_record["error"] = f"exit {exit_code} (transient API/network)"
                attempt_record["retry_reason"] = "transient_api_failure"
                attempts.append(attempt_record)
                meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
                LOG.warning(
                    f"[{step_name}] claude -p exited {exit_code} on a TRANSIENT "
                    f"API/network failure — resuming the session in {wait:.0f}s "
                    f"(attempt {attempt+1}/{MAX_PARSE_RETRIES}); no work lost")
                time.sleep(wait)
                current_prompt = prompt  # resume the same ask; session carries state
                continue

            attempt_record["error"] = f"exit {exit_code} ({terminal_subtype})" if terminal_subtype else f"exit {exit_code}"
            attempts.append(attempt_record)
            meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
            preview = (stderr_text or "")[:500]
            raise ClaudePInvocationError(
                f"[{step_name}] claude -p exited {exit_code}"
                + (f" (terminal={terminal_subtype})" if terminal_subtype else "")
                + f"\nstderr preview:\n{preview}"
            )

        if not result_text_holder:
            attempt_record["error"] = "no 'result' event in stream"
            attempts.append(attempt_record)
            current_prompt = _retry_prompt(prompt, "expected final 'result' event in stream-json output but received none")
            continue

        result_text = result_text_holder[-1]   # last 'result' event
        clean = _strip_code_fence(result_text)
        # Coerce + persist. Free-text (schema=None) returns the raw markdown
        # document; otherwise validate. The schema=None branch is centralized in
        # _coerce_and_persist (shared with claude_p.invoke_agent) so it cannot
        # drift between the two paths — the #316/#318 gaps came from that drift.
        try:
            result = _coerce_and_persist(clean, schema, parsed_path)
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            attempt_record["error"] = f"parse/validate: {e}"
            attempts.append(attempt_record)
            # Surgical repair (parity with invoke_agent): re-prompt with the model's
            # OWN prior output + the exact errors so it fixes only the bad fields,
            # rather than re-running the whole task (which reproduces the omission).
            schema_name = getattr(schema, "__name__", "the expected") if schema else "the expected"
            current_prompt = _repair_prompt(
                clean,
                error_msg=f"Your previous JSON failed validation against the "
                          f"{schema_name} schema:\n\n{str(e)[:1500]}")
            continue

        # Success (free-text document or validated model).
        attempt_record["success"] = True
        attempts.append(attempt_record)
        meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
        if schema is None:
            return result
        if DISPLAY_PARSED_JSON:
            _display_parsed_json(step_name, result, attempt_record["duration_sec"])
        return result

    # All retries exhausted
    meta_path.write_text(json.dumps(_step_meta_payload(step_name, attempts), indent=2))
    raise ClaudePInvocationError(
        f"[{step_name}] exhausted {MAX_PARSE_RETRIES} parse-retry attempts; see {meta_path}"
    )
