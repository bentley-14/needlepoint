"""Invocation-harness contract — the seam every agent step runs through.

Mocks the subprocess boundary (``subprocess.run`` for ``invoke_agent``;
``Popen`` + the reader thread for ``invoke_agent_streaming``) so we can pin the
parse / cache / retry / schema behavior WITHOUT spawning ``claude -p``.

Why this file exists: two gaps in the ``schema=None`` free-text mode slipped to
production this session because there was no test net here —

  • PR #316: ``case_writer`` JSON-wrapped a large prose doc; sonnet emitted bare
    markdown → 3 parse-retries failed → the run died ``agent_error``. Fix was
    ``invoke_agent(schema=None)`` free-text mode.
  • PR #318: the FOLLOW-ON gap — the resume **cache-read** path and the
    **streaming** path still assumed a model (``schema.model_validate_json`` on
    ``None``), so a writer-only ``--case-id`` resume crashed.

The cases below are the regression net for both: free-text return, the
cache-read SKIP for ``schema=None`` (re-run, don't reuse a stale doc), schema
validation, the cache HIT for a real schema, parse-retry, and **parity**
between the plain and streaming paths.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time

import pytest
from pydantic import BaseModel

import fraud_investigator.claude_p as claude_p


class _Mini(BaseModel):
    x: int


# ── test doubles ──────────────────────────────────────────────────────────

def _wrapper(text: str, *, key: str = "result") -> str:
    """The ``claude -p --output-format json`` stdout wrapper. The agent's
    response text lives under ``result`` (or an alternate key some versions
    use), which ``_extract_response_text`` digs out."""
    return json.dumps({key: text})


def _completed(stdout: str, *, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeRun:
    """Stand-in for ``subprocess.run``: returns the configured ``CompletedProcess``
    for each successive call (one per attempt). A call past the configured list
    raises — so an unexpected extra spawn fails loudly instead of looping."""

    def __init__(self, *results: subprocess.CompletedProcess):
        self._results = list(results)
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls > len(self._results):
            raise AssertionError(
                f"subprocess.run called {self.calls}× but only "
                f"{len(self._results)} result(s) configured")
        return self._results[self.calls - 1]


class _ExplodingRun:
    """``subprocess.run`` that must never be called (cache-hit assertions)."""

    def __call__(self, *args, **kwargs):  # pragma: no cover — failure mode
        raise AssertionError("subprocess.run was called but a cache hit was expected")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Keep the plain path on the plain path: no event sink (else it routes to
    streaming), the default exec backend (not tmux), and no calibration context."""
    monkeypatch.setattr(claude_p, "_EVENT_SINK", None)
    monkeypatch.setattr(claude_p, "EXEC_BACKEND", "claude_p")
    import fraud_investigator.calibration as _cal
    monkeypatch.setattr(_cal, "get_calibration_context", lambda: None)


def _run_plain(monkeypatch, fake, *, prompt="irrelevant", schema, out_dir, step="mystep", **kw):
    monkeypatch.setattr(claude_p.subprocess, "run", fake)
    return claude_p.invoke_agent(
        prompt=prompt, schema=schema, out_dir=out_dir, step_name=step,
        claude_bin="/nonexistent/claude-must-never-spawn-for-real", **kw)


# ── plain path: schema=None free-text mode (PR #316) ────────────────────────

def test_schema_none_returns_raw_text(tmp_path, monkeypatch):
    doc = "# Case Report\n\nThe provider billed 99213 at impossible volume.\n"
    fake = _FakeRun(_completed(_wrapper(doc)))
    out = _run_plain(monkeypatch, fake, schema=None, out_dir=tmp_path)

    assert isinstance(out, str)
    assert out == doc.strip()
    # The deliverable is persisted as RAW text, not a JSON model dump.
    assert (tmp_path / "mystep.parsed.json").read_text() == doc.strip()
    assert fake.calls == 1


def test_schema_none_accepts_alternate_wrapper_key(tmp_path, monkeypatch):
    # Some claude -p versions return the text under 'response' instead of 'result'.
    doc = "## Layperson Summary\n\nPlain-English explanation here."
    fake = _FakeRun(_completed(_wrapper(doc, key="response")))
    out = _run_plain(monkeypatch, fake, schema=None, out_dir=tmp_path)
    assert out == doc


def test_schema_none_skips_stale_cache(tmp_path, monkeypatch):
    """THE PR #318 bug: a free-text step must NOT reuse a prior parsed.json.

    The cache-read is JSON-model-shaped; for schema=None the cached file is raw
    text and re-running is cheap, so the read is guarded by ``schema is not
    None``. If that guard regresses, this returns the stale doc (and would have
    crashed on ``None.model_validate_json`` before #318)."""
    (tmp_path / "mystep.parsed.json").write_text("STALE prior document")
    fresh = "# Fresh Report\n\nRegenerated on resume.\n"
    fake = _FakeRun(_completed(_wrapper(fresh)))
    out = _run_plain(monkeypatch, fake, schema=None, out_dir=tmp_path)

    assert out == fresh.strip()
    assert fake.calls == 1  # spawned — did not short-circuit on the stale cache
    assert (tmp_path / "mystep.parsed.json").read_text() == fresh.strip()


# ── plain path: schema mode ─────────────────────────────────────────────────

def test_schema_validates_inner_json(tmp_path, monkeypatch):
    fake = _FakeRun(_completed(_wrapper('{"x": 7}')))
    out = _run_plain(monkeypatch, fake, schema=_Mini, out_dir=tmp_path)
    assert isinstance(out, _Mini)
    assert out.x == 7
    # Persisted as the model's canonical dump (not the raw wrapper).
    assert json.loads((tmp_path / "mystep.parsed.json").read_text()) == {"x": 7}


def test_schema_strips_code_fence(tmp_path, monkeypatch):
    fenced = "Here is the JSON:\n```json\n{\"x\": 3}\n```\nthanks"
    fake = _FakeRun(_completed(_wrapper(fenced)))
    out = _run_plain(monkeypatch, fake, schema=_Mini, out_dir=tmp_path)
    assert out.x == 3


def test_cache_hit_returns_without_spawning(tmp_path, monkeypatch):
    (tmp_path / "mystep.parsed.json").write_text(json.dumps({"x": 42}))
    out = _run_plain(monkeypatch, _ExplodingRun(), schema=_Mini, out_dir=tmp_path)
    assert out.x == 42  # served from cache; _ExplodingRun would have raised


def test_cache_bypass_env_forces_respawn(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAUD_INV_NO_CACHE_LOAD", "1")
    (tmp_path / "mystep.parsed.json").write_text(json.dumps({"x": 1}))
    fake = _FakeRun(_completed(_wrapper('{"x": 99}')))
    out = _run_plain(monkeypatch, fake, schema=_Mini, out_dir=tmp_path)
    assert out.x == 99 and fake.calls == 1  # ignored the cache, re-ran


def test_parse_retry_on_invalid_then_valid(tmp_path, monkeypatch):
    fake = _FakeRun(
        _completed(_wrapper("not json at all")),   # attempt 1 → validation fail
        _completed(_wrapper('{"x": 5}')),          # attempt 2 → valid
    )
    out = _run_plain(monkeypatch, fake, schema=_Mini, out_dir=tmp_path)
    assert out.x == 5 and fake.calls == 2


def test_exhausted_retries_raise(tmp_path, monkeypatch):
    bad = [_completed(_wrapper("nope")) for _ in range(claude_p.MAX_PARSE_RETRIES)]
    fake = _FakeRun(*bad)
    with pytest.raises(claude_p.ClaudePInvocationError):
        _run_plain(monkeypatch, fake, schema=_Mini, out_dir=tmp_path)
    assert fake.calls == claude_p.MAX_PARSE_RETRIES


def test_nonzero_exit_raises(tmp_path, monkeypatch):
    fake = _FakeRun(_completed("", returncode=1, stderr="command not found"))
    with pytest.raises(claude_p.ClaudePInvocationError):
        _run_plain(monkeypatch, fake, schema=_Mini, out_dir=tmp_path)


# ── streaming path: parity (PR #318 closed the same gap here) ───────────────
# The streaming path spawns via Popen + a reader thread. We stub the reader to
# deposit a result directly (the event-forwarding internals are covered by
# test_event_sink_stage_coverage / test_thinking_heartbeat) and give Popen a
# minimal fake, so these tests exercise the cache / schema / return logic only.

class _FakeStdin:
    def write(self, s): pass
    def close(self): pass


class _FakeStderr:
    def read(self): return ""


class _FakePopen:
    def __init__(self, *a, **k):
        self.stdin = _FakeStdin()
        self.stderr = _FakeStderr()
        self.stdout = iter(())  # reader thread is stubbed; never iterated

    def wait(self, timeout=None): return 0
    def kill(self): pass  # pragma: no cover


def _stub_reader(result_text: str):
    def _reader(proc, sink, stage, raw_lines, result_text_holder):
        raw_lines.append('{"type":"result"}')
        result_text_holder.append(result_text)
    return _reader


def _event_sink(out_dir):
    # The production run-level sink (TeeEventSink reads sinks[0].path).
    from fraud_investigator.event_sink import EventSink
    out_dir.mkdir(parents=True, exist_ok=True)
    return EventSink(out_dir / "events.jsonl")


def _run_streaming(monkeypatch, *, result_text, schema, out_dir, step="mystep"):
    import fraud_investigator.streaming as streaming
    monkeypatch.setattr(streaming.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(streaming, "_stream_reader_thread", _stub_reader(result_text))
    return streaming.invoke_agent_streaming(
        prompt="irrelevant", schema=schema, out_dir=out_dir, step_name=step,
        event_sink=_event_sink(out_dir), stage_name="investigator",
        claude_bin="/nonexistent/claude-must-never-spawn-for-real")


def test_streaming_schema_none_returns_raw_text(tmp_path, monkeypatch):
    doc = "# Streamed Report\n\nProse deliverable, not JSON.\n"
    out = _run_streaming(monkeypatch, result_text=doc, schema=None, out_dir=tmp_path)
    assert isinstance(out, str)
    assert out == doc.strip()
    assert (tmp_path / "mystep.parsed.json").read_text() == doc.strip()


def test_streaming_schema_none_skips_stale_cache(tmp_path, monkeypatch):
    """PR #318 streaming gap: free-text resume must re-run, not reuse the cache
    (and must not crash on ``None.model_validate_json``)."""
    (tmp_path / "mystep.parsed.json").write_text("STALE streamed doc")
    fresh = "# Fresh Streamed\n\nRegenerated.\n"
    out = _run_streaming(monkeypatch, result_text=fresh, schema=None, out_dir=tmp_path)
    assert out == fresh.strip()


def test_streaming_schema_validates(tmp_path, monkeypatch):
    out = _run_streaming(monkeypatch, result_text='{"x": 11}', schema=_Mini, out_dir=tmp_path)
    assert isinstance(out, _Mini) and out.x == 11


def test_streaming_cache_hit_returns_without_spawning(tmp_path, monkeypatch):
    import fraud_investigator.streaming as streaming
    (tmp_path / "mystep.parsed.json").write_text(json.dumps({"x": 8}))

    def _no_popen(*a, **k):  # pragma: no cover — failure mode
        raise AssertionError("Popen called but a cache hit was expected")

    monkeypatch.setattr(streaming.subprocess, "Popen", _no_popen)
    out = streaming.invoke_agent_streaming(
        prompt="irrelevant", schema=_Mini, out_dir=tmp_path, step_name="mystep",
        event_sink=_event_sink(tmp_path), stage_name="investigator",
        claude_bin="/nonexistent/claude-must-never-spawn-for-real")
    assert out.x == 8


# ── streaming reliability: timeout RESUMES to finalize (not raise) ───────────
# The ~18h-stall class: a long step must salvage on timeout, not nuke the run.

def test_streaming_timeout_resumes_to_finalize(tmp_path, monkeypatch):
    import fraud_investigator.streaming as streaming
    n = {"wait": 0}

    class _TimeoutThenOk(_FakePopen):
        def wait(self, timeout=None):
            n["wait"] += 1
            if n["wait"] == 1:
                raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
            return 0  # the resumed attempt finalizes cleanly

    monkeypatch.setattr(streaming.subprocess, "Popen", _TimeoutThenOk)
    monkeypatch.setattr(streaming, "_stream_reader_thread", _stub_reader('{"x": 7}'))
    out = streaming.invoke_agent_streaming(
        prompt="p", schema=_Mini, out_dir=tmp_path, step_name="s",
        event_sink=_event_sink(tmp_path), stage_name="investigator",
        session_id="sess-uuid",  # resume-to-finalize requires a session
        claude_bin="/nonexistent/claude-must-never-spawn-for-real")
    assert isinstance(out, _Mini) and out.x == 7
    assert n["wait"] == 2  # timed out once → resumed → finalized
    meta = json.loads((tmp_path / "s.meta.json").read_text())
    assert any(a.get("retry_reason") == "wall_clock_timeout" for a in meta["attempts"])


def test_streaming_timeout_without_session_raises(tmp_path, monkeypatch):
    # No session → can't resume → must surface the timeout (not hang).
    import fraud_investigator.streaming as streaming

    class _AlwaysTimeout(_FakePopen):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

    monkeypatch.setattr(streaming.subprocess, "Popen", _AlwaysTimeout)
    monkeypatch.setattr(streaming, "_stream_reader_thread", _stub_reader('{"x": 1}'))
    with pytest.raises(claude_p.ClaudePInvocationError):
        streaming.invoke_agent_streaming(
            prompt="p", schema=_Mini, out_dir=tmp_path, step_name="s",
            event_sink=_event_sink(tmp_path), stage_name="investigator",
            session_id=None,
            claude_bin="/nonexistent/claude-must-never-spawn-for-real")


# ── cross-path parity: same return contract ─────────────────────────────────

def test_both_paths_agree_on_schema_none_text(tmp_path, monkeypatch):
    doc = "# Identical Contract\n\nBoth paths return raw stripped text."
    plain = _run_plain(
        monkeypatch, _FakeRun(_completed(_wrapper(doc))),
        schema=None, out_dir=tmp_path / "plain")
    stream = _run_streaming(
        monkeypatch, result_text=doc, schema=None, out_dir=tmp_path / "stream")
    assert plain == stream == doc.strip()
    assert type(plain) is type(stream) is str


# ── shared kernel helpers, in isolation (1b extraction) ─────────────────────
# These three pure functions are the de-duplicated decision points both invoke
# paths now call. Pinning them directly documents the contract crisply and
# guards it independent of the call sites.

def test_load_cached_parsed_schema_none_never_loads(tmp_path):
    (tmp_path / "p.json").write_text("anything")
    assert claude_p._load_cached_parsed(None, tmp_path / "p.json") is None


def test_load_cached_parsed_valid(tmp_path):
    (tmp_path / "p.json").write_text(json.dumps({"x": 4}))
    out = claude_p._load_cached_parsed(_Mini, tmp_path / "p.json")
    assert isinstance(out, _Mini) and out.x == 4


def test_load_cached_parsed_missing_or_corrupt(tmp_path):
    assert claude_p._load_cached_parsed(_Mini, tmp_path / "absent.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert claude_p._load_cached_parsed(_Mini, tmp_path / "bad.json") is None


def test_coerce_and_persist_schema_none_writes_raw(tmp_path):
    p = tmp_path / "p.json"
    out = claude_p._coerce_and_persist("# raw doc", None, p)
    assert out == "# raw doc" and p.read_text() == "# raw doc"


def test_coerce_and_persist_schema_validates_and_dumps(tmp_path):
    p = tmp_path / "p.json"
    out = claude_p._coerce_and_persist('{"x": 6}', _Mini, p)
    assert isinstance(out, _Mini) and out.x == 6
    assert json.loads(p.read_text()) == {"x": 6}  # canonical model dump


def test_coerce_and_persist_raises_on_invalid(tmp_path):
    p = tmp_path / "p.json"
    with pytest.raises((ValueError, Exception)):
        claude_p._coerce_and_persist("not json", _Mini, p)
    assert not p.exists()  # nothing persisted on validation failure


def test_resolve_continuation(monkeypatch):
    # no session → unchanged; already-continuation → unchanged
    assert claude_p._resolve_continuation(None, False, "s") is False
    assert claude_p._resolve_continuation("uuid", True, "s") is True
    # session file present on disk → upgrade False→True
    monkeypatch.setattr(claude_p, "session_file_exists", lambda sid: True)
    assert claude_p._resolve_continuation("uuid", False, "s") is True
    monkeypatch.setattr(claude_p, "session_file_exists", lambda sid: False)
    assert claude_p._resolve_continuation("uuid", False, "s") is False


# ── meta.json carries the resolved model/effort routing ─────────────────────
# Was absent entirely (no keys), so a calibration/battery run could not prove
# which model actually executed a step. Battery profile makes the expectation
# deterministic regardless of the developer's env / overrides.json.

def test_meta_records_resolved_model_and_effort(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAUD_INV_PROFILE", "battery")
    fake = _FakeRun(_completed(_wrapper('{"x": 1}')))
    _run_plain(monkeypatch, fake, schema=_Mini, out_dir=tmp_path, step="defense")
    meta = json.loads((tmp_path / "defense.meta.json").read_text())
    ok = meta["attempts"][-1]
    assert ok["status"] == "ok"
    assert ok["model"] == "claude-opus-5[1m]"
    assert ok["effort"] == "max"


def test_meta_records_none_when_routing_inherits(tmp_path, monkeypatch):
    """A step with no routing (model/effort omitted → CLI inherits) records
    explicit nulls, not missing keys."""
    monkeypatch.setenv("FRAUD_INV_PROFILE", "battery")  # unknown step → None under battery
    fake = _FakeRun(_completed(_wrapper('{"x": 1}')))
    _run_plain(monkeypatch, fake, schema=_Mini, out_dir=tmp_path, step="mystep")
    meta = json.loads((tmp_path / "mystep.meta.json").read_text())
    ok = meta["attempts"][-1]
    assert ok["model"] is None
    assert ok["effort"] is None


# ── meta.json carries step-level duration totals (T2b) ───────────────────────
# Additive alongside the existing per-attempt started/finished/duration_sec:
# started_at/ended_at/duration_s summarize wall-clock elapsed from the FIRST
# attempt's start to this write, so a step's total cost (incl. retries) is
# readable without diffing every attempt's timestamps by hand.

def test_meta_records_step_level_duration_totals(tmp_path, monkeypatch):
    fake = _FakeRun(_completed(_wrapper('{"x": 1}')))
    _run_plain(monkeypatch, fake, schema=_Mini, out_dir=tmp_path, step="mystep")
    meta = json.loads((tmp_path / "mystep.meta.json").read_text())
    assert meta["started_at"] == meta["attempts"][0]["started"]
    assert meta["ended_at"] is not None
    assert meta["duration_s"] is not None and meta["duration_s"] >= 0


def test_meta_duration_totals_span_multiple_attempts(tmp_path, monkeypatch):
    """A retried step's totals span from the FIRST attempt's start, not just
    the final (successful) attempt's own duration_sec."""
    fake = _FakeRun(
        _completed(_wrapper("not json at all")),   # attempt 1 → validation fail
        _completed(_wrapper('{"x": 1}')),          # attempt 2 → valid
    )
    _run_plain(monkeypatch, fake, schema=_Mini, out_dir=tmp_path, step="mystep")
    meta = json.loads((tmp_path / "mystep.meta.json").read_text())
    assert len(meta["attempts"]) == 2
    assert meta["started_at"] == meta["attempts"][0]["started"]
    assert meta["duration_s"] is not None and meta["duration_s"] >= 0


def test_streaming_meta_records_step_level_duration_totals(tmp_path, monkeypatch):
    """Same additive totals at the STREAMING seam (the production path when
    the orchestrator has registered an event sink) — proves the shared
    ``_step_meta_payload`` helper is actually wired into both call sites,
    not just the plain path."""
    _run_streaming(monkeypatch, result_text='{"x": 1}', schema=_Mini, out_dir=tmp_path)
    meta = json.loads((tmp_path / "mystep.meta.json").read_text())
    assert meta["started_at"] == meta["attempts"][0]["started"]
    assert meta["ended_at"] is not None
    assert meta["duration_s"] is not None and meta["duration_s"] >= 0


# ── stream-stall guard (T2a) ──────────────────────────────────────────────────
# A child that emits NO stream event for FRAUD_INV_STREAM_STALL_S seconds
# while it's still alive is killed + retried (independent of timeout_sec,
# which can be much larger). A HEALTHY child that keeps producing events must
# never be touched by this guard — that's the false-positive risk.

class _NeverProducesPopen:
    """Simulates a wedged child: never exits on its own, produces no output.
    wait() blocks (polling) until an external .kill() (the stall watcher)."""

    def __init__(self, *a, **k):
        self.stdin = _FakeStdin()
        self.stderr = _FakeStderr()
        self._killed = threading.Event()

    def poll(self):
        return None if not self._killed.is_set() else -9

    def kill(self):
        self._killed.set()

    def wait(self, timeout=None):
        deadline = time.monotonic() + (timeout or 5)
        while time.monotonic() < deadline:
            if self._killed.is_set():
                return -9
            time.sleep(0.01)
        raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)


def test_stalled_child_is_killed_and_retried(tmp_path, monkeypatch):
    import fraud_investigator.streaming as streaming
    monkeypatch.setenv(streaming.STREAM_STALL_ENV, "0.2")
    calls = {"n": 0}
    killed = []

    def _popen_factory(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _NeverProducesPopen()
        return _FakePopen()   # attempt 2: instant clean exit

    def _reader_stub(proc, sink, stage, raw_lines, result_text_holder):
        if calls["n"] == 1:
            return   # silent — never append a single stream event (the stall)
        raw_lines.append('{"type":"result"}')
        result_text_holder.append('{"x": 1}')

    monkeypatch.setattr(streaming.subprocess, "Popen", _popen_factory)
    monkeypatch.setattr(streaming, "_stream_reader_thread", _reader_stub)
    # Wrap kill() so we can assert it fired without changing the fake's shape.
    def _tracking_kill(self):
        killed.append(True)
        self._killed.set()
    monkeypatch.setattr(_NeverProducesPopen, "kill", _tracking_kill)

    out = streaming.invoke_agent_streaming(
        prompt="irrelevant", schema=_Mini, out_dir=tmp_path, step_name="mystep",
        event_sink=_event_sink(tmp_path), stage_name="investigator",
        claude_bin="/nonexistent/claude-must-never-spawn-for-real",
        timeout_sec=5,
    )
    assert out.x == 1
    assert killed, "the stalled (silent) child was never killed"
    meta = json.loads((tmp_path / "mystep.meta.json").read_text())
    assert meta["attempts"][0]["retry_reason"] == "stall_timeout"
    assert len(meta["attempts"]) == 2


class _DripFeedPopen:
    """Simulates a HEALTHY, slow-but-alive child. kill() must never be called
    by the stall guard while this drip-feed is running."""

    def __init__(self, *a, **k):
        self.stdin = _FakeStdin()
        self.stderr = _FakeStderr()

    def poll(self):
        return None

    def kill(self):  # pragma: no cover — asserted-against, not expected to run
        raise AssertionError("stall guard killed a HEALTHY streaming child")

    def wait(self, timeout=None):
        time.sleep(0.5)
        return 0


def test_healthy_streaming_child_is_not_touched_by_stall_guard(tmp_path, monkeypatch):
    import fraud_investigator.streaming as streaming
    monkeypatch.setenv(streaming.STREAM_STALL_ENV, "0.4")

    def _reader_stub(proc, sink, stage, raw_lines, result_text_holder):
        # Keeps producing stream lines well inside every stall-window poll,
        # for longer than proc.wait()'s 0.5s — the guard must stay quiet.
        for _ in range(10):
            time.sleep(0.05)
            raw_lines.append('{"type":"system","subtype":"progress"}')
        raw_lines.append('{"type":"result"}')
        result_text_holder.append('{"x": 1}')

    monkeypatch.setattr(streaming.subprocess, "Popen", _DripFeedPopen)
    monkeypatch.setattr(streaming, "_stream_reader_thread", _reader_stub)

    out = streaming.invoke_agent_streaming(
        prompt="irrelevant", schema=_Mini, out_dir=tmp_path, step_name="mystep",
        event_sink=_event_sink(tmp_path), stage_name="investigator",
        claude_bin="/nonexistent/claude-must-never-spawn-for-real",
        timeout_sec=5,
    )
    assert out.x == 1
    meta = json.loads((tmp_path / "mystep.meta.json").read_text())
    assert meta["attempts"][-1].get("retry_reason") != "stall_timeout"
    assert len(meta["attempts"]) == 1
