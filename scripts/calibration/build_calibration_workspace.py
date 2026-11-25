"""Build a SANITIZED, answer-key-free calibration workspace for one NPI.

Why: calibrating the investigator loop on a true-positive NPI is only honest if the agent
cannot read the answer. We learned (docs/INVESTIGATOR_SANDBOX_EVAL.md, 2026-05-30) that the
web sandbox alone is not enough — the repo itself is an answer key (an agent will `grep`
`gold_set.jsonl`). This builder produces a self-contained workspace that contains ONLY:

    tools/sandbox_browser.py        point-in-time web (the only web access in calibration)
    data/provider_context.json     npi, specialty, state, cutoff  (NO name, NO outcome/scheme)
    data/billing_pre_cutoff.json    the NPI's CMS billing for data_year < cutoff, identity stripped
    TASK.md, README.md, MANIFEST.json

NO gold_set / proposals / audits / defendant name reach the workspace, so an investigator run
with cwd = the workspace (and Grep/Glob denied) cannot reach the answer.

Generalize to any future calibration case:
    python scripts/build_calibration_workspace.py --npi <NPI> --as-of <YYYY-MM-DD>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fraudlib import r2 as _r2  # noqa: E402 — path inserted above

REPO = Path(__file__).resolve().parents[2]
PANELS = REPO / "data/panels"
GOLD = REPO / "data/extracted/gold_set.jsonl"
TOOL = REPO / "scripts/sandbox_browser.py"
SCRIPTS_DIR = REPO / "scripts"

# --with-panels: standalone, answer-key-free analysis tools cloned into the
# workspace so the agent runs the SAME tools as a production run (instead of
# reading only pre-computed JSON). Verified standalone (stdlib + pandas/duckdb,
# no repo-package imports), so they run with cwd=workspace reading
# workspace-local panels. sandbox_browser.py is copied separately (see TOOL).
# DELIBERATELY EXCLUDED (they would surface the post-cutoff answer): doj_pr_lookup,
# courtlistener_search, ca_exclusion_lookup, pecos_lookup. The agent never calls
# nppes_lookup.py directly (it calls sandbox_nppes.py) — but sandbox_nppes EXECS
# nppes_lookup.py as a subprocess, so the file must be present in the export or
# sandbox_nppes fails with "can't open". It is shipped but NOT agent-allowlisted.
SAFE_TOOLS = (
    "dump_hcpcs_breakdown.py",   # per-(NPI,HCPCS) line-item breakdown (subject)
    "provider_trajectory.py",    # cross-program Part B/D/DMEPOS trajectory by year
    "address_sweep.py",          # co-located billers at the subject's practice address
    "hcpcs_lookup.py",           # HCPCS code descriptions / predicate checks
    "sandbox_nppes.py",          # date-gated NPPES identity wrapper (agent calls this)
    "nppes_lookup.py",           # sandbox_nppes runtime dep (live NPPES; result date-gated)
)
# Per-year panel families the analysis tools read. Only years < cutoff are cloned.
PANEL_FAMILIES = ("cms_partb_svc", "cms_partb", "cms_partd", "cms_dmepos")


def _panel_path(fname: str, src_dir: "Path | None" = None) -> Path:
    """Return a local path to a panel parquet, fetching from R2 if absent locally.

    Uses ``src_dir`` when given (the --with-panels clone loop already resolves its own
    source dir), otherwise falls back to the module-level ``PANELS`` global (which
    main() patches to the resolved panels dir before any call reaches here).
    Raises only when BOTH local and R2 are unavailable (fetch_local will raise).
    """
    d = src_dir if src_dir is not None else PANELS
    local = d / fname
    # Builder stages LOCAL-FIRST (r2_first=False): clone the local panel when present
    # (fast, identical pre-cutoff data), fetch from R2 only on a clean machine. The
    # production tools default to R2-first; the builder's bulk staging does not.
    return _r2.fetch_local(local, f"{_r2.R2_PANELS}/{fname}", r2_first=False)


def _resolve_panels_dir() -> Path:
    """Locate data/panels for the build.

    Git worktrees don't carry the (gitignored, multi-GB) parquet panels — they
    live only in the main checkout. When this script runs from a worktree
    (path contains ``/.claude/worktrees/``), fall back to the main checkout's
    panels so the export can still be built from a worktree.
    """
    if PANELS.exists() and any(PANELS.glob("cms_partb_svc_*.parquet")):
        return PANELS
    s = str(REPO)
    marker = "/.claude/worktrees/"
    if marker in s:
        main_panels = Path(s[: s.index(marker)]) / "data" / "panels"
        if main_panels.exists() and any(main_panels.glob("cms_partb_svc_*.parquet")):
            return main_panels
    return PANELS  # may be empty; the caller errors with a clear message


def _clone_file(src: Path, dst: Path) -> str:
    """Materialize ``src`` at ``dst`` as the agent's ONLY copy.

    Prefers APFS clonefile (``cp -c``): instant, zero physical bytes, and an
    INDEPENDENT copy — so the agent (which only reads) can never affect the
    source panel even via a same-inode hardlink. Falls back to hardlink
    (same-filesystem, shares inode) then a full byte copy.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        r = subprocess.run(["cp", "-c", str(src), str(dst)],
                           capture_output=True, text=True)
        if r.returncode == 0 and dst.exists():
            return "clone"
    except Exception:
        pass
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError:
        pass
    shutil.copy2(src, dst)
    return "copy"

PROGRAMS = {"partb": "cms_partb_%d.parquet", "partd": "cms_partd_%d.parquet",
            "dmepos": "cms_dmepos_%d.parquet",
            "partb_svc": "cms_partb_svc_%d.parquet"}
# Columns whose names contain any of these tokens are stripped (identity / re-identification).
ID_TOKENS = ("name", "first", "last", "_mi", "crdntl", "credential", "_st1", "_st2",
             "street", "addr", "city", "zip", "phone")
MIN_YEAR = 2013

# Part B columns used to build interpretable specialty-peer benchmarks.
BENCH_COLS = ["Rndrng_NPI", "Rndrng_Prvdr_Type", "Tot_Benes", "Tot_Srvcs", "Tot_HCPCS_Cds",
              "Tot_Mdcr_Pymt_Amt", "Tot_Sbmtd_Chrg", "Tot_Mdcr_Alowd_Amt",
              "Bene_Dual_Cnt", "Bene_Age_GT_84_Cnt", "Bene_Avg_Risk_Scre"]
PCT_POINTS = [10, 25, 50, 75, 90, 95, 99]

# Diagnostic HCPCS codes for the per-code peer-stat benchmark — chosen because each
# discriminates a known fraud archetype:
#   E&M established (99211-99215) + new (99202-99205): complexity-upcoding signature
#   AWV (G0438/G0439), TCM (99495/99496), CCM (99490), HHA cert (G0180),
#     behavioral counseling (G0447, 99406), Welcome-to-Medicare (G0402):
#     NP-typical chronic-care workflow — when these run hot under a physician NPI
#     with workload-anomaly aggregate stats, that's the incident-to signature
#   Common in-office labs (82947, 81003, 83036, 85025, 80053): high-intensity
#     workup pattern signature (per-bene volume vs peers)
DIAGNOSTIC_HCPCS = [
    "99211", "99212", "99213", "99214", "99215",     # E&M established
    "99202", "99203", "99204", "99205",                # E&M new
    "G0438", "G0439",                                   # AWV initial/subsequent
    "99495", "99496", "99490",                          # TCM moderate/high, CCM
    "G0180",                                            # Home Health certification
    "G0447", "99406",                                   # Behavioral counseling
    "G0402",                                            # Welcome to Medicare
    "82947", "81003", "83036", "85025", "80053",      # Labs
]
EM_EST = ["99211", "99212", "99213", "99214", "99215"]
EM_NEW = ["99202", "99203", "99204", "99205"]


def _npi_col(names):
    for c in names:
        if c.lower() == "npi":
            return c
    for c in names:
        if "npi" in c.lower():
            return c
    return None


def _is_identity(col: str) -> bool:
    c = col.lower()
    return any(t in c for t in ID_TOKENS)


def _defendant_name(npi: str):
    """Trusted setup-only read of gold_set to get the name FOR THE SANITIZATION AUDIT. Never written."""
    if not GOLD.exists():
        return None
    with GOLD.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if str(d.get("npi") or d.get("v4_npi") or "") == npi:
                return d.get("defendant_name")
    return None


def _extract(npi: str, pre_years: list[int]):
    """Return (billing dict {program: [rows]}, specialty, state) — identity columns stripped."""
    billing: dict[str, list] = {}
    specialty = state = None
    for prog, patt in PROGRAMS.items():
        frames = []
        for y in pre_years:
            try:
                p = _panel_path(patt % y)
            except Exception:
                continue
            if not p.exists():
                continue
            names = pq.read_schema(p).names
            col = _npi_col(names)
            if not col:
                continue
            # dtype-aware predicate pushdown (NPI is int64 in the rich panels)
            field = pq.read_schema(p).field(col)
            val = int(npi) if pa.types.is_integer(field.type) else npi
            df = pd.read_parquet(p, filters=[(col, "==", val)])
            if df.empty:
                continue
            if prog == "partb":
                specialty = specialty or df.iloc[0].get("Rndrng_Prvdr_Type")
                state = state or df.iloc[0].get("Rndrng_Prvdr_State_Abrvtn")
            keep = [c for c in df.columns if not _is_identity(c) and c != "data_year"]
            clean = df[keep].copy()
            clean.insert(0, "data_year", y)
            frames.append(clean)
        if frames:
            billing[prog] = json.loads(pd.concat(frames, ignore_index=True).to_json(orient="records"))
    return billing, specialty, state


def _derive_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Interpretable Part B metrics (raw + per-bene/per-service rates), defensive to missing cols."""
    out = pd.DataFrame(index=df.index)
    g = lambda c: df[c] if c in df.columns else None
    benes, srvcs = g("Tot_Benes"), g("Tot_Srvcs")
    b = benes.replace(0, pd.NA) if benes is not None else None
    s = srvcs.replace(0, pd.NA) if srvcs is not None else None
    if benes is not None: out["tot_benes"] = benes
    if srvcs is not None: out["tot_srvcs"] = srvcs
    if g("Tot_HCPCS_Cds") is not None: out["tot_hcpcs_cds"] = g("Tot_HCPCS_Cds")
    if g("Tot_Mdcr_Pymt_Amt") is not None: out["tot_mdcr_pymt"] = g("Tot_Mdcr_Pymt_Amt")
    if g("Bene_Avg_Risk_Scre") is not None: out["bene_avg_risk_scre"] = g("Bene_Avg_Risk_Scre")
    if srvcs is not None and b is not None: out["svcs_per_bene"] = srvcs / b
    if g("Tot_Mdcr_Pymt_Amt") is not None and b is not None: out["pymt_per_bene"] = g("Tot_Mdcr_Pymt_Amt") / b
    if g("Tot_Mdcr_Pymt_Amt") is not None and s is not None: out["pymt_per_srvc"] = g("Tot_Mdcr_Pymt_Amt") / s
    if g("Bene_Dual_Cnt") is not None and b is not None: out["pct_dual"] = g("Bene_Dual_Cnt") / b
    if g("Bene_Age_GT_84_Cnt") is not None and b is not None: out["pct_age_85plus"] = g("Bene_Age_GT_84_Cnt") / b
    if g("Tot_Sbmtd_Chrg") is not None and g("Tot_Mdcr_Alowd_Amt") is not None:
        out["sbmtd_to_alowd"] = g("Tot_Sbmtd_Chrg") / g("Tot_Mdcr_Alowd_Amt").replace(0, pd.NA)
    return out


def _benchmarks(npi: str, specialty, pre_years: list[int]) -> dict:
    """Per-year specialty-peer distributions (Part B) + the subject's percentile rank — identity-free."""
    if not specialty:
        return {}
    by_year = {}
    for y in pre_years:
        try:
            p = _panel_path(PROGRAMS["partb"] % y)
        except Exception:
            continue
        if not p.exists():
            continue
        names = pq.read_schema(p).names
        cols = [c for c in BENCH_COLS if c in names]
        if "Rndrng_Prvdr_Type" not in cols:
            continue
        peers = pd.read_parquet(p, columns=cols)
        peers = peers[peers["Rndrng_Prvdr_Type"] == specialty]
        if peers.empty:
            continue
        col = _npi_col(peers.columns)  # NPI column within the narrow `peers` view, not the full schema
        m = _derive_metrics(peers)
        tgt_mask = (peers[col].astype("Int64").astype(str) == npi).values
        tgt = m[tgt_mask]
        metrics = {}
        for c in m.columns:
            ser = pd.to_numeric(m[c], errors="coerce").dropna()
            if ser.empty:
                continue
            entry = {f"p{q}": round(float(ser.quantile(q / 100)), 4) for q in PCT_POINTS}
            entry["mean"] = round(float(ser.mean()), 4)
            if len(tgt) and pd.notna(tgt.iloc[0][c]):
                tv = float(tgt.iloc[0][c])
                entry["subject_value"] = round(tv, 4)
                entry["subject_pctile"] = round(float((ser <= tv).mean() * 100), 1)
            metrics[c] = entry
        by_year[str(y)] = {"n_peers": int(len(peers)), "metrics": metrics}
    return {"specialty": specialty,
            "basis": "national Part B providers with Rndrng_Prvdr_Type == specialty, per year; "
                     "distribution stats only (identity-free); subject_pctile = % of peers <= subject",
            "by_year": by_year}


def _hcpcs_benchmarks(npi: str, specialty, pre_years: list[int]) -> dict:
    """Per-HCPCS-code specialty-peer distributions + E&M code-mix peer stats.

    Two outputs per year:
      - `per_code`: for each diagnostic HCPCS code, peer distribution of services-per-bene
        (p50/p90/p99) + subject's value + subject's percentile rank
      - `em_code_mix`: established-patient (99211-99215) and new-patient (99202-99205) code
        distributions, with subject's level-5 share + L4+L5 share vs peer distribution.

    Identity-free: distribution stats + the subject's own metric only. The agent can use
    these to find anomalies it could not otherwise compute (the SVC panel would require
    full-panel scanning).
    """
    if not specialty:
        return {}
    by_year = {}
    for y in pre_years:
        try:
            p_svc = _panel_path(PROGRAMS["partb_svc"] % y)
            p_agg = _panel_path(PROGRAMS["partb"] % y)
        except Exception:
            continue
        if not p_svc.exists() or not p_agg.exists():
            continue
        # Read SVC panel filtered to specialty + diagnostic codes (small + fast)
        svc = pd.read_parquet(p_svc, filters=[
            ("Rndrng_Prvdr_Type", "==", specialty),
            ("HCPCS_Cd", "in", DIAGNOSTIC_HCPCS),
        ], columns=["Rndrng_NPI", "HCPCS_Cd", "Tot_Srvcs"])
        if svc.empty:
            continue
        # Per-NPI total-benes from aggregate panel
        agg = pd.read_parquet(p_agg, filters=[
            ("Rndrng_Prvdr_Type", "==", specialty),
            ("Tot_Benes", ">=", 30),
        ], columns=["Rndrng_NPI", "Tot_Benes"])
        # Pivot SVC: NPI × HCPCS_Cd -> sum(Tot_Srvcs)
        pv = svc.pivot_table(index="Rndrng_NPI", columns="HCPCS_Cd",
                            values="Tot_Srvcs", aggfunc="sum", fill_value=0)
        # Join with bene count
        pv = pv.merge(agg, left_index=True, right_on="Rndrng_NPI", how="inner").set_index("Rndrng_NPI")
        # Subject lookup value
        field = pq.read_schema(p_svc).field("Rndrng_NPI")
        subj_val = int(npi) if pa.types.is_integer(field.type) else npi

        # Per-code peer stats (services-per-bene)
        per_code = {}
        for code in DIAGNOSTIC_HCPCS:
            if code not in pv.columns:
                continue
            per_bene = pv[code] / pv["Tot_Benes"]
            if subj_val in per_bene.index:
                sv = float(per_bene.loc[subj_val])
                pctile = float((per_bene <= sv).mean() * 100)
            else:
                sv, pctile = 0.0, 0.0
            per_code[code] = {
                "subject_per_bene": round(sv, 4),
                "peer_p50_per_bene": round(float(per_bene.median()), 4),
                "peer_p90_per_bene": round(float(per_bene.quantile(0.90)), 4),
                "peer_p99_per_bene": round(float(per_bene.quantile(0.99)), 4),
                "subject_pctile": round(pctile, 1),
            }

        # E&M code-mix peer stats — established-patient
        em_mix = {}
        est_cols = [c for c in EM_EST if c in pv.columns]
        if est_cols and "99215" in pv.columns:
            tot_est = pv[est_cols].sum(axis=1)
            with_vol = tot_est >= 50  # filter low-volume noise providers
            l5_share = (100 * pv["99215"] / tot_est.replace(0, pd.NA))[with_vol]
            l45_share = (100 * (pv.get("99214", 0) + pv["99215"]) / tot_est.replace(0, pd.NA))[with_vol]
            if subj_val in pv.index and tot_est.loc[subj_val] > 0:
                s_l5 = float(100 * pv.loc[subj_val].get("99215", 0) / tot_est.loc[subj_val])
                s_l45 = float(100 * (pv.loc[subj_val].get("99214", 0) + pv.loc[subj_val].get("99215", 0)) / tot_est.loc[subj_val])
                em_mix["established_patient"] = {
                    "subject_total_em_visits": int(tot_est.loc[subj_val]),
                    "subject_99215_share_pct": round(s_l5, 1),
                    "peer_99215_share_p50": round(float(l5_share.median()), 1),
                    "peer_99215_share_p90": round(float(l5_share.quantile(0.90)), 1),
                    "peer_99215_share_p99": round(float(l5_share.quantile(0.99)), 1),
                    "subject_99215_share_pctile": round(float((l5_share <= s_l5).mean() * 100), 1),
                    "subject_L4plus5_share_pct": round(s_l45, 1),
                    "peer_L4plus5_share_p50": round(float(l45_share.median()), 1),
                    "peer_L4plus5_share_p90": round(float(l45_share.quantile(0.90)), 1),
                    "subject_L4plus5_share_pctile": round(float((l45_share <= s_l45).mean() * 100), 1),
                    "n_peers_with_em_volume": int(with_vol.sum()),
                }

        # E&M code-mix peer stats — new-patient
        new_cols = [c for c in EM_NEW if c in pv.columns]
        if new_cols and "99205" in pv.columns:
            tot_new = pv[new_cols].sum(axis=1)
            with_vol_new = tot_new >= 5
            l5n_share = (100 * pv["99205"] / tot_new.replace(0, pd.NA))[with_vol_new]
            if subj_val in pv.index and tot_new.loc[subj_val] > 0:
                s_99205 = float(100 * pv.loc[subj_val].get("99205", 0) / tot_new.loc[subj_val])
                em_mix["new_patient"] = {
                    "subject_total_new_visits": int(tot_new.loc[subj_val]),
                    "subject_99205_share_pct": round(s_99205, 1),
                    "peer_99205_share_p50": round(float(l5n_share.median()), 1),
                    "peer_99205_share_p90": round(float(l5n_share.quantile(0.90)), 1),
                    "subject_99205_share_pctile": round(float((l5n_share <= s_99205).mean() * 100), 1),
                    "n_peers_with_new_visits": int(with_vol_new.sum()),
                }

        by_year[str(y)] = {
            "n_peers_total": int(len(pv)),
            "per_code": per_code,
            "em_code_mix": em_mix,
        }
    return {
        "specialty": specialty,
        "basis": ("national Part B SVC line-item, providers with Rndrng_Prvdr_Type == specialty + "
                  ">=30 benes/yr, per year. per_code: peer p50/p90/p99 of services-per-bene + "
                  "subject value + subject's percentile rank. em_code_mix: peer distribution of "
                  "99215-share and L4+L5-share of established-patient visits, and 99205-share of "
                  "new-patient visits (low-volume providers filtered: est >= 50 visits, new >= 5)."),
        "by_year": by_year,
    }


TASK_TMPL = """# Calibration investigation task

You are a Medicare fraud-risk investigator. Assess ONE provider **as of {cutoff}** — a point-in-time
review, so you must be blind to anything dated on/after {cutoff}.

**Subject:** NPI **{npi}** — {specialty}, {state}. The provider's name is **provided in your agent prompt and is
public** — you may use it and investigate freely; a real fraud investigation knows who it is examining. What is
withheld is the **case disposition**: whether this provider was ever investigated, charged, settled, excluded, or
convicted. Treat any web result dated on/after {cutoff} as nonexistent (the sandbox enforces this) and do not
presume an outcome. Your task is to determine — from the pre-cutoff billing and legitimately-fetchable pre-cutoff
record — whether the pattern indicates fraud, not to recall a known case.

**Inputs in this workspace**
- `data/billing_pre_cutoff.json` — the provider's CMS Medicare billing for years < {cutoff} (identity columns stripped). Contains four programs:
  - `partb` — aggregate Part B per provider-year (Tot_Benes / Tot_Srvcs / Tot_Mdcr_Pymt_Amt / Bene_Dual_Cnt / Bene_Avg_Risk_Scre / etc.)
  - `partb_svc` — **HCPCS-line-item** Part B (one row per HCPCS code per year): HCPCS_Cd + HCPCS_Desc + Tot_Benes + Tot_Srvcs + Avg_Sbmtd_Chrg. Use this to inspect code mix (E&M complexity distribution, AWV/TCM/CCM/home-health-cert workflow, lab intensity).
  - `partd` — aggregate Part D prescriber data per year.
  - `dmepos` — aggregate DMEPOS supplier count per year.
- `data/provider_context.json` — npi / specialty / state / cutoff.
- `data/specialty_benchmarks.json` — per-year **specialty-peer** distributions (p10-p99 + mean) for aggregate billing metrics with subject percentile rank.
- `data/hcpcs_benchmarks.json` — per-year, per-HCPCS-code peer distributions for diagnostic codes (E&M, AWV, TCM, CCM, home-health-cert, behavioral counseling, common labs) PLUS E&M code-mix peer stats (peer distribution of 99215-share, L4+L5-share, 99205-share among new patients). Use these to flag anomalies in code-mix or in per-code intensity vs peers.
- `tools/sandbox_browser.py` — the ONLY web access:
  `python3 tools/sandbox_browser.py {{search,fetch,check}} --as-of {cutoff} ...`
  Returns only content dated before {cutoff}; a `BLOCKED` result means it did not exist yet.

**Rules**
- Use no other network or tool; do NOT pass `--allow-live-if-dated`; do NOT try to re-identify the provider.
- Ground your assessment in the pre-cutoff billing patterns (+ any legitimately fetchable pre-cutoff web).

**Output format:** defined by the agent prompt that launched you — if it supplies a JSON
schema, return ONLY that JSON. Only when the launch prompt specifies no format (bare
standalone smoke run) fall back to: a short rationale, then a final line
`RISK: <HIGH|MEDIUM|LOW>`, the most likely fraud scheme type(s) if elevated, and a
one-sentence rationale grounded in pre-cutoff evidence.
"""

README_TMPL = """# Sanitized calibration workspace — NPI {npi}, as_of {cutoff}

Self-contained and **answer-key-free**: no `gold_set.jsonl` / `proposals/` / `audits/` and no
defendant name. The launch command **restricts Read to this workspace only** — `gold_set.jsonl`,
the `docs/calibration/` answer-key docs, and the git history are unreachable, even by absolute path:

```bash
WS_ABS="$(cd "$(dirname "$0")" && pwd)"
cd "$WS_ABS"
claude -p "$(cat TASK.md)" \\
  --permission-mode default \\
  --allowed-tools "Bash(python3 tools/sandbox_browser.py:*),Read($WS_ABS/**)" \\
  --disallowed-tools "WebSearch,WebFetch,Grep,Glob"
```

Stacked guards (verified end-to-end 2026-06-03):
- `Read($WS_ABS/**)` — **allowlist anchored to the workspace absolute path**. Tested: blocks
  `Read("../../data/extracted/gold_set.jsonl")` AND
  `Read("/abs/path/.../data/extracted/gold_set.jsonl")` with permission-denied error.
- Only `sandbox_browser` is callable via `Bash` (no general `curl` / `find` / `cat`).
- `WebSearch` / `WebFetch` denied — only pre-cutoff Wayback via `sandbox_browser`.
- `Grep` / `Glob` denied — can't enumerate the filesystem to discover paths.

The `sandbox_browser.py` shipped here intentionally does NOT include its `browser_fetch.py` peer
module, so the `fetch`/`check`/`search` subcommands fail-safe (ModuleNotFoundError) and the agent
cannot inadvertently fetch a Wayback page that names the defendant. The `cutoff-for-npi` subcommand
is also non-functional because no gold_set exists in this workspace. See
`docs/INVESTIGATOR_SANDBOX_EVAL.md` (§ Agent-level validation) for the threat model.
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--npi", required=True)
    ap.add_argument("--as-of", required=True, help="YYYY-MM-DD cutoff (discovery/charge date, not the disposition)")
    # Default to OUTSIDE the repo. The repo itself is the answer key (gold_set.jsonl,
    # docs/calibration/, git history, backups), so a workspace inside the repo is one
    # `Read("../../data/extracted/gold_set.jsonl")` away from total leakage. The launch
    # command also anchors `Read(...)` to the workspace abs path; that's the primary
    # defense. Out-of-repo location is defense in depth.
    ap.add_argument("--out-root", default=str(Path.home() / ".calibration_workspaces"))
    ap.add_argument("--with-panels", action="store_true",
                    help="Also clone the pre-cutoff CMS parquet panels + copy the standalone "
                         "analysis tools (dump_hcpcs_breakdown/provider_trajectory/address_sweep/"
                         "hcpcs_lookup/sandbox_nppes) into the workspace, so the agent runs the "
                         "REAL tools (not just pre-computed JSON). Fail-closed: only years < cutoff "
                         "are cloned; no answer-key files; no .git.")
    ap.add_argument("--panels-src", default=None,
                    help="Override the source data/panels dir (default: auto-resolve, falling "
                         "back to the main checkout when run from a git worktree).")
    ap.add_argument("--with-claims", default=None, metavar="PARQUET",
                    help="Path to a pre-built synth_claims.parquet "
                         "(scripts/calibration/build_synth_claims.py). Copies it into data/ and "
                         "ships claims_lookup.py, making this the Arm-B (claim-level) export. "
                         "Requires --with-panels. See docs/calibration/"
                         "ASPEN_CLAIMS_LEVEL_AB_EXPERIMENT.md.")
    ap.add_argument("--with-claims-config", default=None, metavar="CONFIG_JSON",
                    help="Path to a synth-claims JSON config in scripts/calibration/synth_configs/. "
                         "Invokes build_synth_claims.py --config <CONFIG_JSON> in a subprocess to "
                         "auto-generate synth_claims.parquet for THIS case, then copies it in (same "
                         "as --with-claims with a pre-built parquet). Mutually exclusive with "
                         "--with-claims. Requires --with-panels. The toggle: pass it for Arm B, "
                         "omit for Arm A.")
    args = ap.parse_args(argv)

    # Point ALL panel reads (billing extract + benchmarks + clone) at the resolved
    # panels dir. Git worktrees lack the gitignored panels, so this falls back to the
    # main checkout — without it, a build from a worktree would silently produce empty
    # billing/benchmark JSON.
    global PANELS
    PANELS = Path(args.panels_src).resolve() if args.panels_src else _resolve_panels_dir()

    npi = args.npi.strip()
    cutoff = args.as_of.strip()
    cutoff_year = int(cutoff[:4])
    pre_years = list(range(MIN_YEAR, cutoff_year))
    if not pre_years:
        print(f"ERROR: cutoff {cutoff} leaves no pre-cutoff panel years (panels start {MIN_YEAR}).", file=sys.stderr)
        return 1

    billing, specialty, state = _extract(npi, pre_years)
    ws = Path(args.out_root) / f"npi_{npi}_asof_{cutoff}"
    (ws / "tools").mkdir(parents=True, exist_ok=True)
    (ws / "data").mkdir(parents=True, exist_ok=True)

    ctx = {"npi": npi, "specialty": specialty, "state": state, "as_of_cutoff": cutoff,
           "pre_cutoff_years_available": pre_years,
           "programs_with_billing": sorted(billing.keys()),
           "note": "Identity columns (name/address/city/zip/credentials) stripped; case-disposition fields intentionally excluded."}
    (ws / "data" / "provider_context.json").write_text(json.dumps(ctx, indent=2))
    (ws / "data" / "billing_pre_cutoff.json").write_text(json.dumps(billing, indent=2))
    (ws / "data" / "specialty_benchmarks.json").write_text(json.dumps(_benchmarks(npi, specialty, pre_years), indent=2))
    (ws / "data" / "hcpcs_benchmarks.json").write_text(json.dumps(_hcpcs_benchmarks(npi, specialty, pre_years), indent=2))
    shutil.copy2(TOOL, ws / "tools" / "sandbox_browser.py")
    fmt = dict(npi=npi, cutoff=cutoff, specialty=specialty or "unknown", state=state or "unknown")
    (ws / "TASK.md").write_text(TASK_TMPL.format(**fmt))
    (ws / "README.md").write_text(README_TMPL.format(**fmt))

    # ---- optional: clone pre-cutoff panels + analysis tools (--with-panels) ----
    panel_report: dict = {}
    if args.with_panels:
        panels_src = Path(args.panels_src).resolve() if args.panels_src else _resolve_panels_dir()
        local_panels_present = panels_src.exists() and any(panels_src.glob("cms_partb_svc_*.parquet"))
        if not local_panels_present:
            # Not an immediate error — R2 fallback via _panel_path may succeed per file.
            # Only hard-fail if panels_src was explicitly set and doesn't exist at all.
            if args.panels_src and not panels_src.exists():
                print(f"ERROR: --panels-src path does not exist: {panels_src}", file=sys.stderr)
                return 1
            print(f"  NOTE: local panels not found at {panels_src}; will fetch from R2 as needed.",
                  file=sys.stderr)
        panel_dst = ws / "data" / "panels"
        panel_dst.mkdir(parents=True, exist_ok=True)
        methods: dict[str, int] = {}
        for fam in PANEL_FAMILIES:
            for y in pre_years:                       # pre_years = [MIN_YEAR .. cutoff_year-1]
                fname = f"{fam}_{y}.parquet"
                try:
                    src = _panel_path(fname, panels_src)
                except Exception as exc:
                    print(f"  WARN: {fname} unavailable locally and R2 fetch failed ({exc}); skipping.",
                          file=sys.stderr)
                    continue
                if src.exists():
                    m = _clone_file(src, panel_dst / fname)
                    methods[m] = methods.get(m, 0) + 1
        # Stage ground_truth reference tables so jailed tools (hcpcs_lookup.py etc.)
        # can read them without R2 access. Local-first via r2.fetch_local; R2 key is
        # r2:needlepoint-data/ground_truth/v1/<name>. These are code-definition tables (NOT
        # time-series) — no cutoff filter; stage the latest unconditionally.
        GT_TABLES = (
            "hcpcs_codes.parquet",
            "hcpcs_codes_by_year.parquet",
            "hcpcs_level2_full.parquet",
            "partd_drugs.parquet",
        )
        _GT_R2 = "r2:needlepoint-data/ground_truth/v1"
        _GT_LOCAL = REPO / "data" / "ground_truth"
        gt_dst = ws / "data" / "ground_truth"
        gt_dst.mkdir(parents=True, exist_ok=True)
        gt_staged: list[str] = []
        gt_missing: list[str] = []
        for tbl in GT_TABLES:
            try:
                src_gt = _r2.fetch_local(_GT_LOCAL / tbl, f"{_GT_R2}/{tbl}", r2_first=False)
                _clone_file(src_gt, gt_dst / tbl)
                gt_staged.append(tbl)
            except Exception as exc:
                print(f"  WARN: ground_truth/{tbl} unavailable ({exc}); skipping.", file=sys.stderr)
                gt_missing.append(tbl)
        if gt_missing:
            print(f"  WARN: ground_truth tables not staged (will skip): {gt_missing}", file=sys.stderr)
        # provider_trajectory/address_sweep resolve PROC = <script_parent>/data/processed;
        # dump_hcpcs_breakdown reads <cwd>/data/panels. With tools in ws/tools and the agent's
        # cwd = ws, both must land on ws/data/panels — so symlink data/processed -> panels
        # (an in-tree relative symlink; resolves inside the workspace).
        proc_link = ws / "data" / "processed"
        if proc_link.is_symlink() or proc_link.exists():
            proc_link.unlink()
        proc_link.symlink_to("panels")
        # Copy the standalone analysis tools into ws/scripts/ — the agent runs
        # `python3 scripts/<tool>` (matching the investigator prompt's references)
        # with cwd=ws, so dump_hcpcs reads <cwd>/data/panels and provider_trajectory/
        # address_sweep resolve parents[1]=<ws>/data/processed (-> panels). Also ship
        # sandbox_browser + its browser_fetch dependency so Wayback actually FUNCTIONS
        # (the legacy JSON-only workspace deliberately omits browser_fetch to fail-safe;
        # the full-loop calibration needs working pre-cutoff Wayback access).
        (ws / "scripts").mkdir(parents=True, exist_ok=True)
        copied_tools = []
        for t in (*SAFE_TOOLS, "sandbox_browser.py", "browser_fetch.py"):
            s = SCRIPTS_DIR / t
            if s.exists():
                shutil.copy2(s, ws / "scripts" / t)
                copied_tools.append(t)
        panel_report = {
            "panels_src": str(panels_src),
            "panel_files_cloned": sum(methods.values()),
            "clone_methods": methods,
            "tools_copied": copied_tools,
            "data_processed_symlink": "-> panels",
            "ground_truth_staged": gt_staged,
            "ground_truth_missing": gt_missing,
        }

    # ---- optional: claim-level layer (--with-claims OR --with-claims-config) ----
    # Ships a leak-scrubbed synth_claims.parquet + claims_lookup.py. The agent then has the
    # claim-level granularity the public PUFs lack (rendering vs billed-under), activated
    # data-drivenly by calibration.launch_flags_for_agent (tool offered iff the parquet is
    # present). Arm A = same build WITHOUT this; Arm B = with it.
    #
    # Two paths to populate the parquet:
    #   --with-claims <PATH>           — caller supplies a pre-built parquet (legacy / advanced)
    #   --with-claims-config <CONFIG>  — auto-build via build_synth_claims.py --config <CONFIG>
    #                                    (the standard toggle path; works for any case with a
    #                                    config in scripts/calibration/synth_configs/)
    claims_report: dict = {}
    if args.with_claims and args.with_claims_config:
        print("ERROR: --with-claims and --with-claims-config are mutually exclusive — pick one.",
              file=sys.stderr)
        return 1
    if args.with_claims_config and not args.with_panels:
        print("ERROR: --with-claims-config requires --with-panels (the synth-claims builder reads "
              "the cloned panels from the export).", file=sys.stderr)
        return 1
    if args.with_claims and not args.with_panels:
        print("ERROR: --with-claims requires --with-panels (the claim-level experiment "
              "needs the real billing toolset).", file=sys.stderr)
        return 1

    src_pq: Path | None = None
    build_config_report: dict = {}
    if args.with_claims_config:
        cfg_path = Path(args.with_claims_config).resolve()
        if not cfg_path.exists():
            print(f"ERROR: --with-claims-config not found: {cfg_path}", file=sys.stderr)
            return 1
        build_dir = ws / "data" / "_synth_claims_build"
        build_dir.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(SCRIPTS_DIR / "build_synth_claims.py"),
               "--config", str(cfg_path), "--export", str(ws.resolve()),
               "--out", str(build_dir.resolve())]
        print(f"  invoking: {' '.join(cmd)}", file=sys.stderr)
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            print(f"ERROR: build_synth_claims.py exited {rc}", file=sys.stderr)
            return 1
        src_pq = build_dir / "synth_claims.parquet"
        if not src_pq.exists():
            print(f"ERROR: build_synth_claims.py succeeded but didn't write {src_pq}",
                  file=sys.stderr)
            return 1
        build_config_report = {"claims_config": str(cfg_path), "claims_build_dir": str(build_dir)}
    elif args.with_claims:
        src_pq = Path(args.with_claims).resolve()
        if not src_pq.exists():
            print(f"ERROR: --with-claims parquet not found: {src_pq}", file=sys.stderr)
            return 1

    if src_pq is not None:
        (ws / "scripts").mkdir(parents=True, exist_ok=True)
        shutil.copy2(SCRIPTS_DIR / "claims_lookup.py", ws / "scripts" / "claims_lookup.py")
        shutil.copy2(src_pq, ws / "data" / "synth_claims.parquet")
        claims_report = {"claims_parquet_src": str(src_pq), "claims_tool": "claims_lookup.py",
                         **build_config_report}

    # ---- sanitization audit ----
    # Three classes of leaks checked:
    #   (A) Defendant name fragments in any workspace data file (case-insensitive)
    #   (B) Fraud-scheme tokens that imply post-cutoff knowledge of the case
    #   (C) Files named after the defendant (e.g. backups with name in filename)
    #   (D) Workspace LOCATION — if inside the repo, flag because Read deny patterns
    #       in the launch command still depend on the user using the documented flags.
    name = _defendant_name(npi)
    # `forbidden` runs ONLY on data files — TASK.md/README.md legitimately use
    # "fraud" / "scheme" as generic instructions to the investigator agent.
    forbidden = ["indict", "plea", "settlement", "restitution", "convict",
                 "guilty", "whistleblow", "qui tam", "relator", "doj-",
                 "false claim"]
    data_targets = ["data/provider_context.json", "data/billing_pre_cutoff.json",
                    "data/specialty_benchmarks.json", "data/hcpcs_benchmarks.json"]
    name_targets = data_targets + ["TASK.md", "README.md"]
    leaks = []
    # Name-fragment check (all data + templates)
    for rel in name_targets:
        full = ws / rel
        if not full.exists():
            continue
        txt = full.read_text().lower()
        if name:
            for part in str(name).split():
                if len(part) > 2 and part.lower() in txt:
                    leaks.append(f"{rel}: defendant name fragment '{part}'")
    # Forbidden-token check (data files only)
    for rel in data_targets:
        full = ws / rel
        if not full.exists():
            continue
        txt = full.read_text().lower()
        for tok in forbidden:
            if tok in txt:
                leaks.append(f"{rel}: forbidden token '{tok}'")
    # Class C — filenames within the workspace itself
    if name:
        name_parts = [p.lower() for p in str(name).split() if len(p) > 2]
        for p in ws.rglob("*"):
            if p.is_file():
                pname = p.name.lower()
                for part in name_parts:
                    if part in pname:
                        leaks.append(f"filename leak: '{p.relative_to(ws)}' contains name fragment '{part}'")
    answer_files = [p for p in ws.rglob("*") if any(k in p.name.lower() for k in ("gold_set", "proposal", "audit"))]
    if answer_files:
        leaks.append(f"answer files present: {[str(p) for p in answer_files]}")
    # Class E — structural panel audit (--with-panels). The cloned parquet panels
    # legitimately contain PUBLIC provider names (not a leak — the name is in the
    # subject string and the fraud allegation is NOT in CMS billing data), so we do
    # NOT text-scan them. We DO hard-fail on the two things that WOULD leak the answer:
    #   (1) any panel year >= cutoff (post-cutoff billing is itself unavailable-as-of info)
    #   (2) a .git dir (history resurrects gold_set / raw_press_releases / baseline-truth)
    if args.with_panels:
        for p in (ws / "data" / "panels").glob("*.parquet"):
            m = re.search(r"_(\d{4})\.parquet$", p.name)
            if m and int(m.group(1)) >= cutoff_year:
                leaks.append(f"POST-CUTOFF panel present: {p.name} (year >= {cutoff_year})")
        if (ws / ".git").exists():
            leaks.append(".git present in workspace (git history is an answer key)")
    # Class D — workspace inside the repo? warn (not a hard fail; the Read-allowlist
    # in the launch command is the primary defense regardless of location).
    try:
        ws.resolve().relative_to(REPO.resolve())
        in_repo = True
    except ValueError:
        in_repo = False
    if in_repo:
        leaks.append(
            f"workspace is INSIDE the repo at {ws.resolve()} — Read-allowlist in the "
            f"launch command remains the primary defense, but consider moving to "
            f"--out-root outside the repo for defense in depth."
        )

    manifest = {**ctx, "files": sorted(str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file()),
                "sanitized": not leaks,
                "with_panels": bool(args.with_panels),
                "panels": panel_report,
                "with_claims": bool(args.with_claims),
                "claims": claims_report,
                "excluded": ["gold_set.jsonl", "proposals/", "audits/", "defendant_name",
                             "identity_columns", "post_cutoff_panel_years", ".git"]}
    (ws / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))

    print(f"workspace: {ws}")
    print(f"  specialty={specialty}  state={state}  pre_years={pre_years}  programs={sorted(billing.keys())}")
    if args.with_panels:
        print(f"  panels: {panel_report.get('panel_files_cloned', 0)} files "
              f"({panel_report.get('clone_methods', {})}); tools: {panel_report.get('tools_copied', [])}")
    if args.with_claims:
        print(f"  claims layer: {claims_report.get('claims_tool')} + synth_claims.parquet "
              f"(Arm-B export)")
    print(f"  sanitization: {'PASS (no leaks)' if not leaks else 'FAIL -> ' + '; '.join(leaks)}")
    return 0 if not leaks else 2


if __name__ == "__main__":
    sys.exit(main())
