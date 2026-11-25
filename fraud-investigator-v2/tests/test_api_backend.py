"""Provider-agnostic ``api`` backend: tool policy, tool execution, the tool loop, and dispatch.

A scripted fake ``completion`` function stands in for the provider, so these tests
exercise every code path of the backend without network access or API spend.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from fraud_investigator import api_backend, claude_p
from fraud_investigator.llm_tools import ToolExecutor, policy_from_flags


class Verdict(BaseModel):
    verdict: str
    score: float


# ----------------------------------------------------------------------------- policy
def test_default_policy_allows_everything():
    pol = policy_from_flags(None)
    assert pol.allow_all and pol.enabled("Bash") and pol.enabled("WebSearch")
    assert pol.check_bash("ls; rm -rf /tmp/x") is None  # production mode = unrestricted


def test_disallowed_tools_removed():
    pol = policy_from_flags(["--disallowedTools", "WebSearch,WebFetch"])
    assert pol.enabled("Bash") and not pol.enabled("WebSearch") and not pol.enabled("WebFetch")


def test_bash_prefix_allowlist_and_escape_blocking():
    pol = policy_from_flags(["--allowedTools",
                             "Read,Bash(python3 scripts/precedent_lookup.py:*),Bash(python scripts/x.py get:*)"])
    assert not pol.allow_all and pol.enabled("Read") and not pol.enabled("Write")
    assert pol.check_bash("python3 scripts/precedent_lookup.py --cite foo") is None
    assert pol.check_bash("python scripts/x.py get key") is None
    assert pol.check_bash("python scripts/x.py put key") is not None          # wrong subcommand
    assert pol.check_bash("python3 scripts/precedent_lookup.py_evil") is not None  # not a word boundary
    assert pol.check_bash("python3 scripts/precedent_lookup.py; cat /etc/passwd") is not None
    assert pol.check_bash("python3 scripts/precedent_lookup.py $(whoami)") is not None
    assert pol.check_bash("cat secrets.txt") is not None


def test_calibration_style_read_jail(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "ok.txt").write_text("inside")
    (tmp_path / "outside.txt").write_text("secret")
    pol = policy_from_flags(["--allowed-tools", f"Bash(python3 /abs/sandbox.py:*),Read({ws}/**)",
                             "--disallowed-tools", "WebSearch,WebFetch,Grep,Glob"])
    ex = ToolExecutor(ws, pol)
    assert "inside" in ex.run("read_file", {"path": "sub/ok.txt"})
    assert ex.run("read_file", {"path": str(tmp_path / "outside.txt")}).startswith("ERROR")
    assert ex.run("grep", {"pattern": "x"}).startswith("ERROR")
    names = {t["function"]["name"] for t in ex.tool_specs()}
    assert names == {"bash", "read_file"}


# ----------------------------------------------------------------------------- tools
def test_file_tools_roundtrip(tmp_path):
    ex = ToolExecutor(tmp_path, policy_from_flags(None))
    assert "wrote" in ex.run("write_file", {"path": "a/b.txt", "content": "hello world\nline2"})
    assert "1\thello world" in ex.run("read_file", {"path": "a/b.txt"})
    assert "edited" in ex.run("edit_file", {"path": "a/b.txt", "old_text": "world", "new_text": "there"})
    assert "hello there" in (tmp_path / "a" / "b.txt").read_text()
    assert "b.txt" in ex.run("glob", {"pattern": "**/*.txt"})
    assert "line2" in ex.run("grep", {"pattern": "line\\d", "path": "."})
    assert "[exit 0]" in ex.run("bash", {"command": "echo hi"})
    assert ex.run("nope", {}).startswith("ERROR")
    assert ex.run("read_file", {"wrong_arg": 1}).startswith("ERROR")


def test_web_search_uses_injected_provider(tmp_path):
    ex = ToolExecutor(tmp_path, policy_from_flags(None),
                      search_fn=lambda q, n: [{"title": "T", "url": "https://example.com", "snippet": q}])
    out = json.loads(ex.run("web_search", {"query": "npi lookup"}))
    assert out[0]["snippet"] == "npi lookup"


# ----------------------------------------------------------------------------- models
def test_model_resolution(monkeypatch):
    for k in ("FRAUD_INV_API_MODEL", "FRAUD_INV_PROFILE", "FRAUD_INV_MODEL_DEFAULT",
              "FRAUD_INV_MODEL_DEFENSE", "FRAUD_INV_API_MODEL_DEFENSE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(claude_p, "_load_overrides", lambda: {})
    assert api_backend.resolve_api_model("defense").startswith("anthropic/claude-")
    assert "[" not in api_backend.resolve_api_model("defense")
    monkeypatch.setenv("FRAUD_INV_PROFILE", "draft")
    assert api_backend.resolve_api_model("defense") == "anthropic/claude-sonnet-5"
    monkeypatch.setenv("FRAUD_INV_API_MODEL", "openai/gpt-5")
    assert api_backend.resolve_api_model("defense") == "openai/gpt-5"  # beats the draft profile
    monkeypatch.setenv("FRAUD_INV_API_MODEL_DEFENSE", "gemini/gemini-2.5-pro")
    assert api_backend.resolve_api_model("defense") == "gemini/gemini-2.5-pro"


# ----------------------------------------------------------------------------- tool loop
class ScriptedModel:
    """Fake LiteLLM ``completion``: returns queued responses and records each call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs, default=str)))
        return self.responses.pop(0)


def _tool_call(name, args, cid="c1"):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10}}


def _answer(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20}}


class Sink:
    def __init__(self):
        self.events = []

    def emit(self, stage, kind, payload):
        self.events.append((stage, kind, payload))


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(api_backend, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(claude_p, "_DEFAULT_CWD", tmp_path)
    monkeypatch.setenv("FRAUD_INV_NO_CACHE_LOAD", "1")
    monkeypatch.setenv("FRAUD_INV_API_MODEL", "openai/test-model")
    monkeypatch.setattr(claude_p, "DISPLAY_PARSED_JSON", False)
    return tmp_path


def test_tool_loop_runs_tools_then_parses(isolated):
    fake = ScriptedModel([_tool_call("bash", {"command": "echo evidence"}),
                          _answer('```json\n{"verdict": "HIGH", "score": 0.9}\n```')])
    sink = Sink()
    out = isolated / "case"
    result = api_backend.run_step(prompt="Investigate.", schema=Verdict, out_dir=out, step_name="triage",
                                  timeout_sec=60, completion=fake, event_sink=sink, stage_name="triage")
    assert result == Verdict(verdict="HIGH", score=0.9)
    # tool result was fed back to the model
    tool_msgs = [m for m in fake.calls[1]["messages"] if m["role"] == "tool"]
    assert "evidence" in tool_msgs[0]["content"]
    raw = json.loads((out / "triage.raw.json").read_text())
    assert raw["usage"] == {"input_tokens": 150, "output_tokens": 30} and raw["backend"] == "api"
    meta = json.loads((out / "triage.meta.json").read_text())
    assert meta["attempts"][-1]["status"] == "ok"
    kinds = [k for _, k, _ in sink.events]
    assert len(kinds) >= 2  # tool_use + tool_result at minimum


def test_schema_repair_retry(isolated):
    fake = ScriptedModel([_answer('{"verdict": "HIGH"}'), _answer('{"verdict": "HIGH", "score": 0.5}')])
    result = api_backend.run_step(prompt="x", schema=Verdict, out_dir=isolated / "c", step_name="triage",
                                  timeout_sec=60, completion=fake)
    assert result.score == 0.5
    assert "REPAIR" in fake.calls[1]["messages"][-1]["content"]


def test_max_turns_triggers_finalize_without_tools(isolated, monkeypatch):
    monkeypatch.setattr(claude_p, "TURN_BUDGET_BY_STEP", {"triage": 1})
    fake = ScriptedModel([_tool_call("bash", {"command": "echo 1"}),
                          _answer('{"verdict": "LOW", "score": 0.1}')])
    result = api_backend.run_step(prompt="x", schema=Verdict, out_dir=isolated / "c", step_name="triage",
                                  timeout_sec=60, completion=fake)
    assert result.verdict == "LOW"
    assert fake.calls[1].get("tool_choice") == "none"


def test_free_text_and_session_continuation(isolated):
    fake = ScriptedModel([_answer("# Report\nfirst")])
    out = api_backend.run_step(prompt="write", schema=None, out_dir=isolated / "c", step_name="case_writer",
                               timeout_sec=60, completion=fake, session_id="sess-1")
    assert out.startswith("# Report")
    fake2 = ScriptedModel([_answer("# Report\nsecond")])
    api_backend.run_step(prompt="revise", schema=None, out_dir=isolated / "c2", step_name="case_writer",
                         timeout_sec=60, completion=fake2, session_id="sess-1", is_continuation=True)
    contents = [m.get("content") for m in fake2.calls[0]["messages"]]
    assert "write" in contents and "revise" in contents  # prior turns were replayed


def test_transient_error_retries(isolated, monkeypatch):
    monkeypatch.setattr(claude_p, "transient_backoff_sec", lambda a: 0)
    state = {"n": 0}

    def flaky(**kw):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("overloaded_error: 529 Overloaded")
        return _answer('{"verdict": "HIGH", "score": 1.0}')

    assert api_backend.run_step(prompt="x", schema=Verdict, out_dir=isolated / "c", step_name="triage",
                                timeout_sec=60, completion=flaky).score == 1.0


def test_invoke_agent_dispatches_to_api_backend(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(claude_p, "EXEC_BACKEND", "api")
    monkeypatch.setattr(api_backend, "run_step", lambda **kw: seen.update(kw) or "ok")
    assert claude_p.invoke_agent(prompt="p", schema=None, out_dir=tmp_path, step_name="defense",
                                 extra_args=["--disallowedTools", "WebSearch"]) == "ok"
    assert seen["step_name"] == "defense" and seen["extra_args"] == ["--disallowedTools", "WebSearch"]


def test_streaming_dispatches_to_api_backend(monkeypatch, tmp_path):
    from fraud_investigator import streaming
    seen = {}
    monkeypatch.setattr(claude_p, "EXEC_BACKEND", "api")
    monkeypatch.setattr(api_backend, "run_step", lambda **kw: seen.update(kw) or "ok")
    assert streaming.invoke_agent_streaming(prompt="p", schema=None, out_dir=tmp_path, step_name="triage",
                                            event_sink=Sink(), stage_name="triage") == "ok"
    assert seen["stage_name"] == "triage"
