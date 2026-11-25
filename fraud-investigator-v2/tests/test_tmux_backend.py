"""Unit tests for the tmux execution backend's pure helpers.

These cover the parts that don't spawn a pane (the spawning path is verified
manually — see the plan's Phase 0/5). The key regression guard here is
``_shq`` correctly quoting bracketed model names like ``opus[1m]`` (an unquoted
``[1m]`` is a zsh glob → "no matches found", which killed pane boot once).
"""
import json
import os
from pathlib import Path
from unittest import mock

import pytest

from fraud_investigator import tmux_backend as tb


# ── _shq: shell-quoting (the opus[1m] glob regression) ────────────────────────
def test_shq_quotes_bracketed_model_name():
    # opus[1m] / sonnet[1m] contain glob metacharacters → must be quoted.
    assert tb._shq("opus[1m]") == "'opus[1m]'"
    assert tb._shq("sonnet[1m]") == "'sonnet[1m]'"


def test_shq_leaves_plain_tokens_unquoted():
    for tok in ("sonnet", "--model", "high", "user,project,local",
                "Bash,Read,Write", "--permission-mode", "bypassPermissions"):
        assert tb._shq(tok) == tok, tok


def test_shq_escapes_embedded_single_quote():
    assert tb._shq("a'b") == "'a'\\''b'"


# ── _sanitize_window ──────────────────────────────────────────────────────────
def test_sanitize_window_keeps_addressable_chars():
    assert tb._sanitize_window("counsel_build") == "counsel_build"
    assert tb._sanitize_window("triage batch/1") == "triage_batch_1"


# ── directive builders carry the absolute paths ───────────────────────────────
def test_run_directive_mentions_all_three_paths(tmp_path):
    p = tmp_path / "s.prompt.md"
    r = tmp_path / "s.result.json"
    d = tmp_path / "s.done"
    msg = tb._run_directive(p, r, d)
    assert str(p) in msg and str(r) in msg and str(d) in msg
    assert "ONLY" in msg  # instructs JSON-only output


def test_repair_directive_includes_error_and_paths(tmp_path):
    r = tmp_path / "s.result.json"
    d = tmp_path / "s.done"
    msg = tb._repair_directive(r, d, "field 'tier' required")
    assert "tier" in msg and str(r) in msg and str(d) in msg
    assert "re-run" in msg.lower()  # tells it NOT to re-run the task


def test_finalize_directive_paths_and_calm_wording(tmp_path):
    # The finalize nudge must reference both paths AND avoid urgency/injection-
    # flavored phrasing — a security-conscious agent refused the alarm wording
    # ("STOP now / immediately / time budget / output nothing else") as a prompt
    # injection. Keep it calm and task-anchored.
    r = tmp_path / "s.result.json"
    d = tmp_path / "s.done"
    msg = tb._finalize_directive(r, d)
    assert str(r) in msg and str(d) in msg
    low = msg.lower()
    for banned in ("stop calling tools", "immediately", "time budget",
                   "output nothing else"):
        assert banned not in low, f"finalize nudge reads like an injection: {banned!r}"


# ── _launch_flags mirrors claude_p's permission policy ────────────────────────
def test_launch_flags_bypass_when_not_safe_perms():
    with mock.patch.object(tb, "SAFE_PERMS", False):
        flags = tb.TmuxOrchestra._launch_flags("opus[1m]", "max")
    assert "--permission-mode" in flags
    assert flags[flags.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--model" in flags and "opus[1m]" in flags
    assert "--effort" in flags and "max" in flags
    assert "--allowedTools" not in flags


def test_launch_flags_default_plus_allowlist_under_safe_perms():
    with mock.patch.object(tb, "SAFE_PERMS", True):
        flags = tb.TmuxOrchestra._launch_flags("sonnet", "medium")
    assert flags[flags.index("--permission-mode") + 1] == "default"
    assert "--allowedTools" in flags
    assert flags[flags.index("--allowedTools") + 1] == tb.SAFE_PERMS_BASE_ALLOWED
    assert "bypassPermissions" not in flags


def test_launch_flags_omit_model_effort_when_none():
    with mock.patch.object(tb, "SAFE_PERMS", False):
        flags = tb.TmuxOrchestra._launch_flags(None, None)
    assert "--model" not in flags
    assert "--effort" not in flags
    assert "--setting-sources" in flags


# ── calibration: clean-room jail at the pane level ────────────────────────────
def test_calibration_pane_flags_scope_io_and_deny_web(tmp_path):
    out_dir = tmp_path / "iter-01"
    ea = ["--allowed-tools", "Bash(python3 scripts/sandbox_browser.py:*),Read(/exp/**)",
          "--disallowed-tools", "WebSearch,WebFetch,Grep,Glob"]
    flags = tb._calibration_pane_flags(ea, out_dir)
    od = str(out_dir.resolve())
    assert "--permission-mode" in flags
    ai = flags.index("--allowedTools") + 1
    allowed = flags[ai]
    # preserves the cal sandbox tool + export Read, adds out_dir Read+Write
    assert "Bash(python3 scripts/sandbox_browser.py:*)" in allowed
    assert "Read(/exp/**)" in allowed
    assert f"Read({od}/**)" in allowed and f"Write({od}/**)" in allowed
    # web/grep/glob denied; out_dir granted via --add-dir
    assert flags[flags.index("--disallowedTools") + 1] == "WebSearch,WebFetch,Grep,Glob"
    assert od in flags[flags.index("--add-dir") + 1]
    assert "bypassPermissions" not in flags


def test_calibration_pane_scopes_to_case_root_across_iterations(tmp_path):
    """Regression (2026-06-15): a persistent cal pane reused across iterations
    must grant Read/Write on the CASE ROOT, not the per-step out_dir — else an
    iter-02 `touch …/iter-02/X.done` falls outside the iter-01 grant and
    acceptEdits prompts, hanging the unattended pane."""
    case_root = tmp_path / "CALIBRATION-ABCD1234"
    ea = ["--allowed-tools", "Read(/exp/**)", "--disallowed-tools", "WebSearch,WebFetch"]
    cr = str(case_root.resolve())
    for step_dir in (case_root / "iter-01", case_root / "iter-02", case_root / "adv-r1"):
        flags = tb._calibration_pane_flags(ea, step_dir)
        allowed = flags[flags.index("--allowedTools") + 1]
        assert flags[flags.index("--add-dir") + 1] == cr
        assert f"Read({cr}/**)" in allowed and f"Write({cr}/**)" in allowed
        # never the narrow per-step dir
        assert f"Write({step_dir.resolve()}/**)" not in allowed


def test_hook_settings_injects_guard_when_given(tmp_path):
    spool = tmp_path / "spool"
    case_root = tmp_path / "CALIBRATION-X"
    export = tmp_path / "exp"
    js = tb._hook_settings_json(spool, (case_root, export))
    cmd = json.loads(js)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--guard" in cmd and str(case_root) in cmd and str(export) in cmd
    # no guard → plain spool hook (production streaming)
    assert "--guard" not in tb._hook_settings_json(spool)


def test_guard_hook_reproduces_headless_jail():
    """The PreToolUse guard: sandbox tools + scoped file-I/O allowed; web,
    repo-traversal, and out-of-scope reads/bash denied (no prompt either way)."""
    from fraud_investigator import _tmux_tool_hook as hook
    roots = ["/run/CALIBRATION-X", "/exp"]
    d = lambda tn, ti: hook._decide(tn, ti, roots)[0]
    # allowed: sandbox tools (even ones absent from the export → run+fail, like headless), sentinels
    assert d("Bash", {"command": "python3 scripts/hcpcs_lookup.py --code 99213"}) == "allow"
    assert d("Bash", {"command": "touch /run/CALIBRATION-X/iter-02/x.done"}) == "allow"
    assert d("Read", {"file_path": "/exp/panels/cms.parquet"}) == "allow"
    assert d("Write", {"file_path": "/run/CALIBRATION-X/iter-02/r.result.json"}) == "allow"
    assert d("Task", {"prompt": "sub"}) == "allow"
    # denied: answer-key reads, raw web, repo traversal, out-of-scope paths
    assert d("Bash", {"command": "cat /Users/x/repo/data/gold_set.jsonl"}) == "deny"
    assert d("Read", {"file_path": "/Users/x/repo/data/gold_set.jsonl"}) == "deny"
    assert d("WebFetch", {"url": "https://justice.gov"}) == "deny"
    assert d("Grep", {"pattern": "settlement"}) == "deny"


def test_launch_flags_calibration_overrides_permissions(tmp_path):
    cal = tb._calibration_pane_flags(
        ["--allowed-tools", "Read(/exp/**)", "--disallowed-tools", "WebSearch"], tmp_path)
    with mock.patch.object(tb, "SAFE_PERMS", False):
        flags = tb.TmuxOrchestra._launch_flags("opus[1m]", "max", None, cal)
    # cal flags win; NOT bypassPermissions, NOT the SAFE_PERMS broad allowlist
    assert "bypassPermissions" not in flags
    assert tb.SAFE_PERMS_BASE_ALLOWED not in flags
    assert "--disallowedTools" in flags and "--add-dir" in flags
    assert "--model" in flags and "opus[1m]" in flags


# ── hook streaming: settings injection ────────────────────────────────────────
def test_launch_flags_inject_hook_settings_when_spool_given(tmp_path):
    with mock.patch.object(tb, "SAFE_PERMS", False):
        flags = tb.TmuxOrchestra._launch_flags("sonnet", "medium", tmp_path)
    assert "--settings" in flags
    settings = json.loads(flags[flags.index("--settings") + 1])
    assert "PreToolUse" in settings["hooks"] and "PostToolUse" in settings["hooks"]
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "_tmux_tool_hook.py" in cmd and str(tmp_path) in cmd


def test_launch_flags_no_settings_without_spool():
    with mock.patch.object(tb, "SAFE_PERMS", False):
        flags = tb.TmuxOrchestra._launch_flags("sonnet", "medium", None)
    assert "--settings" not in flags


def test_hook_settings_json_quotes_paths_with_spaces():
    # The repo path contains a space ("Healthcare Fraud Detection"); the hook
    # command must shell-quote it so claude can run the hook.
    spool = Path("/tmp/has space/spool")
    s = json.loads(tb._hook_settings_json(spool))
    cmd = s["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert "'/tmp/has space/spool'" in cmd or '"/tmp/has space/spool"' in cmd


# ── hook event → EventSink mapping ────────────────────────────────────────────
class _CapSink:
    def __init__(self):
        self.events = []

    def emit(self, stage, type_, payload=None):
        self.events.append((stage, type_, payload or {}))


def test_forward_pretooluse_maps_to_tool_use():
    s = _CapSink()
    tb._forward_hook_event(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash",
         "tool_input": {"command": "echo hi"}, "tool_use_id": "t1"},
        s, "investigator")
    assert s.events == [("investigator", tb.EVENT_STAGE_TOOL_USE,
                         {"name": "Bash", "input": {"command": "echo hi"}, "id": "t1"})]


def test_forward_posttooluse_maps_to_tool_result_with_content():
    s = _CapSink()
    tb._forward_hook_event(
        {"hook_event_name": "PostToolUse", "tool_use_id": "t1",
         "tool_response": {"output": "hi\n"}},
        s, "investigator")
    stage, typ, payload = s.events[0]
    assert typ == tb.EVENT_STAGE_TOOL_RESULT
    assert payload["tool_use_id"] == "t1" and payload["content"] == "hi\n"


def test_forward_tags_subagent_tool_calls():
    s = _CapSink()
    tb._forward_hook_event(
        {"hook_event_name": "PreToolUse", "tool_name": "Grep", "tool_input": {},
         "tool_use_id": "t9", "agent_type": "general-purpose"},
        s, "investigator")
    assert s.events[0][2].get("agent_type") == "general-purpose"


def test_stringify_tool_response_variants():
    assert tb._stringify_tool_response("plain") == "plain"
    assert tb._stringify_tool_response({"stdout": "abc"}) == "abc"
    assert "k" in tb._stringify_tool_response({"k": "v"})  # falls back to json
    assert tb._stringify_tool_response(None) == ""


# ── reasoning scrape filters tool lines, keeps prose ──────────────────────────
def test_scrape_reasoning_emits_prose_not_tools(monkeypatch):
    pane = (
        "⏺ Let me analyze the billing pattern for this provider.\n"
        "⏺ Bash(echo hi)\n"
        "  ⎿  hi\n"
        "⏺ Web Search(\"skin substitute fraud\")\n"
        "⏺ The Q4188 code shows an anomalous spike.\n"
    )
    orch = mock.Mock()
    orch.capture.return_value = pane
    s = _CapSink()
    tb._scrape_reasoning(orch, "sess:win", s, "investigator", set())
    texts = [p["text"] for (_, t, p) in s.events if t == tb.EVENT_STAGE_TEXT_TURN]
    assert any("analyze the billing pattern" in x for x in texts)
    assert any("anomalous spike" in x for x in texts)
    # tool invocations must NOT be forwarded as reasoning
    assert not any("Bash(" in x or "Web Search(" in x for x in texts)


# ── regression: schema=None (free-text steps like case_writer) on the tmux backend
# must NOT call None.model_validate_json (the #316/#318 crash that killed Bradley's
# dossier on the tmux backend and triggered a duplicate opus rerun). ─────────────

def _stub_orch(tmp_path):
    class _Orch:
        cwd = tmp_path
        def ensure_pane(self, *a, **k): return "sess:win"
        def spool_for(self, step): return None
        def send_directive(self, target, directive): pass
        def capture(self, target): return ""
    return _Orch()


def _wire_tmux_stubs(tb, monkeypatch, tmp_path, result_text):
    monkeypatch.setattr(tb, "_get_orchestra", lambda: _stub_orch(tmp_path))
    monkeypatch.setattr(tb, "_poll_for_marker", lambda *a, **k: True)
    monkeypatch.setattr(tb, "_read_result", lambda p, **k: result_text)
    monkeypatch.setattr(tb, "_drain_spool", lambda *a, **k: None)
    monkeypatch.setattr(tb, "_emit", lambda *a, **k: None)


def test_tmux_run_step_schema_none_returns_text(tmp_path, monkeypatch):
    import fraud_investigator.tmux_backend as tb
    _wire_tmux_stubs(tb, monkeypatch, tmp_path, "# Case Report\n\nProse deliverable.")
    out = tb.run_step(prompt="p", schema=None, out_dir=tmp_path, step_name="case_writer")
    assert isinstance(out, str) and "Case Report" in out
    assert (tmp_path / "case_writer.parsed.json").read_text().startswith("# Case Report")


def test_tmux_run_step_schema_none_skips_stale_cache(tmp_path, monkeypatch):
    import fraud_investigator.tmux_backend as tb
    (tmp_path / "case_writer.parsed.json").write_text("STALE prior doc")  # the crash trigger
    _wire_tmux_stubs(tb, monkeypatch, tmp_path, "# Fresh Report")
    out = tb.run_step(prompt="p", schema=None, out_dir=tmp_path, step_name="case_writer")
    assert out == "# Fresh Report"  # re-ran (didn't None.model_validate_json the stale cache)
