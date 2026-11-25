"""Step-0 deterministic evidence pack (E1).

Pre-computes, BEFORE the first investigator/discover call, everything about a
target NPI that is mechanical lookup rather than judgment: identity, PECOS
enrollment (target + every co-biller), exclusion/enforcement checks, the
multi-year Part B profile, the per-code breakdown, and — the historically
most-skipped mandatory step — the full co-biller billing characterization
(investigator.md Step 2B). The orchestrator injects the rendered pack into
the discover + verify prompts; the agents interpret it and chase what it
can't answer instead of burning tool turns re-deriving it.

Why this exists: the investigator prompt mandates ~30 discrete queries
against a 25-turn budget — an internal contradiction that caused silent,
uncontrolled step-skipping (notably Step 2B, whose absence flips case theory
between single-NPI and conspiracy framings). Deterministic pre-computation
makes the mechanical steps unskippable and frees the whole turn budget for
open-web work and weird-thread chasing.

Calibration safety: when ``as_of`` is set the builder emits ONLY
panel-derived sections (the calibration export's panels are already
fail-closed pre-cutoff), year-gated to years strictly before the cutoff
year, and REDACTS every live-API / current-day-registry section (NPPES,
PECOS, LEIE, revocations, CA S&I, Medicaid T-MSIS, cross-program
trajectory) — a current exclusion or enrollment status is exactly the kind
of post-cutoff fact that leaks the answer. Redacted sections say so
explicitly so the jailed agent knows the gap is intentional.

Every section carries the literal command / query that produced it, so
evidence items citing the pack stay reproducible per the investigator's
citation discipline.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

SECTION_CHAR_CAP = 4_000          # per-section body cap in the rendered pack
PACK_CHAR_CAP = 60_000            # whole-pack cap (defensive)
SUBPROCESS_TIMEOUT_SEC = 120
MAX_COBILLERS = 12                # characterize at most this many co-billers
MAX_PECOS_COBILLERS = 10
TOP_CODES = 12

# Live-API / current-day-registry tools — NEVER run in calibration mode
# (their output reflects the present and can leak the answer).
_LIVE_ONLY_NOTE = (
    "REDACTED (calibration mode): this section reads a live API or a "
    "current-day registry whose present-day state can leak post-cutoff "
    "facts. Use the sandboxed tools per the calibration protocol if the "
    "case genuinely needs it."
)


@dataclass
class Section:
    name: str
    title: str
    status: str            # ok | unavailable | redacted_calibration
    command: str           # reproducible provenance (citation discipline)
    body: str = ""

    def to_markdown(self) -> str:
        body = self.body.strip()
        if len(body) > SECTION_CHAR_CAP:
            body = body[:SECTION_CHAR_CAP] + "\n…[truncated]"
        head = f"### {self.title}  `[{self.status}]`\n*source:* `{self.command}`\n"
        return head + ("\n" + body + "\n" if body else "\n")


def extract_npi(subject: str) -> str | None:
    """Pull the first plausible NPI (10 digits, not part of a longer run) out
    of the case subject line."""
    m = re.search(r"(?<!\d)(\d{10})(?!\d)", subject or "")
    return m.group(1) if m else None


# ── panel access ─────────────────────────────────────────────────────────────

def _panel(root: Path, family: str, year: int) -> Path | None:
    for sub in ("processed", "panels"):
        p = root / "data" / sub / f"{family}_{year}.parquet"
        if p.exists():
            return p
    return None


def _years_available(root: Path, family: str) -> list[int]:
    yrs = set()
    for sub in ("processed", "panels"):
        d = root / "data" / sub
        if d.is_dir():
            for p in d.glob(f"{family}_*.parquet"):
                m = re.fullmatch(rf"{family}_(\d{{4}})", p.stem)
                if m:
                    yrs.add(int(m.group(1)))
    return sorted(yrs)


def _gate_years(years: list[int], as_of: str | None) -> list[int]:
    """Calibration year gate: strictly before the cutoff year (a year-Y panel
    describes billing through Dec Y, which post-dates an as-of mid-Y)."""
    if not as_of:
        return years
    cutoff_year = int(as_of[:4])
    return [y for y in years if y < cutoff_year]


def _duckdb():
    import duckdb
    return duckdb


# ── subprocess runner (injectable for tests) ────────────────────────────────

def run_cmd(cmd: list[str], timeout: int = SUBPROCESS_TIMEOUT_SEC,
            cwd: "Path | None" = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=str(cwd) if cwd else None)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except OSError as e:
        return 127, "", str(e)


def _tool_section(name: str, title: str, script_args: list[str], *,
                  root: Path, as_of: str | None, runner,
                  gateable: bool = False) -> Section:
    cmd = [sys.executable] + script_args
    cmd_str = " ".join(["python"] + script_args)
    if as_of and not gateable:
        return Section(name, title, "redacted_calibration", cmd_str,
                       _LIVE_ONLY_NOTE)
    if as_of and gateable:
        # Cutoff-gated Medicaid tool (medicaid_cutoff.py): run from the REAL
        # repo (absolute script path) so it finds the T-MSIS parquet — the
        # calibration export holds only Part-B panels. Leak-safety is the env
        # cutoff FRAUD_INV_MEDICAID_CUTOFF, set by the orchestrator from as_of
        # and inherited here, which restricts every read to before the cutoff.
        # FAIL-CLOSED: only run when the gate is provably active; if the env is
        # missing (a pack built outside a gated run), redact rather than leak.
        from fraudlib.medicaid_cutoff import cutoff_iso
        if not cutoff_iso():
            return Section(name, title, "redacted_calibration", cmd_str,
                           _LIVE_ONLY_NOTE + " (medicaid cutoff gate inactive)")
        repo = Path(__file__).resolve().parents[3]
        abs_cmd = [sys.executable, str(repo / script_args[0])] + script_args[1:]
        rc, out, err = runner(abs_cmd, SUBPROCESS_TIMEOUT_SEC, repo)
        note = f"\n\n[POINT-IN-TIME: Medicaid reads gated to before {as_of}]"
        if rc == 0 and out.strip():
            return Section(name, title, "ok", cmd_str, out + note)
        detail = (err or out or "no output").strip()[-600:]
        return Section(name, title, "unavailable", cmd_str,
                       f"gated tool failed (exit {rc}): {detail}")
    rc, out, err = runner(cmd, SUBPROCESS_TIMEOUT_SEC, root)
    if rc == 0 and out.strip():
        return Section(name, title, "ok", cmd_str, out)
    detail = (err or out or "no output").strip()[-600:]
    return Section(name, title, "unavailable", cmd_str,
                   f"tool failed (exit {rc}): {detail}")


# ── panel-derived sections (work in BOTH modes; year-gated) ─────────────────

def _fmt_money(x) -> str:
    try:
        return f"${float(x):,.0f}"
    except (TypeError, ValueError):
        return str(x)


def _target_profile(root: Path, npi: str, years: list[int]) -> Section:
    duckdb = _duckdb()
    rows = []
    for y in years[-6:]:
        p = _panel(root, "cms_partb", y)
        if not p:
            continue
        df = duckdb.sql(
            "SELECT Rndrng_Prvdr_Last_Org_Name AS last, Rndrng_Prvdr_First_Name AS first, "
            "Rndrng_Prvdr_Type AS type, Rndrng_Prvdr_St1 AS st1, Rndrng_Prvdr_Zip5 AS zip, "
            "Rndrng_Prvdr_State_Abrvtn AS state, Tot_Benes, Tot_Srvcs, Tot_HCPCS_Cds, "
            "Tot_Mdcr_Pymt_Amt, Drug_Mdcr_Pymt_Amt "
            f"FROM read_parquet('{p}') WHERE Rndrng_NPI = ?", params=[npi]).df()
        if len(df):
            r = df.iloc[0]
            rows.append(
                f"| {y} | {r['last']}, {r['first']} | {r['type']} | {r['st1']}, "
                f"{r['zip']} {r['state']} | {int(float(r['Tot_Benes']))} | "
                f"{int(float(r['Tot_Srvcs']))} | {int(float(r['Tot_HCPCS_Cds']))} | "
                f"{_fmt_money(r['Tot_Mdcr_Pymt_Amt'])} | {_fmt_money(r['Drug_Mdcr_Pymt_Amt'])} |")
    cmd = "duckdb: SELECT … FROM cms_partb_{year}.parquet WHERE Rndrng_NPI = '<npi>'"
    if not rows:
        return Section("target_profile", "Target Part B profile (per year)",
                       "unavailable", cmd, "NPI not present in any gated Part B panel year.")
    body = ("| year | name | type | practice address | benes | srvcs | codes | "
            "total paid | drug paid |\n|---|---|---|---|---|---|---|---|---|\n"
            + "\n".join(rows))
    return Section("target_profile", "Target Part B profile (per year)", "ok", cmd, body)


def _target_top_codes(root: Path, npi: str, years: list[int]) -> Section:
    duckdb = _duckdb()
    cmd = ("duckdb: top codes by est. payment FROM cms_partb_svc_{year}.parquet "
           "WHERE Rndrng_NPI = '<npi>' (est_paid = Avg_Mdcr_Pymt_Amt * Tot_Srvcs)")
    parts = []
    for y in [yy for yy in (years[-1:], years[-4:-3]) for yy in yy]:
        p = _panel(root, "cms_partb_svc", y)
        if not p:
            continue
        df = duckdb.sql(
            "SELECT HCPCS_Cd AS code, substr(HCPCS_Desc, 1, 60) AS descr, "
            "Tot_Benes, Tot_Srvcs, Tot_Bene_Day_Srvcs, "
            "Avg_Mdcr_Pymt_Amt * Tot_Srvcs AS est_paid "
            f"FROM read_parquet('{p}') WHERE Rndrng_NPI = ? "
            "ORDER BY est_paid DESC LIMIT ?", params=[npi, TOP_CODES]).df()
        if not len(df):
            continue
        lines = [f"**{y}** (svc/bene and bene-day/srvc ratios are the "
                 "distinct-dates / phantom-volume screens)",
                 "| code | descriptor | benes | srvcs | srvc/bene | bene-day-srvcs | est paid |",
                 "|---|---|---|---|---|---|---|"]
        for _, r in df.iterrows():
            benes, srvcs = float(r["Tot_Benes"]), float(r["Tot_Srvcs"])
            ratio = f"{srvcs / benes:.1f}" if benes else "—"
            lines.append(f"| {r['code']} | {r['descr']} | {int(benes)} | {int(srvcs)} "
                         f"| {ratio} | {int(float(r['Tot_Bene_Day_Srvcs']))} | {_fmt_money(r['est_paid'])} |")
        parts.append("\n".join(lines))
    if not parts:
        return Section("target_top_codes", "Target top HCPCS codes", "unavailable",
                       cmd, "NPI not present in any gated by-service panel year.")
    return Section("target_top_codes", "Target top HCPCS codes", "ok", cmd,
                   "\n\n".join(parts))


def _top_hcpcs_codes(root: Path, npi: str, years: list[int], limit: int = 3) -> list[str]:
    """The target's top-N codes by est. payment, most-recent gated year only —
    used to scope the falsity-anchor check to the codes that actually matter."""
    duckdb = _duckdb()
    if not years:
        return []
    p = _panel(root, "cms_partb_svc", years[-1])
    if not p:
        return []
    df = duckdb.sql(
        "SELECT HCPCS_Cd AS code FROM read_parquet(?) WHERE Rndrng_NPI = ? "
        "ORDER BY Avg_Mdcr_Pymt_Amt * Tot_Srvcs DESC LIMIT ?",
        params=[str(p), npi, limit]).df()
    return [str(c) for c in df["code"]] if len(df) else []


def _falsity_anchor_section(root: Path, npi: str, years: list[int],
                            as_of: str | None = None) -> Section:
    """FALSITY-ANCHOR AVAILABILITY (failure_mode_taxonomy_2026-07.md #2).

    Runs scripts/anchor_check.py on the target's top HCPCS codes so counsel sees
    the LCD/NCD/article landscape at iteration 1, before investing in a build.
    Calibration-safe: whether a coverage rule "exists" can itself be a
    post-cutoff fact (a new LCD can publish after the as-of date), so in
    calibration mode this passes ``effective_on=as_of`` to anchor_check to
    restrict to determinations in force AS OF the cutoff — never "current".
    Surfaces only; never gates a verdict."""
    cmd = "python scripts/anchor_check.py --hcpcs <top target code(s)> --format json"
    codes = _top_hcpcs_codes(root, npi, years)
    if not codes:
        return Section("falsity_anchor", "FALSITY-ANCHOR AVAILABILITY", "unavailable",
                       cmd, "No top HCPCS codes resolvable for this NPI/year.")
    try:
        import sys as _sys
        # scripts/ lives at the repo root, two levels above this package
        # (fraud-investigator-v2/src/fraud_investigator/evidence_pack.py) —
        # resolve from __file__, NOT the data `root` param (a synthetic/data
        # root in tests has no scripts/ dir, and correctly degrades below).
        _scripts = str(Path(__file__).resolve().parents[3] / "scripts")
        if _scripts not in _sys.path:
            _sys.path.insert(0, _scripts)
        import anchor_check  # noqa: PLC0415
    except Exception as e:  # pragma: no cover — defensive
        return Section("falsity_anchor", "FALSITY-ANCHOR AVAILABILITY", "unavailable",
                       cmd, f"anchor_check import failed: {e}")

    lines = []
    for code in codes:
        try:
            r = anchor_check.check_anchor(code, effective_on=as_of)
        except Exception as e:  # pragma: no cover — defensive
            lines.append(f"- `{code}`: lookup failed ({e})")
            continue
        if r["lcd_exists"] or r["ncd_exists"]:
            anchors = ", ".join(
                f"LCD {d['id']}" for d in r["lcds"]) + ", ".join(
                f"NCD {d['id']}" for d in r["ncds"])
            lines.append(f"- `{code}`: **coverage rule exists** ({anchors.strip(', ')}) — "
                         "a criteria-failure falsity theory is available.")
        elif r["article_only"]:
            lines.append(f"- `{code}`: **article only, no LCD/NCD** "
                         f"({', '.join(a['id'] for a in r['articles'])}) — billing guidance "
                         "exists but no coverage-criteria hook; falsity must rest on a "
                         "per-se statutory/regulatory ground or confirmed-fact evidence.")
        else:
            lines.append(f"- `{code}`: **NO LCD/NCD/article found** — no coverage-criteria "
                         "falsity theory is available for this code; a per-se hook "
                         "(PECOS mismatch, scope-of-practice, supervision, manual "
                         "non-coverage, CLIA absence) or confirmed-fact evidence is required "
                         "before iterating further on this code.")
    body = ("\n".join(lines) + "\n\nCorpus-wide finding (failure_mode_taxonomy_2026-07.md): "
            "every filed count in the sweep was anchored on a public coverage/enrollment "
            "rule — absence of BOTH a coverage rule and a per-se hook has been a terminal "
            "wall (CASE-6A4588BF). This section surfaces the landscape; it does not gate "
            "the build.")
    return Section("falsity_anchor", "FALSITY-ANCHOR AVAILABILITY", "ok", cmd, body)


def _cobiller_characterization(root: Path, npi: str, years: list[int]) -> tuple[Section, list[str]]:
    """Step 2B, made unskippable: every co-located biller's profile, their
    volume on the TARGET's top codes, and a 3-year payment trajectory.
    Returns (section, cobiller_npis) so PECOS can be pulled for each."""
    duckdb = _duckdb()
    cmd = ("duckdb: co-billers = cms_partb_{year}.parquet rows sharing the target's "
           "(Rndrng_Prvdr_St1, Rndrng_Prvdr_Zip5); per co-biller top codes + volume on "
           "target's top-5 codes FROM cms_partb_svc_{year}.parquet")
    if not years:
        return Section("cobillers", "Co-biller characterization (Step 2B)",
                       "unavailable", cmd, "No gated panel years available."), []
    y = years[-1]
    pb, psvc = _panel(root, "cms_partb", y), _panel(root, "cms_partb_svc", y)
    if not pb:
        return Section("cobillers", "Co-biller characterization (Step 2B)",
                       "unavailable", cmd, f"cms_partb_{y}.parquet not found."), []

    tgt = duckdb.sql(
        f"SELECT Rndrng_Prvdr_St1 AS st1, Rndrng_Prvdr_Zip5 AS zip "
        f"FROM read_parquet('{pb}') WHERE Rndrng_NPI = ?", params=[npi]).df()
    if not len(tgt):
        return Section("cobillers", "Co-biller characterization (Step 2B)",
                       "unavailable", cmd, f"Target NPI absent from cms_partb_{y}."), []
    st1, zip5 = tgt.iloc[0]["st1"], tgt.iloc[0]["zip"]

    co = duckdb.sql(
        f"SELECT Rndrng_NPI AS npi, Rndrng_Prvdr_Last_Org_Name AS last, "
        f"Rndrng_Prvdr_First_Name AS first, Rndrng_Prvdr_Type AS type, "
        f"Tot_Benes, Tot_Srvcs, Tot_Mdcr_Pymt_Amt "
        f"FROM read_parquet('{pb}') "
        f"WHERE Rndrng_Prvdr_St1 = ? AND Rndrng_Prvdr_Zip5 = ? AND Rndrng_NPI <> ? "
        f"ORDER BY Tot_Mdcr_Pymt_Amt DESC LIMIT ?",
        params=[st1, zip5, npi, MAX_COBILLERS]).df()

    if not len(co):
        body = (f"Address sweep ({y}): NO other Part B billers at `{st1}, {zip5}`. "
                "This is the address-sweep anchor for a `single_npi` analysis-unit "
                "choice — cite this section.")
        return Section("cobillers", "Co-biller characterization (Step 2B)", "ok", cmd, body), []

    target_codes: list[str] = []
    if psvc:
        tdf = duckdb.sql(
            f"SELECT HCPCS_Cd FROM read_parquet('{psvc}') WHERE Rndrng_NPI = ? "
            "ORDER BY Avg_Mdcr_Pymt_Amt * Tot_Srvcs DESC LIMIT 5", params=[npi]).df()
        target_codes = [str(c) for c in tdf["HCPCS_Cd"]]

    lines = [f"Address sweep ({y}) at `{st1}, {zip5}` — {len(co)} co-biller(s) "
             f"(cap {MAX_COBILLERS}). Target's top-5 codes: {', '.join(target_codes) or 'n/a'}.",
             "", "| co-biller NPI | name | type | benes | srvcs | total paid "
             f"| paid trajectory {years[-3:] if len(years) >= 3 else years} "
             "| their top-3 codes | volume on TARGET's top-5 codes |",
             "|---|---|---|---|---|---|---|---|---|"]
    npis: list[str] = []
    for _, r in co.iterrows():
        c_npi = str(r["npi"])
        npis.append(c_npi)
        top3, overlap = "—", "NONE"
        if psvc:
            t3 = duckdb.sql(
                f"SELECT HCPCS_Cd, CAST(Tot_Srvcs AS BIGINT) s FROM read_parquet('{psvc}') "
                "WHERE Rndrng_NPI = ? ORDER BY Avg_Mdcr_Pymt_Amt * Tot_Srvcs DESC LIMIT 3",
                params=[c_npi]).df()
            top3 = "; ".join(f"{a}×{b}" for a, b in zip(t3["HCPCS_Cd"], t3["s"])) or "—"
            if target_codes:
                ph = ",".join("?" for _ in target_codes)
                ov = duckdb.sql(
                    f"SELECT HCPCS_Cd, CAST(Tot_Srvcs AS BIGINT) s FROM read_parquet('{psvc}') "
                    f"WHERE Rndrng_NPI = ? AND HCPCS_Cd IN ({ph})",
                    params=[c_npi] + target_codes).df()
                overlap = "; ".join(f"{a}×{b}" for a, b in zip(ov["HCPCS_Cd"], ov["s"])) or "NONE"
        traj = []
        for ty in years[-3:]:
            tp = _panel(root, "cms_partb", ty)
            if tp:
                td = duckdb.sql(f"SELECT Tot_Mdcr_Pymt_Amt FROM read_parquet('{tp}') "
                                "WHERE Rndrng_NPI = ?", params=[c_npi]).df()
                traj.append(_fmt_money(td.iloc[0, 0]) if len(td) else "absent")
        lines.append(
            f"| {c_npi} | {r['last']}, {r['first']} | {r['type']} | "
            f"{int(float(r['Tot_Benes']))} | {int(float(r['Tot_Srvcs']))} | "
            f"{_fmt_money(r['Tot_Mdcr_Pymt_Amt'])} | {' → '.join(traj) or '—'} | {top3} | {overlap} |")
    lines += ["", "Per Step 2B you must still assign each co-biller a categorical verdict "
              "(`MATCHES_TARGET_ANOMALY` / `DIVERGES_FROM_TARGET` / `INSUFFICIENT_DATA`) — "
              "the numbers above are the inputs; the judgment is yours. `NONE` overlap on "
              "the target's anomalous codes is divergence evidence (within-practice control)."]
    return Section("cobillers", "Co-biller characterization (Step 2B)", "ok", cmd,
                   "\n".join(lines)), npis


def _leie_section(root: Path, npi: str, as_of: str | None) -> Section:
    cmd = "duckdb: SELECT * FROM data/processed/leie.parquet WHERE NPI = '<npi>'"
    title = "LEIE exclusion check (investigative only — NOT a label source)"
    if as_of:
        return Section("leie", title, "redacted_calibration", cmd, _LIVE_ONLY_NOTE)
    p = root / "data" / "processed" / "leie.parquet"
    if not p.exists():
        return Section("leie", title, "unavailable", cmd, "leie.parquet not found locally.")
    duckdb = _duckdb()
    cols = [r[0] for r in duckdb.sql(f"DESCRIBE SELECT * FROM read_parquet('{p}')").fetchall()]
    npi_col = next((c for c in cols if c.lower() == "npi"), None)
    if not npi_col:
        return Section("leie", title, "unavailable", cmd, f"no NPI column in leie.parquet ({cols[:8]}…)")
    df = duckdb.sql(f"SELECT * FROM read_parquet('{p}') WHERE CAST({npi_col} AS VARCHAR) = ?",
                    params=[npi]).df()
    body = (f"NO LEIE row for NPI {npi}. (Expected for a novel-detection target — "
            "uninformative either way per foreclosure_check cardinal rules; name-based "
            "fuzzy matching on affiliates is still the investigator's job.)"
            if not len(df) else df.to_string(index=False))
    return Section("leie", title, "ok", cmd, body)


# ── T3a pre-warm sections ────────────────────────────────────────────────────
# Three MORE guarded sections, added on top of the original pack. Kept to a
# tighter cap than the pack's default SECTION_CHAR_CAP (4000) since they're
# additive on an already-large pack (up to MAX_COBILLERS co-billers + up to
# MAX_PECOS_COBILLERS PECOS lookups already). Two of the three (PECOS
# reassignment roster, NPPES address sweep) are genuinely LIVE current-state
# registry reads — gated OUT of calibration via the same `_tool_section`
# full-redaction mechanism the existing nppes/pecos_target/etc. sections use.
# The third (LCD/article roster) mirrors `_falsity_anchor_section`'s gating
# EXACTLY instead: point-in-time via effective_on=as_of, never fully
# redacted — a coverage determination is a dateable, versioned regulatory
# fact (queryable as-of any date), not a live current-state registry.
PREWARM_SECTION_CHAR_CAP = 2_000


def _cap_body(section: Section, cap: int = PREWARM_SECTION_CHAR_CAP) -> Section:
    if len(section.body) > cap:
        section.body = section.body[:cap] + "\n…[truncated — pre-warm sections stay lean]"
    return section


def _target_postal_code(root: Path, npi: str, years: list[int]) -> str | None:
    """Resolve the target's practice ZIP5 from the (already year-gated)
    panel — cheap and calibration-safe (the same zip is already exposed in
    `_target_profile`'s table). Used ONLY to scope the NPPES address sweep
    below; the sweep itself (a live registry query) is what's actually
    redacted under calibration, not this panel lookup."""
    if not years:
        return None
    duckdb = _duckdb()
    p = _panel(root, "cms_partb", years[-1])
    if not p:
        return None
    df = duckdb.sql(
        "SELECT CAST(Rndrng_Prvdr_Zip5 AS VARCHAR) AS zip FROM read_parquet(?) "
        "WHERE Rndrng_NPI = ?", params=[str(p), npi]).df()
    if not len(df):
        return None
    zip5 = (df.iloc[0]["zip"] or "")[:5]
    return zip5 or None


def _lcd_article_roster_section(root: Path, npi: str, years: list[int],
                                as_of: str | None = None) -> Section:
    """LCD/NCD/article METADATA + IDs for the target's top-2 codes — a
    compact reference roster (source/id/title), distinct from
    `_falsity_anchor_section`'s yes/no coverage-rule-exists framing.
    "Full text only if cheap": a SINGLE unambiguous determination gets one
    bounded extra lookup for a short coverage-text excerpt; a multi-hit (or
    zero-hit) roster stays metadata-only.
    """
    cmd = "python scripts/lcd_lookup.py --hcpcs <top-2 target codes> --effective-on <as_of> --format json"
    codes = _top_hcpcs_codes(root, npi, years, limit=2)
    if not codes:
        return Section("lcd_article_roster", "LCD/article metadata + IDs (top-2 codes)",
                       "unavailable", cmd, "No top HCPCS codes resolvable for this NPI/year.")
    try:
        import sys as _sys
        # scripts/ lives at the repo root, two levels above this package —
        # see _falsity_anchor_section's identical comment.
        _scripts = str(Path(__file__).resolve().parents[3] / "scripts")
        if _scripts not in _sys.path:
            _sys.path.insert(0, _scripts)
        import anchor_check  # noqa: PLC0415
        import lcd_lookup  # noqa: PLC0415
    except Exception as e:  # pragma: no cover — defensive
        return Section("lcd_article_roster", "LCD/article metadata + IDs (top-2 codes)",
                       "unavailable", cmd, f"anchor_check/lcd_lookup import failed: {e}")

    lines: list[str] = []
    all_hits: list[tuple[str, str, dict]] = []   # (code, source, {id, title})
    for code in codes:
        try:
            r = anchor_check.check_anchor(code, effective_on=as_of)
        except Exception as e:  # pragma: no cover — defensive
            lines.append(f"- `{code}`: lookup failed ({e})")
            continue
        hits = ([("lcd", d) for d in r["lcds"]] + [("ncd", d) for d in r["ncds"]]
                + [("article", d) for d in r["articles"]])
        if not hits:
            lines.append(f"- `{code}`: no LCD/NCD/article on file.")
            continue
        for source, d in hits:
            lines.append(f"- `{code}` [{source.upper()} {d['id']}]: {d['title']}")
            all_hits.append((code, source, d))

    if len(all_hits) == 1:
        code, source, d = all_hits[0]
        try:
            scope = "all" if (as_of or source == "ncd") else "current"
            df = lcd_lookup.filter_by_id(lcd_lookup._load(source, scope), source, d["id"])
            rows = lcd_lookup._summarize(df, source)
            if rows and rows[0].get("coverage_text"):
                lines.append(f"  excerpt: {rows[0]['coverage_text'][:400]}")
        except Exception:
            pass   # metadata above still stands; the excerpt is a bonus, not required

    return _cap_body(Section("lcd_article_roster", "LCD/article metadata + IDs (top-2 codes)",
                             "ok", cmd, "\n".join(lines)))


def _anchor_registry_section(npi: str) -> Section:
    """Coverage-anchor REGISTRY entries for the target's dominant codes — the
    Lab Ebony-lesson seam. The registry (data/reference/coverage_anchors.yaml,
    fraudlib.anchors) says whether each dominant code family carries a PUBLIC
    condition-of-payment a blanket/uniform pattern violates on its face
    (fileable_band) or whether falsity lives in non-public facts (referral
    track). Injecting this at Step-0 lets counsel BUILD counts around the
    anchor from iteration 1 — Lab Ebony reached SETTLEMENT_READY mid-case but
    finally failed `confirmed_false_claim` because its anchor arrived late via
    a human checkpoint instead of at count-build time.

    Calibration-safe: the registry is static repo reference data (rules all
    pre-date any cutoff we use); no live API, no post-cutoff facts.
    """
    import sys as _sys
    _outer = Path(__file__).resolve().parents[3]
    if str(_outer) not in _sys.path:
        _sys.path.insert(0, str(_outer))
    from fraudlib.anchors import anchor_for_code   # noqa: PLC0415
    from fraudlib.medicaid_cutoff import clamp_feature_year  # noqa: PLC0415
    cmd = "fraudlib.anchors.anchor_for_code(<dominant codes from medicaid features + partb>)"
    codes: list[str] = []
    # clamp_feature_year honors FRAUD_INV_MEDICAID_CUTOFF (set by calibration
    # runs) so we read a PRE-CUTOFF frame, never the enforcement-year one.
    fy = clamp_feature_year(2024)
    med = _outer / "data" / "processed" / f"medicaid_features_{fy}.parquet"
    if med.exists():
        try:
            duckdb = _duckdb()
            df = duckdb.sql(f"SELECT top_hcpcs FROM read_parquet('{med}') "
                            f"WHERE CAST(npi AS VARCHAR)='{npi}'").df()
            codes += [c for c in df["top_hcpcs"].tolist() if c]
        except Exception:
            pass
    if not codes:
        return Section("coverage_anchor_registry", "Coverage-anchor registry (fileability)",
                       "unavailable", cmd, "no dominant codes found for target")
    lines = []
    for c in dict.fromkeys(codes):
        e = anchor_for_code(c)
        if e is None:
            lines.append(f"- `{c}`: registry silent — investigate coverage rules directly.")
        elif e.get("fileable_band"):
            lines.append(
                f"- `{c}` [{e['family']}] **FILEABLE-BAND anchor**: {', '.join(e.get('anchor_ids', []))} — "
                f"{str(e.get('basis','')).strip()[:300]} (verify: {e.get('verify_via','')})")
        else:
            lines.append(
                f"- `{c}` [{e['family']}] NO public per-claim anchor "
                f"({e.get('anchor_type')}): {str(e.get('basis','')).strip()[:220]} — "
                "falsity requires non-public evidence; treat as referral-track unless found.")
    return _cap_body(Section("coverage_anchor_registry",
                             "Coverage-anchor registry (fileability of dominant codes)",
                             "ok", cmd, "\n".join(lines)))


# ── pack assembly ────────────────────────────────────────────────────────────

def build_evidence_pack(npi: str, *, root: Path, as_of: str | None = None,
                        runner=run_cmd) -> dict:
    """Build the Step-0 pack for one NPI. Failure-tolerant: every section is
    `ok`, `unavailable` (with the error), or `redacted_calibration` — the
    pack never raises for a single bad section."""
    root = Path(root)
    years = _gate_years(_years_available(root, "cms_partb"), as_of)
    sections: list[Section] = []

    def _safe(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:  # pragma: no cover — defensive
            name = getattr(fn, "__name__", "section").lstrip("_")
            return Section(name, name, "unavailable", "internal", f"builder error: {e}")

    sections.append(_safe(_target_profile, root, npi, years))
    sections.append(_safe(_target_top_codes, root, npi, years))
    sections.append(_safe(_anchor_registry_section, npi))
    sections.append(_safe(_falsity_anchor_section, root, npi, years, as_of))
    sections.append(_safe(_lcd_article_roster_section, root, npi, years, as_of))
    cob = _safe(_cobiller_characterization, root, npi, years)
    if isinstance(cob, tuple):
        cob_section, cob_npis = cob
    else:  # builder error path
        cob_section, cob_npis = cob, []
    sections.append(cob_section)

    # Live-API / current-registry sections (auto-redacted in calibration)
    sections.append(_tool_section(
        "nppes", "NPPES identity (live registry)",
        ["scripts/nppes_lookup.py", "--npi", npi], root=root, as_of=as_of, runner=runner))
    zip5 = _safe(_target_postal_code, root, npi, years)
    if zip5:
        sections.append(_cap_body(_tool_section(
            "nppes_address_sweep", f"NPPES address-sweep co-locators (ZIP {zip5}, top 10)",
            ["scripts/nppes_lookup.py", "--postal-code", zip5, "--limit", "10"],
            root=root, as_of=as_of, runner=runner)))
    else:
        sections.append(Section(
            "nppes_address_sweep", "NPPES address-sweep co-locators", "unavailable",
            "python scripts/nppes_lookup.py --postal-code <zip> --limit 10",
            "target postal code not resolvable from any gated Part B panel year."))
    sections.append(_tool_section(
        "pecos_target", "PECOS enrollment — target",
        ["scripts/pecos_lookup.py", "--npi", npi], root=root, as_of=as_of, runner=runner))
    sections.append(_cap_body(_tool_section(
        "pecos_reassignment_roster", "PECOS reassignment roster + co-members",
        ["scripts/pecos_lookup.py", "--reassignments", npi], root=root, as_of=as_of, runner=runner)))
    for i, c_npi in enumerate(cob_npis[:MAX_PECOS_COBILLERS], 1):
        sections.append(_tool_section(
            f"pecos_cobiller_{i}", f"PECOS enrollment — co-biller {c_npi} "
            "(shared-TIN / reassignment-org screen)",
            ["scripts/pecos_lookup.py", "--npi", c_npi], root=root, as_of=as_of, runner=runner))
    sections.append(_safe(_leie_section, root, npi, as_of))
    sections.append(_tool_section(
        "revocations", "CMS revocation extract (§424.535)",
        ["scripts/revoked_provider_lookup.py", "--npi", npi], root=root, as_of=as_of, runner=runner))
    sections.append(_tool_section(
        "ca_exclusion", "CA Medi-Cal S&I exclusion + FFS enrollment",
        ["scripts/ca_exclusion_lookup.py", "--npi", npi], root=root, as_of=as_of, runner=runner))
    sections.append(_tool_section(
        "trajectory", "Cross-program trajectory (Part B + D + DMEPOS)",
        ["scripts/provider_trajectory.py", "--npi", npi], root=root, as_of=as_of, runner=runner))
    sections.append(_tool_section(
        "medicaid_compare", "Medicaid cross-program audit (Step 6.5)",
        ["scripts/medicaid_lookup.py", "--npi", npi, "--compare-medicare"],
        root=root, as_of=as_of, runner=runner, gateable=True))
    # ── P5 all-payer sections: the loop's cross-provider/entity sight at case
    # start. These read the T-MSIS/entity frames, which are now POINT-IN-TIME
    # GATEABLE (fraudlib/medicaid_cutoff.py): under a calibration cutoff they
    # RUN gated (reads restricted to before the cutoff) rather than redacting,
    # so a Medicaid-native org target (no Part-B presence) still gets its
    # billing surface at Step-0. The Part-B panel sections above degrade to
    # unavailable and the case leads from here.
    sections.append(_cap_body(_tool_section(
        "entity_graph", "Entity/provider graph traversal (P5)",
        ["scripts/entity_lookup.py", "npi", npi], root=root, as_of=as_of,
        runner=runner, gateable=True)))
    sections.append(_cap_body(_tool_section(
        "medicaid_org_preflight",
        "Medicaid org pre-flight — intensity/trajectory/ring numbers (P5)",
        ["scripts/medicaid_preflight.py", "--npi", npi],
        root=root, as_of=as_of, runner=runner, gateable=True)))
    if years:
        yrs_arg = ",".join(str(y) for y in years[-3:])
        if as_of:
            # Year-args make this tool gateable, but it may read satellite
            # files we can't audit here — keep calibration packs panel-only
            # and let the jailed agent run it itself with gated years.
            sections.append(Section(
                "hcpcs_breakdown", f"Predicate-evaluation breakdown ({yrs_arg})",
                "redacted_calibration",
                f"python scripts/dump_hcpcs_breakdown.py --npi {npi} --years {yrs_arg}",
                _LIVE_ONLY_NOTE + " (you may run this one yourself inside the "
                "jail — the explicit year arguments keep it cutoff-safe)"))
        else:
            sections.append(_tool_section(
                "hcpcs_breakdown", f"Predicate-evaluation breakdown ({yrs_arg})",
                ["scripts/dump_hcpcs_breakdown.py", "--npi", npi, "--years", yrs_arg],
                root=root, as_of=None, runner=runner))

    return {
        "npi": npi,
        "as_of": as_of,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "panel_years_used": years,
        "cobiller_npis": cob_npis,
        "sections": [asdict(s) for s in sections],
    }


def render_pack_markdown(pack: dict) -> str:
    hdr = [
        "## Step-0 deterministic evidence pack",
        "",
        f"Target NPI **{pack['npi']}** · built {pack['built_at']} · "
        f"panel years used: {pack['panel_years_used']}"
        + (f" · CALIBRATION as_of={pack['as_of']} (live sections redacted)" if pack['as_of'] else ""),
        "",
        "Every `[ok]` section below is deterministic, pre-verified data — cite it "
        "via the section's `source:` command (reproducible per citation discipline) "
        "and do NOT spend tool turns re-running these lookups. `[unavailable]` "
        "sections are yours to fill; `[redacted_calibration]` sections are "
        "intentionally withheld by the sandbox.",
        "",
    ]
    out = "\n".join(hdr) + "\n" + "\n".join(
        Section(**s).to_markdown() for s in pack["sections"])
    if len(out) > PACK_CHAR_CAP:
        out = out[:PACK_CHAR_CAP] + "\n…[pack truncated]"
    return out


def ensure_evidence_pack(*, npi: str, case_dir: Path, root: Path,
                         as_of: str | None = None, runner=run_cmd,
                         logger=None) -> str:
    """Idempotent build-or-load (resume support). Writes
    ``<case_dir>/evidence_pack.{json,md}`` and returns the markdown ('' on
    total failure — the prompts degrade gracefully)."""
    case_dir = Path(case_dir)
    md_path, json_path = case_dir / "evidence_pack.md", case_dir / "evidence_pack.json"
    if md_path.exists():
        return md_path.read_text()
    try:
        pack = build_evidence_pack(npi, root=root, as_of=as_of, runner=runner)
        md = render_pack_markdown(pack)
        case_dir.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(pack, indent=2, default=str))
        md_path.write_text(md)
        return md
    except Exception as e:  # pragma: no cover — never block the case on the pack
        if logger:
            logger.warning(f"evidence pack build failed (continuing without): {e}")
        return ""
