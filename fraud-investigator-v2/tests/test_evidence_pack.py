"""Step-0 evidence pack (E1) — builder unit tests.

Synthetic panels only (no real data, no network, no subprocess): the
subprocess runner is injected, so these tests verify section assembly,
year gating, calibration redaction, co-biller characterization, and
idempotent caching on a clean CI runner.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from fraud_investigator.evidence_pack import (
    PREWARM_SECTION_CHAR_CAP,
    build_evidence_pack,
    ensure_evidence_pack,
    extract_npi,
    render_pack_markdown,
)

TARGET = "1111111111"
CO_MATCH = "2222222222"    # bills the target's anomalous code
CO_DIVERGE = "3333333333"  # same address, different codes

ST1, ZIP5 = "100 MAIN ST STE 4", "63031"


def _mk_panels(root: Path, years=(2019, 2020)) -> None:
    proc = root / "data" / "processed"
    proc.mkdir(parents=True)
    for y in years:
        pd.DataFrame([
            # target + two co-billers at the same address + one elsewhere
            dict(Rndrng_NPI=TARGET, Rndrng_Prvdr_Last_Org_Name="TARGETSON",
                 Rndrng_Prvdr_First_Name="TED", Rndrng_Prvdr_Type="Podiatry",
                 Rndrng_Prvdr_St1=ST1, Rndrng_Prvdr_Zip5=ZIP5,
                 Rndrng_Prvdr_State_Abrvtn="MO", Tot_Benes=100, Tot_Srvcs=5000,
                 Tot_HCPCS_Cds=9, Tot_Mdcr_Pymt_Amt=900000.0,
                 Drug_Mdcr_Pymt_Amt=1000.0),
            dict(Rndrng_NPI=CO_MATCH, Rndrng_Prvdr_Last_Org_Name="MATCHER",
                 Rndrng_Prvdr_First_Name="MIA", Rndrng_Prvdr_Type="Podiatry",
                 Rndrng_Prvdr_St1=ST1, Rndrng_Prvdr_Zip5=ZIP5,
                 Rndrng_Prvdr_State_Abrvtn="MO", Tot_Benes=80, Tot_Srvcs=4000,
                 Tot_HCPCS_Cds=7, Tot_Mdcr_Pymt_Amt=700000.0,
                 Drug_Mdcr_Pymt_Amt=0.0),
            dict(Rndrng_NPI=CO_DIVERGE, Rndrng_Prvdr_Last_Org_Name="DIVERGER",
                 Rndrng_Prvdr_First_Name="DAN", Rndrng_Prvdr_Type="Podiatry",
                 Rndrng_Prvdr_St1=ST1, Rndrng_Prvdr_Zip5=ZIP5,
                 Rndrng_Prvdr_State_Abrvtn="MO", Tot_Benes=90, Tot_Srvcs=900,
                 Tot_HCPCS_Cds=12, Tot_Mdcr_Pymt_Amt=120000.0,
                 Drug_Mdcr_Pymt_Amt=0.0),
            dict(Rndrng_NPI="4444444444", Rndrng_Prvdr_Last_Org_Name="ELSEWHERE",
                 Rndrng_Prvdr_First_Name="EVE", Rndrng_Prvdr_Type="Podiatry",
                 Rndrng_Prvdr_St1="9 OTHER RD", Rndrng_Prvdr_Zip5="99999",
                 Rndrng_Prvdr_State_Abrvtn="MO", Tot_Benes=50, Tot_Srvcs=500,
                 Tot_HCPCS_Cds=5, Tot_Mdcr_Pymt_Amt=50000.0,
                 Drug_Mdcr_Pymt_Amt=0.0),
        ]).to_parquet(proc / f"cms_partb_{y}.parquet")

        svc_rows = [
            dict(Rndrng_NPI=TARGET, HCPCS_Cd="87798", HCPCS_Desc="NAA molecular test",
                 Tot_Benes=100, Tot_Srvcs=4000, Tot_Bene_Day_Srvcs=3900,
                 Avg_Mdcr_Pymt_Amt=200.0),
            dict(Rndrng_NPI=TARGET, HCPCS_Cd="99214", HCPCS_Desc="Office visit est lvl 4",
                 Tot_Benes=100, Tot_Srvcs=1000, Tot_Bene_Day_Srvcs=1000,
                 Avg_Mdcr_Pymt_Amt=100.0),
            dict(Rndrng_NPI=CO_MATCH, HCPCS_Cd="87798", HCPCS_Desc="NAA molecular test",
                 Tot_Benes=80, Tot_Srvcs=3500, Tot_Bene_Day_Srvcs=3400,
                 Avg_Mdcr_Pymt_Amt=200.0),
            dict(Rndrng_NPI=CO_DIVERGE, HCPCS_Cd="11721", HCPCS_Desc="Debride nail 6+",
                 Tot_Benes=90, Tot_Srvcs=900, Tot_Bene_Day_Srvcs=900,
                 Avg_Mdcr_Pymt_Amt=40.0),
        ]
        pd.DataFrame(svc_rows).to_parquet(proc / f"cms_partb_svc_{y}.parquet")


class _FakeRunner:
    """Records subprocess calls; returns canned success output."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd, timeout=None, cwd=None):
        self.calls.append(list(cmd))
        return 0, f"FAKE OUTPUT for {' '.join(cmd[-3:])}", ""


# ── extract_npi ──────────────────────────────────────────────────────────────

def test_extract_npi():
    assert extract_npi("Dr. Foo Bar (NPI 1234567893) — podiatry, MO") == "1234567893"
    assert extract_npi("no npi here") is None
    assert extract_npi("11-digit 12345678901 run is not an NPI") is None


# ── live-mode pack ───────────────────────────────────────────────────────────

def test_live_pack_sections(tmp_path):
    _mk_panels(tmp_path)
    runner = _FakeRunner()
    pack = build_evidence_pack(TARGET, root=tmp_path, runner=runner)

    by_name = {s["name"]: s for s in pack["sections"]}
    # Panel-derived sections OK
    assert by_name["target_profile"]["status"] == "ok"
    assert "TARGETSON" in by_name["target_profile"]["body"]
    assert by_name["target_top_codes"]["status"] == "ok"
    assert "87798" in by_name["target_top_codes"]["body"]

    # FALSITY-ANCHOR AVAILABILITY (failure_mode_taxonomy_2026-07.md #2) —
    # resolves against the real (R2-backed) LCD/NCD parquets via anchor_check.py;
    # status is "ok" whether or not an anchor actually exists for 87798 — the
    # section always reports SOMETHING for a resolvable top code, never blank.
    assert by_name["falsity_anchor"]["status"] == "ok"
    assert "87798" in by_name["falsity_anchor"]["body"]

    # Step-2B co-biller characterization: both co-billers, overlap vs NONE
    cob = by_name["cobillers"]
    assert cob["status"] == "ok"
    assert CO_MATCH in cob["body"] and CO_DIVERGE in cob["body"]
    assert "87798" in cob["body"]          # matcher's overlap on target's code
    assert "NONE" in cob["body"]           # diverger has no overlap
    assert "4444444444" not in cob["body"]  # different address — excluded
    assert set(pack["cobiller_npis"]) == {CO_MATCH, CO_DIVERGE}

    # PECOS pulled for target, the reassignment roster, AND every co-biller
    # (the shared-TIN screen) — T3a adds pecos_reassignment_roster.
    pecos_sections = [n for n in by_name if n.startswith("pecos_")]
    assert "pecos_target" in pecos_sections and "pecos_reassignment_roster" in pecos_sections
    assert len(pecos_sections) == 4
    called = {" ".join(c) for c in runner.calls}
    assert any(CO_MATCH in c for c in called)
    assert any(CO_DIVERGE in c for c in called)
    assert any("--reassignments" in c and TARGET in c for c in called)

    # Live tool sections succeed via the fake runner
    for name in ("nppes", "revocations", "ca_exclusion", "trajectory",
                 "medicaid_compare", "hcpcs_breakdown",
                 "entity_graph", "medicaid_org_preflight"):
        assert by_name[name]["status"] == "ok", name

    # T3a pre-warm sections
    assert by_name["nppes_address_sweep"]["status"] == "ok"
    assert ZIP5 in by_name["nppes_address_sweep"]["title"]
    assert by_name["pecos_reassignment_roster"]["status"] == "ok"
    assert by_name["lcd_article_roster"]["status"] == "ok"
    assert "87798" in by_name["lcd_article_roster"]["body"]
    for name in ("nppes_address_sweep", "pecos_reassignment_roster", "lcd_article_roster"):
        assert len(by_name[name]["body"]) <= PREWARM_SECTION_CHAR_CAP + 60, name  # + truncation marker

    md = render_pack_markdown(pack)
    assert "Step-0 deterministic evidence pack" in md
    assert "[ok]" in md


def test_live_pack_tool_failure_is_section_local(tmp_path):
    _mk_panels(tmp_path)

    def failing_runner(cmd, timeout=None, cwd=None):
        return 1, "", "boom"

    pack = build_evidence_pack(TARGET, root=tmp_path, runner=failing_runner)
    by_name = {s["name"]: s for s in pack["sections"]}
    assert by_name["nppes"]["status"] == "unavailable"
    assert "boom" in by_name["nppes"]["body"]
    # Panel sections unaffected by tool failures
    assert by_name["target_profile"]["status"] == "ok"


# ── calibration mode ─────────────────────────────────────────────────────────

def test_calibration_pack_gates_years_and_redacts_live(tmp_path):
    _mk_panels(tmp_path, years=(2019, 2020))
    runner = _FakeRunner()
    pack = build_evidence_pack(TARGET, root=tmp_path, as_of="2020-06-01",
                               runner=runner)

    # Year gate: strictly before the cutoff YEAR (2020 panel excluded)
    assert pack["panel_years_used"] == [2019]

    by_name = {s["name"]: s for s in pack["sections"]}
    assert by_name["target_profile"]["status"] == "ok"          # 2019 only
    assert "| 2020 |" not in by_name["target_profile"]["body"]
    assert by_name["cobillers"]["status"] == "ok"
    # Falsity-anchor check is point-in-time gated (effective_on=as_of), not
    # redacted — HCPCS coverage history is queryable per-date, but "current"
    # scope would leak post-cutoff LCD/NCD publication as a live fact.
    assert by_name["falsity_anchor"]["status"] == "ok"
    # T3a: the LCD/article roster mirrors that EXACT gating (point-in-time,
    # never redacted) — it's a versioned regulatory corpus, not a live
    # current-state registry.
    assert by_name["lcd_article_roster"]["status"] == "ok"

    # EVERY live/current-registry section redacted; runner never invoked.
    # T3a's PECOS reassignment roster + NPPES address sweep are genuinely
    # LIVE (current enrollment / current registry) — redacted like the rest.
    for name in ("nppes", "nppes_address_sweep", "pecos_target",
                 "pecos_reassignment_roster", "leie", "revocations",
                 "ca_exclusion", "trajectory", "medicaid_compare",
                 "hcpcs_breakdown", "entity_graph", "medicaid_org_preflight"):
        assert by_name[name]["status"] == "redacted_calibration", name
    assert runner.calls == []
    assert not any(n.startswith("pecos_cobiller") and
                   by_name[n]["status"] != "redacted_calibration"
                   for n in by_name)


# ── T3a pre-warm sections (unit-level) ───────────────────────────────────────

def test_cap_body_truncates_long_sections():
    from fraud_investigator.evidence_pack import Section, _cap_body
    long_body = "x" * (PREWARM_SECTION_CHAR_CAP + 500)
    section = Section("s", "s", "ok", "cmd", long_body)
    capped = _cap_body(section)
    assert len(capped.body) <= PREWARM_SECTION_CHAR_CAP + 60  # + truncation marker
    assert capped.body.endswith("[truncated — pre-warm sections stay lean]")


def test_cap_body_leaves_short_sections_untouched():
    from fraud_investigator.evidence_pack import Section, _cap_body
    section = Section("s", "s", "ok", "cmd", "short body")
    assert _cap_body(section).body == "short body"


def test_nppes_address_sweep_unavailable_when_npi_absent_from_panel(tmp_path):
    """A target with no gated-panel row at all (no zip resolvable) yields an
    explicit `unavailable` section — never a bogus live/redacted call."""
    _mk_panels(tmp_path)
    pack = build_evidence_pack("9999999999", root=tmp_path, runner=_FakeRunner())
    by_name = {s["name"]: s for s in pack["sections"]}
    assert by_name["nppes_address_sweep"]["status"] == "unavailable"
    assert "not resolvable" in by_name["nppes_address_sweep"]["body"]


# ── solo practice ────────────────────────────────────────────────────────────

def test_solo_practice_cobiller_section(tmp_path):
    _mk_panels(tmp_path)
    # rebuild panels with only the target at its address
    import duckdb  # noqa: F401  (ensure dep present)
    proc = tmp_path / "data" / "processed"
    for y in (2019, 2020):
        df = pd.read_parquet(proc / f"cms_partb_{y}.parquet")
        df = df[~df.Rndrng_NPI.isin([CO_MATCH, CO_DIVERGE])]
        df.to_parquet(proc / f"cms_partb_{y}.parquet")
    pack = build_evidence_pack(TARGET, root=tmp_path, runner=_FakeRunner())
    cob = next(s for s in pack["sections"] if s["name"] == "cobillers")
    assert cob["status"] == "ok"
    assert "NO other Part B billers" in cob["body"]
    assert pack["cobiller_npis"] == []


# ── ensure_evidence_pack (idempotent cache) ──────────────────────────────────

def test_ensure_pack_idempotent(tmp_path):
    _mk_panels(tmp_path)
    case_dir = tmp_path / "state" / "CASE-X"
    runner = _FakeRunner()
    md1 = ensure_evidence_pack(npi=TARGET, case_dir=case_dir, root=tmp_path,
                               runner=runner)
    assert md1 and (case_dir / "evidence_pack.json").exists()
    n_calls = len(runner.calls)
    assert n_calls > 0
    md2 = ensure_evidence_pack(npi=TARGET, case_dir=case_dir, root=tmp_path,
                               runner=runner)
    assert md2 == md1
    assert len(runner.calls) == n_calls  # cached — no rebuild
