"""Live-watch noise: thinking_tokens system events throttle to a heartbeat.

Pre-fix, every extended-thinking CHUNK emitted a STAGE_LOG_LINE
("system/thinking_tokens") — hundreds of identical lines per xhigh agent
step, clogging the console live watch and bloating events.jsonl (re-parsed
on every 2.5s poll). Now: at most one STAGE_PROGRESS {kind: thinking}
heartbeat per window; other system subtypes unchanged.
"""
from __future__ import annotations

import fraud_investigator.streaming as streaming
from fraud_investigator.streaming import _forward_stream_event


class _Sink:
    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    def emit(self, stage, etype, payload):
        self.events.append((stage, etype, payload))


def _thinking_event():
    return {"type": "system", "subtype": "thinking_tokens"}


def test_thinking_chunks_throttle_to_one_heartbeat(monkeypatch):
    sink = _Sink()
    monkeypatch.setattr(streaming, "_THINKING_HEARTBEAT", {})
    clock = [1000.0]
    monkeypatch.setattr(streaming.time, "monotonic", lambda: clock[0])

    for _ in range(50):  # a burst of thinking chunks within one window
        _forward_stream_event(_thinking_event(), sink, "investigator")
    assert len(sink.events) == 1
    stage, etype, payload = sink.events[0]
    assert etype == "STAGE_PROGRESS" and payload == {"kind": "thinking"}

    clock[0] += streaming.THINKING_HEARTBEAT_SEC + 0.1  # next window
    _forward_stream_event(_thinking_event(), sink, "investigator")
    assert len(sink.events) == 2


def test_throttle_is_per_stage(monkeypatch):
    sink = _Sink()
    monkeypatch.setattr(streaming, "_THINKING_HEARTBEAT", {})
    monkeypatch.setattr(streaming.time, "monotonic", lambda: 5.0)
    _forward_stream_event(_thinking_event(), sink, "investigator")
    _forward_stream_event(_thinking_event(), sink, "defense")
    assert len(sink.events) == 2  # one heartbeat per stage


def test_other_system_subtypes_still_log(monkeypatch):
    sink = _Sink()
    monkeypatch.setattr(streaming, "_THINKING_HEARTBEAT", {})
    _forward_stream_event({"type": "system", "subtype": "init"}, sink, "investigator")
    assert [(e[1], e[2].get("msg")) for e in sink.events] == [
        ("STAGE_LOG_LINE", "system/init")]


def test_thinking_never_emits_log_lines(monkeypatch):
    sink = _Sink()
    monkeypatch.setattr(streaming, "_THINKING_HEARTBEAT", {})
    for _ in range(10):
        _forward_stream_event(_thinking_event(), sink, "counsel_build")
    assert all(e[1] != "STAGE_LOG_LINE" for e in sink.events)
