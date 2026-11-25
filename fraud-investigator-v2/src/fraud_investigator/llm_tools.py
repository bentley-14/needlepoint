"""Backend-neutral agent tools + permission policy for the provider-agnostic ``api`` backend.

The Claude Code backend gets its tools (Bash, Read, WebFetch, ...) from the CLI and
expresses per-agent jails as ``--allowedTools`` / ``--disallowedTools`` flags. This
module gives any other model the same tool surface, implemented in Python, and parses
those SAME flags into a :class:`ToolPolicy`, so every existing per-agent jail and the
calibration clean-room apply unchanged regardless of which provider runs the step.

Tool names exposed to the model are lower_snake_case; each maps to the Claude Code
tool name that governs its permission (``bash`` -> ``Bash``, ``read_file`` -> ``Read``).
"""
from __future__ import annotations

import fnmatch
import glob as _glob
import html
import json
import os
import re
import shlex
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

MAX_TOOL_OUTPUT_CHARS = int(os.environ.get("FRAUD_INV_API_TOOL_OUTPUT_CHARS", "30000"))
BASH_TIMEOUT_SEC = int(os.environ.get("FRAUD_INV_API_BASH_TIMEOUT_SEC", "300"))
WEB_TIMEOUT_SEC = 30
USER_AGENT = "Mozilla/5.0 (compatible; needlepoint-agent/1.0)"

# model-facing tool name -> Claude Code permission name
TOOL_PERMISSION_NAME = {
    "bash": "Bash", "read_file": "Read", "write_file": "Write", "edit_file": "Edit",
    "list_dir": "LS", "glob": "Glob", "grep": "Grep", "web_fetch": "WebFetch",
    "web_search": "WebSearch",
}
ALL_PERMISSION_NAMES = set(TOOL_PERMISSION_NAME.values())
# Shell control operators that would let a prefix-restricted command escape its allowlist.
_SHELL_ESCAPE = re.compile(r"(;|&&|\|\||\||`|\$\(|>|<|\n)")


@dataclass
class ToolPolicy:
    """Which tools are usable and, for Bash/Read/Write/Edit, with which constraints.

    ``allow_all`` mirrors the production default (``--dangerously-skip-permissions``):
    every tool is available unless explicitly disallowed.
    """
    allow_all: bool = True
    allowed: set[str] = field(default_factory=set)          # bare permission names
    patterns: dict[str, list[str]] = field(default_factory=dict)  # name -> ["prefix:*" | glob]
    disallowed: set[str] = field(default_factory=set)

    def enabled(self, perm: str) -> bool:
        if perm in self.disallowed:
            return False
        if self.allow_all:
            return True
        return perm in self.allowed or perm in self.patterns

    def check_bash(self, command: str) -> str | None:
        """Return an error string if ``command`` is not permitted, else None."""
        if not self.enabled("Bash"):
            return "Bash is not permitted for this agent."
        if self.allow_all or "Bash" in self.allowed:
            return None
        cmd = command.strip()
        if _SHELL_ESCAPE.search(cmd):
            return ("Shell operators (; && | ` $( > <) are not permitted under this agent's "
                    "Bash allowlist. Run one allow-listed command at a time.")
        for pat in self.patterns.get("Bash", []):
            prefix = pat[:-2] if pat.endswith(":*") else pat
            if cmd == prefix or cmd.startswith(prefix + " "):
                return None
        allowed = ", ".join(self.patterns.get("Bash", [])) or "(none)"
        return f"Command not in this agent's Bash allowlist. Allowed prefixes: {allowed}"

    def check_path(self, perm: str, path: Path) -> str | None:
        if not self.enabled(perm):
            return f"{perm} is not permitted for this agent."
        if self.allow_all or perm in self.allowed:
            return None
        resolved = str(path.resolve())
        for pat in self.patterns.get(perm, []):
            if fnmatch.fnmatch(resolved, pat) or fnmatch.fnmatch(resolved, pat.rstrip("*").rstrip("/") + "/*") \
                    or resolved == pat.rstrip("/*"):
                return None
        return f"{perm} of {resolved} is outside this agent's allowed paths."


def _split_tool_list(value: str) -> list[str]:
    """Split a comma-separated tool spec while respecting parentheses."""
    out, depth, cur = [], 0, ""
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            if cur.strip():
                out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def policy_from_flags(extra_args: list[str] | None) -> ToolPolicy:
    """Translate Claude Code ``--allowedTools`` / ``--disallowedTools`` flags into a policy.

    Accepts both spellings (``--allowedTools`` and ``--allowed-tools``), comma-separated or
    space-separated values, and the ``Name(pattern)`` form. Unknown flags are ignored.
    """
    pol = ToolPolicy()
    args = list(extra_args or [])
    i = 0
    while i < len(args):
        flag = args[i]
        key = flag.replace("-", "").lower()
        if key in ("allowedtools", "disallowedtools") and i + 1 < len(args):
            specs = _split_tool_list(args[i + 1])
            i += 2
            if key == "allowedtools":
                pol.allow_all = False
                for s in specs:
                    m = re.fullmatch(r"(\w+)\((.*)\)", s)
                    if m:
                        pol.patterns.setdefault(m.group(1), []).append(m.group(2))
                    else:
                        pol.allowed.add(s)
            else:
                for s in specs:
                    pol.disallowed.add(re.sub(r"\(.*\)$", "", s))
            continue
        i += 1
    return pol


def _truncate(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    return text[:MAX_TOOL_OUTPUT_CHARS] + f"\n... [truncated; {len(text)} chars total]"


def _html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


class ToolExecutor:
    """Executes tool calls against a working directory under a :class:`ToolPolicy`."""

    def __init__(self, cwd: Path | None, policy: ToolPolicy,
                 search_fn: Callable[[str, int], list[dict]] | None = None):
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.policy = policy
        self.search_fn = search_fn or duckduckgo_search

    def _path(self, p: str) -> Path:
        path = Path(os.path.expanduser(p))
        return path if path.is_absolute() else (self.cwd / path)

    # -- tool schema advertised to the model ---------------------------------
    def tool_specs(self) -> list[dict]:
        specs = {
            "bash": ("Run a shell command in the working directory and return stdout+stderr. "
                     "Use it to run the project's analysis scripts (e.g. `python3 scripts/duck.py ...`).",
                     {"command": {"type": "string"}}, ["command"]),
            "read_file": ("Read a text file. Optional 1-based line offset and line limit.",
                          {"path": {"type": "string"}, "offset": {"type": "integer"},
                           "limit": {"type": "integer"}}, ["path"]),
            "write_file": ("Create or overwrite a text file.",
                           {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
            "edit_file": ("Replace one exact occurrence of old_text with new_text in a file.",
                          {"path": {"type": "string"}, "old_text": {"type": "string"},
                           "new_text": {"type": "string"}}, ["path", "old_text", "new_text"]),
            "list_dir": ("List entries of a directory.", {"path": {"type": "string"}}, ["path"]),
            "glob": ("Find files matching a glob pattern (recursive ** supported).",
                     {"pattern": {"type": "string"}}, ["pattern"]),
            "grep": ("Search files for a regular expression; returns matching lines with paths.",
                     {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
            "web_fetch": ("Fetch a URL and return its readable text.", {"url": {"type": "string"}}, ["url"]),
            "web_search": ("Search the web; returns titles, URLs and snippets.",
                           {"query": {"type": "string"}, "max_results": {"type": "integer"}}, ["query"]),
        }
        out = []
        for name, (desc, props, req) in specs.items():
            if not self.policy.enabled(TOOL_PERMISSION_NAME[name]):
                continue
            if name == "bash" and not (self.policy.allow_all or "Bash" in self.policy.allowed):
                desc += " Only these command prefixes are allowed: " + ", ".join(self.policy.patterns.get("Bash", []))
            out.append({"type": "function", "function": {
                "name": name, "description": desc,
                "parameters": {"type": "object", "properties": props, "required": req}}})
        return out

    # -- dispatch -------------------------------------------------------------
    def run(self, name: str, args: dict[str, Any]) -> str:
        try:
            fn = getattr(self, f"_t_{name}", None)
            if fn is None or name not in TOOL_PERMISSION_NAME:
                return f"ERROR: unknown tool {name!r}"
            if not self.policy.enabled(TOOL_PERMISSION_NAME[name]):
                return f"ERROR: tool {name} is not permitted for this agent."
            return _truncate(fn(**args))
        except TypeError as e:
            return f"ERROR: bad arguments for {name}: {e}"
        except Exception as e:  # tool failures are reported to the model, never raised
            return f"ERROR: {type(e).__name__}: {e}"

    def _t_bash(self, command: str) -> str:
        err = self.policy.check_bash(command)
        if err:
            return f"ERROR: {err}"
        try:
            proc = subprocess.run(["bash", "-c", command], cwd=self.cwd, capture_output=True,
                                  text=True, timeout=BASH_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            return f"ERROR: command timed out after {BASH_TIMEOUT_SEC}s"
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        return f"[exit {proc.returncode}]\n{out}"

    def _t_read_file(self, path: str, offset: int | None = None, limit: int | None = None) -> str:
        p = self._path(path)
        err = self.policy.check_path("Read", p)
        if err:
            return f"ERROR: {err}"
        lines = p.read_text(errors="replace").splitlines()
        start = max((offset or 1) - 1, 0)
        end = start + limit if limit else len(lines)
        return "\n".join(f"{i + 1}\t{l}" for i, l in enumerate(lines[start:end], start))

    def _t_write_file(self, path: str, content: str) -> str:
        p = self._path(path)
        err = self.policy.check_path("Write", p)
        if err:
            return f"ERROR: {err}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {len(content)} chars to {p}"

    def _t_edit_file(self, path: str, old_text: str, new_text: str) -> str:
        p = self._path(path)
        err = self.policy.check_path("Edit", p)
        if err:
            return f"ERROR: {err}"
        text = p.read_text()
        n = text.count(old_text)
        if n != 1:
            return f"ERROR: old_text must occur exactly once (found {n})."
        p.write_text(text.replace(old_text, new_text, 1))
        return f"edited {p}"

    def _t_list_dir(self, path: str = ".") -> str:
        p = self._path(path)
        err = self.policy.check_path("LS", p)
        if err:
            return f"ERROR: {err}"
        return "\n".join(sorted(e.name + ("/" if e.is_dir() else "") for e in p.iterdir()))

    def _t_glob(self, pattern: str) -> str:
        base = pattern if os.path.isabs(pattern) else str(self.cwd / pattern)
        hits = sorted(_glob.glob(base, recursive=True))[:500]
        return "\n".join(hits) or "(no matches)"

    def _t_grep(self, pattern: str, path: str = ".") -> str:
        rx = re.compile(pattern)
        root = self._path(path)
        files = [root] if root.is_file() else [f for f in root.rglob("*") if f.is_file()]
        out = []
        for f in files:
            if any(part in {".git", "node_modules", "__pycache__"} for part in f.parts):
                continue
            try:
                for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        out.append(f"{f}:{i}:{line[:300]}")
                        if len(out) >= 300:
                            return "\n".join(out) + "\n... [first 300 matches]"
            except (OSError, UnicodeError):
                continue
        return "\n".join(out) or "(no matches)"

    def _t_web_fetch(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            return "ERROR: only http(s) URLs are supported"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=WEB_TIMEOUT_SEC) as r:
            ctype = r.headers.get("Content-Type", "")
            body = r.read(5_000_000).decode("utf-8", errors="replace")
        return body if "json" in ctype or "text/plain" in ctype else _html_to_text(body)

    def _t_web_search(self, query: str, max_results: int = 8) -> str:
        results = self.search_fn(query, max_results)
        return json.dumps(results, indent=1) if results else "(no results)"


def duckduckgo_search(query: str, max_results: int = 8) -> list[dict]:
    """Keyless web search via DuckDuckGo's HTML endpoint (best effort; may be rate limited)."""
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=WEB_TIMEOUT_SEC) as r:
        page = r.read().decode("utf-8", errors="replace")
    results = []
    for m in re.finditer(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
                         page, re.S):
        href = html.unescape(m.group(1))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg")
        results.append({"title": _html_to_text(m.group(2)), "url": q[0] if q else href,
                        "snippet": _html_to_text(m.group(3))})
        if len(results) >= max_results:
            break
    return results
