"""Tests for the labelgen pipeline (scripts/labelgen_{extract,escalate,ground,confirm,land}.py).

Pure-function coverage only — JSON/array parsing robustness, the escalate
selection predicate, the ground filter predicate, dedupe/overlay logic,
chunking determinism + resume keys, land dedupe+backup (tmp dirs), and the
session-cap reset-time parsing (including the timezone-honoring case). NO
NETWORK, NO LLM calls — resolve_org_npi / FacilityIndex / MedicaidSurface /
MedicareSurface / call() / call_chunk() are intentionally NOT exercised here.

Run: pytest tests/test_labelgen.py -v
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod   # needed before exec: @dataclass looks itself up here
    spec.loader.exec_module(mod)
    return mod


lex = _load("labelgen_extract")
les = _load("labelgen_escalate")
lgr = _load("labelgen_ground")
lco = _load("labelgen_confirm")
lla = _load("labelgen_land")


# ── extract_json (single-object parsing; extract + escalate) ────────────────

def test_extract_json_plain():
    assert lex.extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_markdown_fenced():
    assert lex.extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_surrounding_prose():
    assert lex.extract_json('Sure! Here is the JSON:\n{"a": 1}\nHope that helps.') == {"a": 1}


def test_extract_json_garbage_returns_none():
    assert lex.extract_json("no json here at all") is None


def test_extract_json_escalate_matches_extract():
    for text in ('{"a": 1}', '```json\n{"a": 1}\n```', "garbage"):
        assert les.extract_json(text) == lex.extract_json(text)


# ── extract_json_array (chunked confirm parsing) ────────────────────────────

def test_array_plain():
    assert lco.extract_json_array('[{"org_npi": "1"}]') == [{"org_npi": "1"}]


def test_array_markdown_fenced():
    assert lco.extract_json_array('```json\n[{"org_npi": "1"}]\n```') == [{"org_npi": "1"}]


def test_array_with_surrounding_prose():
    out = lco.extract_json_array('Here are the verdicts:\n[{"org_npi": "1"}, {"org_npi": "2"}]\nDone.')
    assert [o["org_npi"] for o in out] == ["1", "2"]


def test_array_rejects_bare_object():
    assert lco.extract_json_array('{"org_npi": "1"}') is None


def test_array_garbage_returns_none():
    assert lco.extract_json_array("total nonsense") is None


# ── needs_escalation (selection predicate) ──────────────────────────────────

def _row(extract):
    return {"uuid": "u1", "title": "t", "url": "u", "extract": extract}


def test_escalate_flag_selects():
    assert les.needs_escalation(_row({"escalate": True, "confidence": "high"}))


def test_low_confidence_selects():
    assert les.needs_escalation(_row({"escalate": False, "confidence": "low"}))


def test_high_confidence_no_flag_not_selected():
    assert not les.needs_escalation(_row({"escalate": False, "confidence": "high"}))


def test_medium_confidence_no_flag_not_selected():
    assert not les.needs_escalation(_row({"escalate": False, "confidence": "medium"}))


def test_errored_row_not_selected():
    # A row with no `extract` (prior parse failure) is labelgen_extract.py's
    # own resume job, not an escalation candidate.
    assert not les.needs_escalation({"uuid": "u1", "error": "timeout"})


# ── passes_ground_filter ─────────────────────────────────────────────────────

GOOD = {"in_scope": True, "is_resolved": True, "label_class": "org",
        "primary_entity_name": "Acme Health LLC"}


def test_ground_filter_accepts_org_medicaid_facility():
    for lc in ("org", "medicaid", "facility"):
        assert lgr.passes_ground_filter({**GOOD, "label_class": lc})


def test_ground_filter_rejects_individual_and_none():
    for lc in ("individual", "none"):
        assert not lgr.passes_ground_filter({**GOOD, "label_class": lc})


def test_ground_filter_rejects_out_of_scope():
    assert not lgr.passes_ground_filter({**GOOD, "in_scope": False})


def test_ground_filter_rejects_unresolved():
    assert not lgr.passes_ground_filter({**GOOD, "is_resolved": False})


def test_ground_filter_rejects_blank_name():
    assert not lgr.passes_ground_filter({**GOOD, "primary_entity_name": "  "})
    assert not lgr.passes_ground_filter({**GOOD, "primary_entity_name": ""})


def test_ground_filter_rejects_missing_extract():
    assert not lgr.passes_ground_filter(None)


def test_individual_candidate_predicate():
    ind = {**GOOD, "label_class": "individual"}
    assert lgr.is_individual_candidate(ind)
    assert not lgr.is_individual_candidate({**ind, "is_resolved": False})
    assert not lgr.is_individual_candidate({**ind, "label_class": "org"})


# ── apply_escalation_overlay ─────────────────────────────────────────────────

def test_overlay_replaces_by_uuid_preserving_order():
    base = [
        {"uuid": "a", "extract": {"confidence": "low"}},
        {"uuid": "b", "extract": {"confidence": "high"}},
        {"uuid": "c", "extract": {"confidence": "low"}},
    ]
    esc = [{"uuid": "c", "extract": {"confidence": "high"}, "provenance": "sonnet_escalation"}]
    out = lgr.apply_escalation_overlay(base, esc)
    assert [r["uuid"] for r in out] == ["a", "b", "c"]
    assert out[2]["provenance"] == "sonnet_escalation"
    assert out[0] is base[0]  # untouched rows pass through


def test_overlay_ignores_errored_escalation_rows():
    base = [{"uuid": "a", "extract": {"confidence": "low"}}]
    esc = [{"uuid": "a", "error": "timeout"}]   # no extract -> no replacement
    out = lgr.apply_escalation_overlay(base, esc)
    assert out[0] is base[0]


def test_overlay_ignores_unknown_uuids():
    base = [{"uuid": "a", "extract": {}}]
    esc = [{"uuid": "zzz", "extract": {}, "provenance": "sonnet_escalation"}]
    assert len(lgr.apply_escalation_overlay(base, esc)) == 1


# ── dedupe helpers (ground) ──────────────────────────────────────────────────

def test_load_existing_proposal_npis(tmp_path):
    p = tmp_path / "entity_labels.jsonl"
    p.write_text(json.dumps({"org_npi": "1234567890"}) + "\n"
                 + json.dumps({"org_npi": "1111111111"}) + "\n"
                 + "not json\n")
    assert lgr.load_existing_proposal_npis(p) == {"1234567890", "1111111111"}


def test_load_existing_proposal_npis_missing_file(tmp_path):
    assert lgr.load_existing_proposal_npis(tmp_path / "nope.jsonl") == set()


def test_load_gold_npis(tmp_path):
    p = tmp_path / "gold_set.jsonl"
    p.write_text(json.dumps({"npi": "1234567890"}) + "\n"
                 + json.dumps({"no_npi": True}) + "\n")
    assert lgr.load_gold_npis(p) == {"1234567890"}


def test_norm_org_name_strips_suffixes():
    assert lgr._norm_org_name("Acme Health, LLC") == lgr._norm_org_name("ACME HEALTH INC.")


# ── twin resolution (pure decision function) ────────────────────────────────

def _match(npi, state, ratio):
    return lgr.OrgMatch(npi=npi, nppes_org_name="X", state=state,
                        match_ratio=ratio, n_candidates=1, confidence="high")


def test_twins_single_npi_collapses_to_best_ratio():
    rel = {"source": "doj", "url": "", "body": ""}
    resolved = [("X Inc", _match("1", "TX", 0.9), rel), ("X", _match("1", "TX", 0.7), rel)]
    name, match, _, flag, cands = lgr.resolve_twin_candidates(resolved)
    assert (match.npi, match.match_ratio, flag, cands) == ("1", 0.9, None, None)


def test_twins_venue_disambiguates():
    rel_tx = {"source": "doj", "url": "https://www.justice.gov/usao-ndtx/pr/x", "body": ""}
    rel_ca = {"source": "doj", "url": "https://www.justice.gov/usao-ndtx/pr/x", "body": ""}
    resolved = [("X Inc", _match("1", "TX", 0.7), rel_tx),   # venue TX == NPPES TX
                ("X", _match("2", "CA", 0.9), rel_ca)]        # venue TX != NPPES CA
    name, match, _, flag, cands = lgr.resolve_twin_candidates(resolved)
    assert (match.npi, flag) == ("1", None)


def test_twins_ambiguous_emits_one_flagged_row():
    rel = {"source": "doj", "url": "", "body": ""}  # no venue signal at all
    resolved = [("X Inc", _match("1", "TX", 0.7), rel), ("X", _match("2", "CA", 0.9), rel)]
    name, match, _, flag, cands = lgr.resolve_twin_candidates(resolved)
    assert match.npi == "2"                      # best ratio wins the emission
    assert flag == "ambiguous_multi_candidate"
    assert {c["npi"] for c in cands} == {"1", "2"}


# ── venue helpers ────────────────────────────────────────────────────────────

def test_usao_slug_pins_state():
    assert lgr.extract_venue_state(
        {"source": "doj", "url": "https://www.justice.gov/usao-sdfl/pr/x", "body": ""}) == "FL"


def test_prose_district_single_state():
    assert lgr.extract_venue_state(
        {"source": "doj", "url": "https://www.justice.gov/archives/opa/pr/x",
         "body": "in the Southern District of Florida today"}) == "FL"


def test_prose_district_multi_state_returns_none():
    body = "the District of New Jersey and the Southern District of Florida"
    assert lgr.extract_venue_state(
        {"source": "doj", "url": "https://www.justice.gov/archives/opa/pr/x", "body": body}) is None


def test_classify_venue_match():
    assert lgr.classify_venue_match("TX", "tx") == "match"
    assert lgr.classify_venue_match("TX", "CA") == "mismatch"
    assert lgr.classify_venue_match(None, "CA") == "unknown"
    assert lgr.classify_venue_match("TX", None) == "unknown"


# ── chunking determinism / resume keys (confirm) ─────────────────────────────

def _cands(n):
    return [{"uuid": f"u{i}", "org_npi": f"{1000000000 + i}"} for i in range(n)]


def test_make_chunks_sizes():
    chunks = lco.make_chunks(_cands(120), size=50)
    assert [len(c) for c in chunks] == [50, 50, 20]


def test_make_chunks_empty():
    assert lco.make_chunks([], size=50) == []


def test_chunk_id_deterministic_and_order_independent():
    chunk = _cands(5)
    a = lco.chunk_id(chunk)
    b = lco.chunk_id(list(reversed(chunk)))
    assert a == b
    assert a != lco.chunk_id(_cands(6))       # different membership -> different id


def test_chunk_resume_skips_fully_done_chunks(tmp_path):
    chunk = _cands(3)
    out = tmp_path / "confirmed.jsonl"
    out.write_text("".join(json.dumps({"org_npi": c["org_npi"], "confirmed_label": "keep"}) + "\n"
                            for c in chunk))
    done = lco.load_done_npis(out)
    assert lco.chunk_is_done(chunk, done)
    assert not lco.chunk_is_done(_cands(4), done)   # has one npi not yet confirmed


def test_reconcile_full_parse():
    chunk = _cands(2)
    parsed = [{"org_npi": chunk[0]["org_npi"], "confirmed_label": "keep",
               "confidence": "high", "basis": "ok"},
              {"org_npi": chunk[1]["org_npi"], "confirmed_label": "reject",
               "confidence": "medium", "basis": "twin"}]
    out = lco.reconcile_chunk(chunk, parsed)
    assert [o["confirmed_label"] for o in out] == ["keep", "reject"]


def test_reconcile_parse_failure_marks_all_needs_human():
    chunk = _cands(2)
    out = lco.reconcile_chunk(chunk, None)
    assert all(o["confirmed_label"] == "needs_human" for o in out)
    assert all(o["confidence"] == "low" for o in out)


def test_reconcile_partial_parse_fallback_is_per_candidate():
    chunk = _cands(3)
    parsed = [{"org_npi": chunk[1]["org_npi"], "confirmed_label": "keep",
               "confidence": "high", "basis": "ok"},
              {"org_npi": chunk[2]["org_npi"], "confirmed_label": "not_a_verdict"}]
    out = lco.reconcile_chunk(chunk, parsed)
    assert [o["confirmed_label"] for o in out] == ["needs_human", "keep", "needs_human"]


# ── land: dedupe + backup (tmp dirs) ─────────────────────────────────────────

def _confirmed(npi, label="keep"):
    return {"org_npi": npi, "confirmed_label": label, "confidence": "high",
            "basis": "b", "defendant_name": "X", "label_class": "org", "state": "TX",
            "disposition": {"type": "settled_civil", "date": "2026-01-01", "amount_usd": 5.0},
            "scheme_summary": "s", "source_url": "https://example.gov/pr"}


def test_land_appends_keeps_only(tmp_path):
    target = tmp_path / "entity_labels.jsonl"
    summary = lla.land([_confirmed("1000000001"), _confirmed("1000000002", "reject"),
                        _confirmed("1000000003", "needs_human")],
                       target=target, date="2026-07-16")
    assert (summary["n_keeps"], summary["n_written"]) == (1, 1)
    rows = [json.loads(l) for l in target.open()]
    assert len(rows) == 1
    assert rows[0]["org_npi"] == "1000000001"
    assert rows[0]["label"] == "fraud"
    assert rows[0]["provenance"] == "haiku_harvest+sonnet_escalation+opus_confirm_2026-07-16"


def test_land_dedupes_against_existing_and_within_batch(tmp_path):
    target = tmp_path / "entity_labels.jsonl"
    target.write_text(json.dumps({"org_npi": "1000000001", "label": "fraud"}) + "\n")
    summary = lla.land([_confirmed("1000000001"),          # dup vs existing
                        _confirmed("1000000002"),
                        _confirmed("1000000002")],          # dup within batch
                       target=target, date="2026-07-16")
    assert (summary["n_written"], summary["n_skipped_dup"]) == (1, 2)
    rows = [json.loads(l) for l in target.open()]
    assert [r["org_npi"] for r in rows] == ["1000000001", "1000000002"]


def test_land_writes_timestamped_backup_first(tmp_path):
    target = tmp_path / "entity_labels.jsonl"
    original = json.dumps({"org_npi": "1000000009", "label": "fraud"}) + "\n"
    target.write_text(original)
    summary = lla.land([_confirmed("1000000002")], target=target, date="2026-07-16")
    assert summary["backup"] is not None
    bak = Path(summary["backup"])
    assert bak.exists() and bak.name.startswith("entity_labels.jsonl.bak_")
    assert bak.read_text() == original          # backup is the PRE-land content


def test_land_no_backup_on_first_run(tmp_path):
    target = tmp_path / "entity_labels.jsonl"
    summary = lla.land([_confirmed("1000000002")], target=target, date="2026-07-16")
    assert summary["backup"] is None
    assert target.exists()


def test_land_refuses_wrong_target_name(tmp_path):
    with pytest.raises(SystemExit):
        lla.land([_confirmed("1000000002")], target=tmp_path / "gold_set.jsonl",
                 date="2026-07-16")
    with pytest.raises(SystemExit):
        lla.land([_confirmed("1000000002")], target=tmp_path / "labels.jsonl",
                 date="2026-07-16")
    assert not (tmp_path / "gold_set.jsonl").exists()   # refused BEFORE any write


def test_land_preserves_existing_rows_verbatim(tmp_path):
    target = tmp_path / "entity_labels.jsonl"
    existing = {"org_npi": "1000000009", "label": "fraud", "custom_field": [1, 2]}
    target.write_text(json.dumps(existing) + "\n")
    lla.land([_confirmed("1000000002")], target=target, date="2026-07-16")
    rows = [json.loads(l) for l in target.open()]
    assert rows[0] == existing


def test_derive_date_from_filename():
    assert lla.derive_date(Path("entity_labels_CONFIRMED_2026-07-16.jsonl")) == "2026-07-16"
    assert lla.derive_date(Path("x.jsonl"), override="2026-01-02") == "2026-01-02"
    assert lla.derive_date(Path("nodate.jsonl")) == datetime.date.today().isoformat()


# ── reset-time parsing (cap gate) — all three copies ─────────────────────────

@pytest.mark.parametrize("mod", [lex, les, lco], ids=["extract", "escalate", "confirm"])
def test_parse_reset_honors_quoted_timezone(mod, monkeypatch):
    """Message says America/Los_Angeles while the machine TZ differs: the
    returned epoch must correspond to 3:30am PACIFIC (+120s buffer), i.e. the
    quoted tz wins over the machine's local tz."""
    from zoneinfo import ZoneInfo
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        ts = mod._parse_reset("session limit reached · resets 3:30am (America/Los_Angeles)")
        assert ts is not None
        back = datetime.datetime.fromtimestamp(ts - 120, tz=ZoneInfo("America/Los_Angeles"))
        assert (back.hour, back.minute) == (3, 30)
        assert ts - 120 > time.time() - 1       # always parked in the future
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


@pytest.mark.parametrize("mod", [lex, les, lco], ids=["extract", "escalate", "confirm"])
def test_parse_reset_pm_and_noon_midnight(mod):
    ts = mod._parse_reset("resets 12:00am (UTC)")
    from zoneinfo import ZoneInfo
    back = datetime.datetime.fromtimestamp(ts - 120, tz=ZoneInfo("UTC"))
    assert (back.hour, back.minute) == (0, 0)      # 12am == 00:xx
    ts = mod._parse_reset("resets 1:05pm (UTC)")
    back = datetime.datetime.fromtimestamp(ts - 120, tz=ZoneInfo("UTC"))
    assert (back.hour, back.minute) == (13, 5)     # pm adds 12


def test_parse_reset_no_tz_falls_back_to_local():
    ts = lex._parse_reset("resets 11:59pm")
    assert ts is not None and ts > time.time()


def test_parse_reset_unparseable_returns_none():
    assert lex._parse_reset("some other error message") is None
    assert lex._parse_reset("") is None


def test_parse_reset_bad_tz_name_degrades_gracefully():
    ts = lex._parse_reset("resets 11:59pm (Not/AZone)")
    assert ts is not None and ts > time.time()


# ── misc plumbing ────────────────────────────────────────────────────────────

def test_default_out_path():
    p = les.default_out_path(Path("/x/doj_haiku_extractions_2026-07-14.jsonl"))
    assert p == Path("/x/doj_haiku_extractions_2026-07-14.escalated.jsonl")


def test_escalate_select_todo_joins_and_skips(tmp_path):
    rows = [
        _row({"escalate": True, "confidence": "high"}),                          # selected, has body
        {"uuid": "u2", "extract": {"escalate": True, "confidence": "high"}},     # selected, NO body
        {"uuid": "u3", "extract": {"escalate": False, "confidence": "high"}},    # not selected
    ]
    raw_cache = {"u1": {"uuid": "u1", "body": "<p>full body</p>"}}
    todo, skipped_no_body = les.select_todo(rows, raw_cache, done_uuids=set())
    assert [r.get("uuid") for r, _ in todo] == ["u1"]
    assert skipped_no_body == 1
    todo2, _ = les.select_todo(rows, raw_cache, done_uuids={"u1"})
    assert todo2 == []


def test_ground_funnel_invariant_documented():
    """The funnel contract: n_filtered decomposes exactly into the four drop
    buckets + written. (Enforced arithmetically in run(); spot-checked here
    on the counter names the .md/report rely on.)"""
    funnel_keys = {"n_in", "n_filtered", "n_nppes_none", "n_dup",
                   "n_venue_dropped", "n_no_surface", "n_written"}
    import inspect
    src = inspect.getsource(lgr.run)
    for k in funnel_keys:
        assert f'"{k}"' in src
