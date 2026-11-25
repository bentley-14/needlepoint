"""Deterministic citation auditor (M6).

Every prompt in the loop demands reproducible citations; nothing enforced
it. This module re-verifies each evidence item's `citation` before the case
writer runs, so a hallucinated command, a dead URL, or a missing data file
is flagged BEFORE it reaches attorney-facing output.

Verification by citation class (v1 — reproducibility, not content-truth):

  script command   re-execute the leading ``python[3] scripts/<x>.py`` (the
                   script file must exist in the repo) via sys.executable +
                   argv — NEVER a shell. Because we never invoke a shell,
                   metacharacters *inside a quoted argument* (a DuckDB
                   ``WHERE amt > 5000000``) are harmless literals and DO run;
                   a cosmetic output stage (``| jq``, ``> out.json``) is
                   truncated and the script head is re-run; a citation that
                   *chains* additional commands (``&&``/``||``/``;``/``&``) is
                   refused (FAILED_EXEC, never executed). → VERIFIED_EXEC /
                   FAILED_EXEC / UNVERIFIABLE_SUBST
  URL              GET with timeout → VERIFIED_URL / FAILED_URL; HTTP 403 is
                   URL_BLOCKED_403 (the known Akamai class — not a failure,
                   the fallback-chain rules in investigator.md cover it)
  data path        data/….{parquet,jsonl,duckdb} exists → VERIFIED_PATH /
                   MISSING_PATH
  case-law cite    MANUAL_REVIEW (CourtListener verification is the
                   investigator's job upstream; not re-spent here)
  anything else    UNRECOGNIZED (counts against the audit — citations must
                   match one of the four reproducible forms)

Results split three ways so unclosed gaps are surfaced honestly, not buried:
``n_verified`` (re-confirmed), ``n_failed`` (real problems — bad command,
dead URL, missing file, unrecognized form), and ``n_gaps`` (unverifiable by
this tool but NOT a reliability signal — 403-blocked URLs, case cites for
manual review, shell-substitution commands, budget-skipped). A high gap count
is an honest "couldn't re-check," a high failure count is a red flag.

Calibration runs SKIP the audit entirely (re-executing live tools / fetching
URLs orchestrator-side would bypass the jail and is a leak vector).
"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

EXEC_TIMEOUT_SEC = 60
URL_TIMEOUT_SEC = 10
MAX_EXECUTIONS = 40   # per-case budget for command re-runs (a full case ~30 cites)

_CMD_RE = re.compile(r"\bpython3?\s+(scripts/[A-Za-z0-9_]+\.py)((?:\s+\S+)*)")
_URL_RE = re.compile(r"https?://[^\s;,)\]'\"]+")
_PATH_RE = re.compile(r"\bdata/[A-Za-z0-9_./-]+\.(?:parquet|jsonl|duckdb)\b")
_CASELAW_RE = re.compile(r"\b\d+\s+(?:F\.\d?d|F\. Supp\.|U\.S\.|S\. Ct\.)\s+\d+|\bv\.\s+[A-Z]")

# Shell operators, classified by what they mean for re-execution. We never run
# a shell, so these matter ONLY when shlex produces them as their own UNQUOTED
# token; the same character inside a quoted SQL arg is a literal and is kept.
_REDIRECT_OPS = {"|", ">", ">>", "<", "2>", "2>>"}   # cosmetic output stage → run the head
_PROSE_CONNECTIVES = {"using", "via", "see", "per", "source", "source:", "data",
                      "confirmed", "verified", "returns", "returned", "shows",
                      "showing", "output", "yields", "gives", "ran", "run"}
_CHAIN_OPS = {"&&", "||", ";", "&"}                  # additional commands → refuse to run
_SUBST_RE = re.compile(r"\$\(|`")                    # command substitution → unverifiable gap


def _strip_prose_punct(tok: str) -> str:
    """Strip sentence punctuation glued onto an arg by surrounding prose.

    Only fires on plain word-ish tokens (`2024.`, `title:`, `--year,`), never
    on quoted SQL (shlex already removed quotes -> those tokens contain spaces
    or operators and are left alone). Observed on CASE-6A36ABA0: `2024.`,
    `2024:`, `--field title:` — each a real argparse failure that made a
    correct citation look fabricated."""
    if re.fullmatch(r"[\w./=-]+[.,:]+", tok or ""):
        stripped = tok.rstrip(".,:")
        if stripped:
            return stripped
    return tok


def _command_argv(args: str):
    """Parse a cited command's arg string into a safe argv for the leading
    ``python3 scripts/X.py`` invocation.

    Citations follow a loose convention: ``<cmd> <args> [; provenance prose]``,
    sometimes with a cosmetic output stage (``| jq``, ``> out.json``). Returns
    ``(argv, marker)``: marker is ``None`` (clean), a redirect op we truncated
    at, the sentinel ``"CHAIN"`` (chains commands — must NOT execute), or, when
    argv is ``None``, an error detail string (shlex parse failure).
    """
    try:
        tokens = shlex.split(args)
    except ValueError:
        # Model-written citations sometimes carry an unbalanced quote from the
        # surrounding prose. One conservative retry with a closing quote; if it
        # still fails, report the original parse error.
        try:
            tokens = shlex.split(args + '"')
        except ValueError:
            try:
                tokens = shlex.split(args + "'")
            except ValueError as e:
                return None, f"unparseable args: {e}"
    argv: list[str] = []
    for tok in tokens:
        if tok in _CHAIN_OPS:
            return argv, "CHAIN"
        if tok in _REDIRECT_OPS:
            return argv, tok            # truncate at the cosmetic output stage
        if ";" in tok:                  # `--npi 1900000002; data: prose` — cmd ends at ';'
            head = tok.split(";", 1)[0]
            if head:
                argv.append(_strip_prose_punct(head))
            return argv, None           # remainder is provenance prose — stop, clean
        # A bare "(" token after >=1 real arg starts a prose parenthetical
        # ("(99308: n=5157, max=3.66)", "(retrieved 2026-08-18)") — the command
        # ended; the rest is commentary. Truncate and verify the clean head.
        # (The quoted SQL is a single shlex token, so a later "(" is outside it.)
        if argv and tok.startswith("("):
            return argv, "PROSE_PAREN"
        # A SECOND interpreter/command pasted into the citation ("python3
        # scripts/a.py … python3 scripts/b.py …", or an absolute interpreter
        # path leaking in) means the first invocation ended — verify the head.
        if argv and (tok in ("python", "python3") or tok.endswith("/python")
                     or tok.endswith("/python3") or tok.endswith("/claude")):
            return argv, "PASTED_CMD"
        # Bare prose connectives after real args mean the command ended and the
        # sentence continued ("--npi 123 using .venv/…", "--year 2024 see data/…").
        # Narrow stoplist; anything else stays an arg (scripts will still fail
        # loudly if the citation is genuinely malformed).
        if (len(argv) >= 2 and tok.lower() in _PROSE_CONNECTIVES
                and not tok.startswith("-")):
            return argv, "PROSE_TAIL"
        argv.append(_strip_prose_punct(tok))
    return argv, None


def _default_run(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=EXEC_TIMEOUT_SEC, cwd=str(cwd))
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {EXEC_TIMEOUT_SEC}s"
    except OSError as e:
        return 127, "", str(e)


def _default_url_check(url: str) -> int:
    """Return the HTTP status (0 on network error)."""
    import requests
    try:
        r = requests.get(url, timeout=URL_TIMEOUT_SEC, stream=True,
                         headers={"User-Agent": "citation-audit/1.0"})
        r.close()
        return r.status_code
    except Exception:
        return 0


def _classify_and_verify(citation: str, *, repo_root: Path, runner,
                         url_checker, exec_budget: list[int],
                         url_cache: dict) -> dict:
    citation = (citation or "").strip()
    if not citation:
        return {"status": "UNRECOGNIZED", "detail": "empty citation"}

    # Litigation-cite tools: verification is counsel's job (see investigator.md
    # role boundary) and is not orchestrator-side re-executable → reviewable gap,
    # never a reliability FAILURE. Recognize before the generic exec branch.
    if re.search(r"\bscripts/(courtlistener_search|precedent_cache|doj_pr_lookup)\.py\b", citation):
        return {"status": "MANUAL_REVIEW",
                "detail": "litigation-cite verification (CourtListener/DOJ/precedent-cache) "
                          "— confirm via verified_precedents, not orchestrator re-run"}

    m = _CMD_RE.search(citation)
    if m:
        script_rel, raw_args = m.group(1), (m.group(2) or "").strip()
        script_path = repo_root / script_rel
        if not script_path.exists():
            return {"status": "FAILED_EXEC",
                    "detail": f"cited script does not exist: {script_rel}"}
        # Command substitution can't be expanded without a shell (we never use
        # one) — an honest gap, not a failure or a fabrication signal.
        if _SUBST_RE.search(raw_args):
            return {"status": "UNVERIFIABLE_SUBST",
                    "detail": "cited command uses shell substitution ($(…)/`…`) "
                              "that cannot be re-executed without a shell"}
        argv, marker = _command_argv(raw_args)
        if argv is None:
            return {"status": "FAILED_EXEC", "detail": marker}
        if marker == "CHAIN":
            # A reproducible citation invokes ONE script; chained commands are
            # never executed (injection guard) and signal a malformed citation.
            return {"status": "FAILED_EXEC",
                    "detail": "cited command chains additional shell commands "
                              "(&&/||/;/&) — not a single reproducible invocation; "
                              "not executed"}
        if exec_budget[0] <= 0:
            return {"status": "SKIPPED_BUDGET",
                    "detail": f"per-case re-execution budget ({MAX_EXECUTIONS}) exhausted"}
        exec_budget[0] -= 1
        rc, out, err = runner([sys.executable, str(script_path)] + argv, repo_root)
        if marker in ("PASTED_CMD", "PROSE_TAIL", "PROSE_PAREN"):
            suffix = " (re-ran script head; trailing prose/pasted text in the citation was ignored)"
        elif marker not in (None, "CHAIN"):
            suffix = f" (re-ran script head; ignored cosmetic '{marker}' output stage)"
        else:
            suffix = ""
        if rc == 0 and out.strip():
            return {"status": "VERIFIED_EXEC",
                    "detail": f"{script_rel} exit 0, {len(out)} chars{suffix}"}
        return {"status": "FAILED_EXEC",
                "detail": f"{script_rel} exit {rc}: {(err or out or '')[-300:]}{suffix}"}

    # OSINT: a WebSearch citation is a query, not a URL. It is an open-web
    # ATTEMPT (real-world investigation), reviewable but not machine-fetchable
    # -> gap, never a FAILED/UNRECOGNIZED penalty. (WebFetch carries a URL and
    # falls through to the URL branch below.) Added 2026-08-18 so the auditor
    # stops penalizing the very real-world scan we want to encourage.
    if re.search(r"\bWebSearch\b", citation) and not _URL_RE.search(citation):
        return {"status": "OSINT_REVIEW",
                "detail": "open-web WebSearch attempt — reviewable, not machine-verifiable"}

    m = _URL_RE.search(citation)
    if m:
        url = m.group(0).rstrip(".")
        if url not in url_cache:
            url_cache[url] = url_checker(url)
        status = url_cache[url]
        if status == 403:
            return {"status": "URL_BLOCKED_403", "detail": url}
        if 200 <= status < 400:
            return {"status": "VERIFIED_URL", "detail": f"{url} → {status}"}
        return {"status": "FAILED_URL", "detail": f"{url} → HTTP {status or 'network error'}"}

    m = _PATH_RE.search(citation)
    if m:
        rel = m.group(0)
        return ({"status": "VERIFIED_PATH", "detail": rel}
                if (repo_root / rel).exists()
                else {"status": "MISSING_PATH", "detail": rel})

    if _CASELAW_RE.search(citation):
        return {"status": "MANUAL_REVIEW",
                "detail": "case-law cite — verified upstream via CourtListener; "
                          "confirm it appears in verified_precedents"}

    if re.search(r"\bev-[a-z0-9-]+", citation, re.I) and re.search(
            r"\b(derived from|already produced|already-gathered|underlying|same "
            r"(underlying )?(command|query|pull)|no new tool call|per (the )?"
            r"round-\d|round-\d .*queries)\b", citation, re.I):
        return {"status": "DERIVED",
                "detail": "cross-reference to prior evidence (no fresh command) "
                          "— verify against the cited ev- items"}

    return {"status": "UNRECOGNIZED",
            "detail": "citation matches none of the four reproducible forms "
                      "(script command / URL / data path / case cite)"}


# Real reliability problems — count against the audit.
_FAIL_STATUSES = ("FAILED_EXEC", "FAILED_URL", "MISSING_PATH", "UNRECOGNIZED")
# Unverifiable by this tool but NOT a reliability signal — surfaced as gaps.
_GAP_STATUSES = ("MANUAL_REVIEW", "URL_BLOCKED_403", "SKIPPED_BUDGET",
                 "UNVERIFIABLE_SUBST", "OSINT_REVIEW", "DERIVED")
_VERIFIED_STATUSES = ("VERIFIED_EXEC", "VERIFIED_URL", "VERIFIED_PATH")


def audit_citations(evidence: list[dict], *, repo_root: Path,
                    runner=_default_run, url_checker=_default_url_check,
                    calibration: bool = False) -> dict:
    """Audit every evidence item's citation. Returns a summary dict with
    per-item results; failure-tolerant and budget-capped."""
    repo_root = Path(repo_root)
    if calibration:
        return {"skipped": True,
                "reason": "calibration mode — orchestrator-side re-execution / "
                          "URL fetches would bypass the jail (leak vector)",
                "items": [], "n_failed": 0}
    exec_budget = [MAX_EXECUTIONS]
    url_cache: dict = {}
    items = []
    for ev in evidence or []:
        try:
            res = _classify_and_verify(ev.get("citation") or "",
                                       repo_root=repo_root, runner=runner,
                                       url_checker=url_checker,
                                       exec_budget=exec_budget,
                                       url_cache=url_cache)
        except Exception as e:  # pragma: no cover — never block the case
            res = {"status": "AUDIT_ERROR", "detail": str(e)}
        items.append({"evidence_id": ev.get("id"), "tier": ev.get("tier"),
                      **res})
    n_failed = sum(1 for i in items if i["status"] in _FAIL_STATUSES)
    n_gaps = sum(1 for i in items if i["status"] in _GAP_STATUSES)
    n_verified = sum(1 for i in items if i["status"] in _VERIFIED_STATUSES)
    by_status: dict[str, int] = {}
    for i in items:
        by_status[i["status"]] = by_status.get(i["status"], 0) + 1
    return {
        "skipped": False,
        "n_items": len(items),
        "n_verified": n_verified,
        "n_failed": n_failed,
        "n_gaps": n_gaps,
        "by_status": by_status,
        "failures": [i for i in items if i["status"] in _FAIL_STATUSES],
        "gaps": [i for i in items if i["status"] in _GAP_STATUSES],
        "items": items,
    }
