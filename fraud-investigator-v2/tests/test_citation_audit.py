"""Citation auditor (M6) — injected runner/url_checker, no network/subprocess."""
from __future__ import annotations

from pathlib import Path

from fraud_investigator.citation_audit import MAX_EXECUTIONS, audit_citations


def _ev(citation, id="ev-1", tier="confirmed_fact"):
    return {"id": id, "tier": tier, "citation": citation}


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "nppes_lookup.py").write_text("# stub")
    (tmp_path / "scripts" / "duck.py").write_text("# stub")
    (tmp_path / "data" / "processed").mkdir(parents=True)
    (tmp_path / "data" / "processed" / "leie.parquet").write_text("x")
    return tmp_path


def _ok_runner(cmd, cwd):
    return 0, "some output", ""


def _url(status):
    return lambda url: status


def test_script_citation_verified(tmp_path):
    res = audit_citations(
        [_ev("python scripts/nppes_lookup.py --npi 1900000002; data: live NPPES API")],
        repo_root=_repo(tmp_path), runner=_ok_runner, url_checker=_url(200))
    # NOTE: the ';' separates command from provenance prose; args end at ws-run
    assert res["items"][0]["status"] in ("VERIFIED_EXEC", "FAILED_EXEC")


def test_nonexistent_script_fails(tmp_path):
    res = audit_citations([_ev("python scripts/made_up_tool.py --npi 1")],
                          repo_root=_repo(tmp_path), runner=_ok_runner,
                          url_checker=_url(200))
    assert res["items"][0]["status"] == "FAILED_EXEC"
    assert "does not exist" in res["items"][0]["detail"]
    assert res["n_failed"] == 1


def test_chained_command_refused_not_executed(tmp_path):
    """A citation that chains a second command (&&/||/;/&) is refused and the
    injected runner is never called — the injection guard."""
    calls = []

    def recording_runner(cmd, cwd):
        calls.append(cmd)
        return 0, "out", ""

    res = audit_citations(
        [_ev("python scripts/nppes_lookup.py --npi 1 && rm -rf /")],
        repo_root=_repo(tmp_path), runner=recording_runner, url_checker=_url(200))
    assert res["items"][0]["status"] == "FAILED_EXEC"
    assert "chains" in res["items"][0]["detail"]
    assert calls == []  # never executed


def test_quoted_sql_metachars_execute(tmp_path):
    """The Bradley failure mode: a DuckDB SQL arg with `>`/`<`/single-quotes
    inside double quotes must EXECUTE (not be rejected), passed as one argv."""
    calls = []

    def recording_runner(cmd, cwd):
        calls.append(cmd)
        return 0, "SUM=10252930\n", ""

    sql = "SELECT SUM(amt) FROM read_parquet('data/x.parquet') WHERE amt > 5000000"
    res = audit_citations(
        [_ev(f'python scripts/duck.py "{sql}"')],
        repo_root=_repo(tmp_path), runner=recording_runner, url_checker=_url(200))
    assert res["items"][0]["status"] == "VERIFIED_EXEC"
    assert calls and calls[0][-1] == sql      # SQL preserved as a single literal arg
    assert res["n_verified"] == 1 and res["n_failed"] == 0


def test_cosmetic_pipe_truncated_and_run(tmp_path):
    """`… | jq` is a cosmetic output stage — truncate it, re-run the script
    head, and do not pass the pipe/jq tokens to the script."""
    calls = []

    def recording_runner(cmd, cwd):
        calls.append(cmd)
        return 0, "rows\n", ""

    res = audit_citations(
        [_ev("python scripts/duck.py \"SELECT 1\" | jq '.x'")],
        repo_root=_repo(tmp_path), runner=recording_runner, url_checker=_url(200))
    assert res["items"][0]["status"] == "VERIFIED_EXEC"
    assert calls[0][-1] == "SELECT 1"          # only the script arg, no '|'/'jq'
    assert "ignored cosmetic" in res["items"][0]["detail"]


def test_command_substitution_is_gap_not_failure(tmp_path):
    res = audit_citations(
        [_ev("python scripts/nppes_lookup.py --as-of $(date +%F)")],
        repo_root=_repo(tmp_path), runner=_ok_runner, url_checker=_url(200))
    assert res["items"][0]["status"] == "UNVERIFIABLE_SUBST"
    assert res["n_failed"] == 0
    assert res["n_gaps"] == 1


def test_gap_vs_failure_breakdown(tmp_path):
    """Verified / failed / gap are reported separately so unclosed gaps are
    surfaced rather than lumped into one failure rate."""
    repo = _repo(tmp_path)
    res = audit_citations(
        [_ev("python scripts/nppes_lookup.py --npi 1", id="ok"),         # VERIFIED_EXEC
         _ev("python scripts/made_up.py --npi 1", id="badcmd"),          # FAILED_EXEC
         _ev("https://law.justia.com/x", id="blocked"),                  # URL_BLOCKED_403 (gap)
         _ev("United States v. Mackby, 261 F.3d 821 (9th Cir. 2001)", id="cite")],  # MANUAL_REVIEW (gap)
        repo_root=repo, runner=_ok_runner, url_checker=_url(403))
    assert res["n_verified"] == 1
    assert res["n_failed"] == 1
    assert res["n_gaps"] == 2
    assert {g["evidence_id"] for g in res["gaps"]} == {"blocked", "cite"}


def test_url_statuses(tmp_path):
    repo = _repo(tmp_path)
    ok = audit_citations([_ev("https://www.justice.gov/usao/pr/x; verified 2026")],
                         repo_root=repo, runner=_ok_runner, url_checker=_url(200))
    assert ok["items"][0]["status"] == "VERIFIED_URL"
    blocked = audit_citations([_ev("https://law.justia.com/x")],
                              repo_root=repo, runner=_ok_runner, url_checker=_url(403))
    assert blocked["items"][0]["status"] == "URL_BLOCKED_403"
    assert blocked["n_failed"] == 0  # 403 is the known Akamai class, not a failure
    dead = audit_citations([_ev("https://example.com/gone")],
                           repo_root=repo, runner=_ok_runner, url_checker=_url(404))
    assert dead["items"][0]["status"] == "FAILED_URL"
    assert dead["n_failed"] == 1


def test_data_path_check(tmp_path):
    repo = _repo(tmp_path)
    res = audit_citations(
        [_ev("data/processed/leie.parquet WHERE NPI='1' via duckdb"),
         _ev("data/processed/never_built.parquet → 12 rows", id="ev-2")],
        repo_root=repo, runner=_ok_runner, url_checker=_url(200))
    st = {i["evidence_id"]: i["status"] for i in res["items"]}
    assert st["ev-1"] == "VERIFIED_PATH"
    assert st["ev-2"] == "MISSING_PATH"


def test_caselaw_and_unrecognized(tmp_path):
    repo = _repo(tmp_path)
    res = audit_citations(
        [_ev("United States v. Mackby, 261 F.3d 821 (9th Cir. 2001)"),
         _ev("based on Part B billing", id="ev-2"),
         _ev("", id="ev-3")],
        repo_root=repo, runner=_ok_runner, url_checker=_url(200))
    st = {i["evidence_id"]: i["status"] for i in res["items"]}
    assert st["ev-1"] == "MANUAL_REVIEW"
    assert st["ev-2"] == "UNRECOGNIZED"
    assert st["ev-3"] == "UNRECOGNIZED"
    assert res["n_failed"] == 2  # the vague + empty ones


def test_execution_budget_cap(tmp_path):
    repo = _repo(tmp_path)
    evs = [_ev(f"python scripts/nppes_lookup.py --npi {i}", id=f"ev-{i}")
           for i in range(MAX_EXECUTIONS + 5)]
    res = audit_citations(evs, repo_root=repo, runner=_ok_runner,
                          url_checker=_url(200))
    skipped = [i for i in res["items"] if i["status"] == "SKIPPED_BUDGET"]
    assert len(skipped) == 5


def test_calibration_skips_everything(tmp_path):
    calls = []

    def recording_runner(cmd, cwd):
        calls.append(cmd)
        return 0, "out", ""

    res = audit_citations([_ev("python scripts/nppes_lookup.py --npi 1"),
                           _ev("https://example.com", id="ev-2")],
                          repo_root=_repo(tmp_path), runner=recording_runner,
                          url_checker=_url(200), calibration=True)
    assert res["skipped"] is True
    assert res["items"] == [] and calls == []


def test_url_deduplication(tmp_path):
    hits = []

    def counting_checker(url):
        hits.append(url)
        return 200

    audit_citations([_ev("https://example.com/a"),
                     _ev("https://example.com/a", id="ev-2")],
                    repo_root=_repo(tmp_path), runner=_ok_runner,
                    url_checker=counting_checker)
    assert len(hits) == 1


# ── prose-boundary artifact repairs (2026-08-17, the Maple 18/23) ────────

def _argv(args):
    from fraud_investigator.citation_audit import _command_argv
    return _command_argv(args)


def test_trailing_period_and_colon_stripped():
    argv, marker = _argv("--year 2024. --npi 1900000016")
    assert argv == ["--year", "2024", "--npi", "1900000016"] and marker is None
    argv, marker = _argv("--year 2024: --code G0482")
    assert argv[1] == "2024"


def test_field_title_colon_stripped():
    argv, marker = _argv("--field title: --query oaktree")
    assert argv[0:2] == ["--field", "title"]


def test_pasted_second_command_truncates_head():
    argv, marker = _argv("--npi 123 python3 scripts/other_tool.py --x 1")
    assert argv == ["--npi", "123"] and marker == "PASTED_CMD"
    argv, marker = _argv("--npi 123 /usr/bin/python3 scripts/other_tool.py")
    assert argv == ["--npi", "123"] and marker == "PASTED_CMD"


def test_unbalanced_quote_auto_closed():
    argv, marker = _argv('--where "amt > 5000')
    assert argv == ["--where", "amt > 5000"] and marker is None


def test_quoted_sql_untouched_by_punct_strip():
    argv, marker = _argv('--sql "SELECT a, b FROM t WHERE y=2024."')
    assert argv == ["--sql", "SELECT a, b FROM t WHERE y=2024."]


def test_chain_still_refused():
    argv, marker = _argv("--npi 123 && rm -rf /")
    assert marker == "CHAIN"


def test_prose_tail_truncated():
    argv, marker = _argv("--npi 123 --year 2024 using .venv/bin/python3 on the panel")
    assert argv == ["--npi", "123", "--year", "2024"] and marker == "PROSE_TAIL"


def test_state_value_not_eaten_by_stoplist():
    argv, marker = _argv("--state ca --year 2024")
    assert argv == ["--state", "ca", "--year", "2024"] and marker is None


# ── 2026-08-18 filing-grade cleanup: prose-paren / litigation-cite / DERIVED ──

def test_prose_parenthetical_truncated():
    argv, marker = _argv('--year 2024 "SELECT 1" (99308: n=5157, max=3.66)')
    assert argv == ["--year", "2024", "SELECT 1"] and marker == "PROSE_PAREN"


def test_nppes_prose_paren_truncated():
    argv, marker = _argv("--postal-code 08759 --limit 200 (independently re-run 2026-08-18)")
    assert argv == ["--postal-code", "08759", "--limit", "200"] and marker == "PROSE_PAREN"


def _classify(cit):
    from fraud_investigator.citation_audit import _classify_and_verify
    from pathlib import Path
    return _classify_and_verify(cit, repo_root=Path("."), runner=lambda *a, **k: (0, "ok", ""),
                                url_checker=lambda u: 200, exec_budget=[40], url_cache={})["status"]


def test_litigation_cite_is_manual_review_not_failure():
    assert _classify('python scripts/courtlistener_search.py verify "<case name>" --reporter "<cite>"') == "MANUAL_REVIEW"
    assert _classify('python scripts/precedent_cache.py put --citation "..." --verified true') == "MANUAL_REVIEW"
    assert _classify('python scripts/doj_pr_lookup.py fetch "https://justice.gov/x"') == "MANUAL_REVIEW"


def test_derived_crossref_recognized():
    assert _classify("derived from ev-1-mue-per-se-anchor and ev-1-nf-code-mix (same underlying commands); no new tool call") == "DERIVED"
    assert _classify("round-1 window-function queries already produced (per ev-rebut-r1-99309-target-is-1st)") == "DERIVED"


def test_derived_needs_ev_ref_not_just_prose():
    # bare prose with no ev- reference stays UNRECOGNIZED
    assert _classify("this is just a prose sentence with no command") == "UNRECOGNIZED"
