"""SAFE-PERMS launch mode — default-mode + broad allowlist instead of
--dangerously-skip-permissions, so a production run is launchable from inside
a Claude Code session (the nested-autonomy guard only trips on skip-perms
nesting; default mode does not — verified live 2026-06-14, permission_denials=[]).

Pins the flag transformation: strip skip-perms, force --permission-mode default,
inject the base allowlist UNLESS the agent already scoped its own allowlist
(counsel_build's narrow pull-only set must not be widened).
"""
from __future__ import annotations

import fraud_investigator.claude_p as cp


def _base_cmd(*extra):
    # Mirrors what invoke_agent builds before the safe-perms hook.
    return ["claude", "-p", "--output-format", "json",
            "--dangerously-skip-permissions",
            "--setting-sources", "user,project,local", *extra]


def test_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(cp, "SAFE_PERMS", False)
    cmd = _base_cmd()
    assert cp._apply_safe_perms(cmd) == cmd  # unchanged; skip-perms retained
    assert "--dangerously-skip-permissions" in cmd


def test_strips_skipperms_and_forces_default(monkeypatch):
    monkeypatch.setattr(cp, "SAFE_PERMS", True)
    out = cp._apply_safe_perms(_base_cmd())
    assert "--dangerously-skip-permissions" not in out
    assert out[out.index("--permission-mode") + 1] == "default"


def test_injects_base_allowlist_for_denylist_agent(monkeypatch):
    monkeypatch.setattr(cp, "SAFE_PERMS", True)
    # A typical agent: ships only a --disallowedTools denylist.
    out = cp._apply_safe_perms(_base_cmd("--disallowedTools", "WebSearch,WebFetch"))
    assert "--allowedTools" in out
    assert out[out.index("--allowedTools") + 1] == cp.SAFE_PERMS_BASE_ALLOWED
    # the agent's denylist is preserved (deny wins on top in default mode)
    assert "--disallowedTools" in out


def test_respects_agent_own_allowlist(monkeypatch):
    monkeypatch.setattr(cp, "SAFE_PERMS", True)
    # counsel_build scopes its OWN narrow allowlist — must NOT be widened.
    narrow = "Read,Grep,Glob,Bash(python3 scripts/precedent_lookup.py:*)"
    out = cp._apply_safe_perms(_base_cmd("--allowedTools", narrow))
    # exactly one --allowedTools, and it's the agent's narrow one
    assert out.count("--allowedTools") == 1
    assert out[out.index("--allowedTools") + 1] == narrow
    assert cp.SAFE_PERMS_BASE_ALLOWED not in out
    # still flips to default mode + drops skip-perms
    assert "--dangerously-skip-permissions" not in out
    assert "--permission-mode" in out
