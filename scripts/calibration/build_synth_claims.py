#!/usr/bin/env python3
"""Build synthetic CLAIM-LEVEL data for a calibration case (config-driven, NPI-agnostic).

A calibration case is fully specified by a JSON config in
``scripts/calibration/synth_configs/`` (see that dir's README.md). The engine reads:
  subject NPI, cutoff window, Synthea fabric path, peer-cohort spec (state/specialty/count),
  scheme spec (type + scheme-specific params), and compliant-background spec.

Division of labor (Synthea backbone + CMS overlay):
  * Synthea supplies the realistic BACKBONE — beneficiaries + longitudinal service dates.
    (Native PROCEDURECODE is SNOMED; we do NOT use it.)
  * The real CMS panels (``data/panels/cms_partb_svc_*.parquet`` in the export) supply every
    provider's real per-(year,HCPCS) aggregate. The SUBJECT is roll-up-grounded to its exact
    profile and the scheme is injected on top; EVERY OTHER provider in the extract is a REAL
    panel NPI (resolves in NPPES / address_sweep / dump_hcpcs), disaggregated to claim level
    with rendering == billed-under (legit).
  * The ONLY synthetic element is the rendering ATTRIBUTION (who performed each claim) —
    which is inherently absent from the aggregate PUFs and is the layer the experiment adds.

Grounding tiers:
  - Subject: roll-up-grounded + scheme injection (the fraud).
  - Scheme renderers (own-NPI billing) + co-located providers: REAL panel rows, fully
    disaggregated, NO scheme injection.
  - Real peer cohort (configurable state/specialty): REAL panel NPIs, E&M claims sampled
    (capped) for cross-provider contrast; a fraction run COMPLIANT incident-to (rendered by
    a real NP NPI) so 'rendering != billed-under' is a normal pattern, not a fraud label.

Leak discipline: the mechanism is encoded; the VERDICT is not. No settlement amounts,
no DOJ strings, no real-case identities beyond the public NPIs the agent surfaces itself.

Deterministic per config.seed. Output: ``<out>/synth_claims.parquet`` + a verification report.

Usage:
    python scripts/calibration/build_synth_claims.py \\
        --config scripts/calibration/synth_configs/aspen.json \\
        --export ~/.calibration_workspaces/npi_1900000007_asof_2020-01-01 \\
        --out /tmp/synth_out

History: through 2026-06-06 this script was hardcoded to Aspen. Refactored
2026-06-07 (PR feat/synth-claims-config-driven) to be NPI-agnostic + scheme-pluggable
so the calibration loop can run Arm B mode for any TP, not just Aspen. The original
Aspen constants live verbatim in synth_configs/aspen.json — same seed, same output.
"""
from __future__ import annotations
import argparse, json, random, sys
from pathlib import Path

import duckdb
import pandas as pd

# ---- generic constants (E&M code sets are CMS-defined, not case-specific) ----------------
POS_OFFICE = "11"
NEW_PATIENT = {"99201", "99202", "99203", "99204", "99205"}
ESTABLISHED = {"99211", "99212", "99213", "99214", "99215"}
OFFICE_EM = NEW_PATIENT | ESTABLISHED


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---- config loader -----------------------------------------------------------------------
class Config:
    """Thin wrapper over the synth-claims JSON config — surfaces required keys, computes
    convenience derived fields (cutoff_years range list, np_enrollment flat dict for the
    enrollment-gating helper), and validates the scheme.type is one we know."""

    SUPPORTED_SCHEMES = {"incident_to"}

    def __init__(self, raw: dict, source_path: Path):
        self._raw = raw
        self.source_path = source_path
        for k in ("case_id", "subject_npi", "cutoff_year_start", "cutoff_year_end",
                  "synthea_dir", "seed", "peer_cohort", "scheme"):
            if k not in raw:
                raise ValueError(f"synth config {source_path} missing required key: {k!r}")
        self.case_id = str(raw["case_id"])
        self.subject_npi = str(raw["subject_npi"])
        self.cutoff_years = list(range(int(raw["cutoff_year_start"]),
                                       int(raw["cutoff_year_end"]) + 1))
        self.synthea_dir = Path(raw["synthea_dir"])
        self.seed = int(raw["seed"])
        self.peer_cohort = raw["peer_cohort"]
        self.scheme = raw["scheme"]
        self.compliant_background = raw.get("compliant_background", {})
        if self.scheme.get("type") not in self.SUPPORTED_SCHEMES:
            raise ValueError(
                f"synth config {source_path} scheme.type={self.scheme.get('type')!r} "
                f"unsupported; known: {sorted(self.SUPPORTED_SCHEMES)}"
            )

    @property
    def np_enrollment(self) -> dict[str, str]:
        """Flat {renderer_npi: ISO-date} dict — used by disaggregate_real's enrollment gate.
        For scheme types without renderers (future: phantom_billing), returns {}."""
        if self.scheme["type"] == "incident_to":
            return {n: r["enrollment"] for n, r in self.scheme["renderer_npis"].items()
                    if "enrollment" in r}
        return {}


def load_config(p: Path) -> Config:
    return Config(json.loads(p.read_text()), p)


# ---- data loaders ------------------------------------------------------------------------
def load_real_profile(export: Path, cutoff_years: list[int]) -> list[dict]:
    """Subject's real partb_svc rows (the calibration target) for the pre-cutoff window."""
    bp = json.loads((export / "data" / "billing_pre_cutoff.json").read_text())
    rows = []
    for r in bp.get("partb_svc", []):
        yr = int(_num(r.get("data_year")))
        if yr not in cutoff_years:
            continue
        rows.append({
            "year": yr, "hcpcs": str(r.get("HCPCS_Cd")),
            "desc": str(r.get("HCPCS_Desc", ""))[:60],
            "services": int(_num(r.get("Tot_Srvcs"))), "benes": max(1, int(_num(r.get("Tot_Benes")))),
            "submitted": round(_num(r.get("Avg_Sbmtd_Chrg")), 2),
            "allowed": round(_num(r.get("Avg_Mdcr_Alowd_Amt")), 2),
            "paid": round(_num(r.get("Avg_Mdcr_Pymt_Amt")), 2),
        })
    return rows


def load_synthea_fabric(synthea_csv: Path, cutoff_years: list[int]) -> dict[int, list[dict]]:
    """Realistic (beneficiary, date) tuples per year, sampled from Synthea claims."""
    df = pd.read_csv(synthea_csv / "claims.csv", usecols=["PATIENTID", "SERVICEDATE"],
                     dtype=str, low_memory=False)
    df["year"] = df["SERVICEDATE"].str[:4]
    fabric: dict[int, list[dict]] = {}
    for yr in cutoff_years:
        sub = df[df["year"] == str(yr)]
        fabric[yr] = [{"bene": p, "date": d[:10]} for p, d in zip(sub["PATIENTID"], sub["SERVICEDATE"])]
    return fabric


# ---- real-panel helpers (v3 grounding) ---------------------------------------------------
def _panels(export: Path, cutoff_years: list[int]) -> dict[int, str]:
    out = {}
    for yr in cutoff_years:
        p = export / "data" / "panels" / f"cms_partb_svc_{yr}.parquet"
        if p.exists():
            out[yr] = str(p)
    return out


def panel_rows(con, panels: dict[int, str], npis: list[str]) -> pd.DataFrame:
    """Real partb_svc rows for the given NPIs, per (year, hcpcs)."""
    if not npis:
        return pd.DataFrame(columns=["year", "npi", "hcpcs", "desc", "services", "submitted", "allowed", "paid"])
    npi_in = ",".join(f"'{n}'" for n in npis)
    parts = [f"""SELECT {yr} AS year, Rndrng_NPI AS npi, HCPCS_Cd AS hcpcs,
        any_value(HCPCS_Desc) AS desc, sum(Tot_Srvcs) AS services,
        avg(Avg_Sbmtd_Chrg) AS submitted, avg(Avg_Mdcr_Alowd_Amt) AS allowed, avg(Avg_Mdcr_Pymt_Amt) AS paid
        FROM read_parquet('{p}') WHERE Rndrng_NPI IN ({npi_in}) GROUP BY 1,2,3""" for yr, p in panels.items()]
    return con.execute(" UNION ALL ".join(parts)).fetchdf()


def _distinct_npis(con, panels, where: str, exclude: list[str], rng: random.Random, k: int) -> list[str]:
    excl = ",".join(f"'{x}'" for x in exclude) or "''"
    parts = [f"SELECT DISTINCT Rndrng_NPI FROM read_parquet('{p}') WHERE {where} AND Rndrng_NPI NOT IN ({excl})"
             for p in panels.values()]
    npis = sorted(r[0] for r in con.execute(" UNION ".join(parts)).fetchall())
    rng.shuffle(npis)
    return npis[:k]


def disaggregate_real(rows: pd.DataFrame, fabric, rng, prefix, np_enrollment: dict[str, str],
                      cap_per_year=None, em_only=False, incident_to_for=None,
                      established_rendering_fraction: float = 0.22,
                      cap_per_code=2000) -> pd.DataFrame:
    """Disaggregate real panel rows into claims with rendering == billed-under (legit). For NPIs
    in ``incident_to_for`` (-> a real NP NPI), ``established_rendering_fraction`` of ESTABLISHED
    E&M is rendered by that NP (compliant incident-to, established only). Enrollment-gates any
    NPI in ``np_enrollment``. ``cap_per_year`` proportionally scales a provider's annual volume."""
    incident_to_for = incident_to_for or {}
    if rows.empty:
        return pd.DataFrame()
    df = rows.copy()
    df["hcpcs"] = df["hcpcs"].astype(str)
    if em_only:
        df = df[df["hcpcs"].isin(OFFICE_EM)]
    if df.empty:
        return pd.DataFrame()
    df["year"] = df["year"].astype(int)
    out, cid = [], 0
    for (npi, yr), g in df.groupby(["npi", "year"]):
        npi, yr = str(npi), int(yr)
        if npi in np_enrollment and yr < int(np_enrollment[npi][:4]):
            continue
        pool = fabric.get(yr) or [{"bene": f"SYN-{yr}", "date": f"{yr}-06-15"}]
        code_rows = []
        for _, r in g.iterrows():
            n = min(int(_num(r["services"])), cap_per_code)
            if n > 0:
                code_rows.append((str(r["hcpcs"]), str(r["desc"] or "")[:60], n,
                                  round(_num(r["submitted"]), 2), round(_num(r["allowed"]), 2),
                                  round(_num(r["paid"]), 2)))
        total = sum(cr[2] for cr in code_rows)
        if total <= 0:
            continue
        scale = (cap_per_year / total) if (cap_per_year and total > cap_per_year) else 1.0
        inc_np = incident_to_for.get(npi)
        for code, desc, n, submitted, allowed, paid in code_rows:
            for _ in range(max(1, int(round(n * scale)))):
                f = rng.choice(pool)
                d = f["date"][:10] if f.get("date", "")[:4] == str(yr) else f"{yr}-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}"
                if npi in np_enrollment and d < np_enrollment[npi]:
                    continue
                rendering, supervising = npi, ""
                if inc_np and code in ESTABLISHED and rng.random() < established_rendering_fraction:
                    rendering, supervising = inc_np, npi  # compliant incident-to (established only)
                out.append({
                    "claim_id": f"{prefix}-{cid:07d}", "bene_id": f["bene"], "service_date": d, "year": yr,
                    "billed_under_npi": npi, "rendering_npi": rendering, "supervising_npi": supervising,
                    "hcpcs": code, "hcpcs_desc": desc, "pos": POS_OFFICE, "units": 1,
                    "submitted_charge": submitted, "allowed_amt": allowed, "paid_amt": paid,
                    "new_patient": code in NEW_PATIENT,
                })
                cid += 1
    return pd.DataFrame(out)


# ---- scheme: incident_to -----------------------------------------------------------------
def _scheme_incident_to__build_subject_claims(profile, fabric, rng, cfg: Config) -> pd.DataFrame:
    """Subject: reproduce its real (year,hcpcs,services,benes,$) EXACTLY at the billed-under
    level; split RENDERING between subject and the scheme NPs per the (enrollment-gated)
    per-year intensities in cfg.scheme.intensity_by_year."""
    np_ids = list(cfg.scheme["renderer_npis"].keys())
    np_enroll = cfg.np_enrollment
    intensity = cfg.scheme["intensity_by_year"]
    np_share_est = {int(y): float(s) for y, s in intensity["established"].items()}
    np_share_new = {int(y): float(s) for y, s in intensity["new_patient"].items()}

    out, cid = [], 0
    for row in profile:
        yr, code, n_svc, n_benes = row["year"], row["hcpcs"], row["services"], row["benes"]
        if n_svc <= 0:
            continue
        pool = fabric.get(yr) or [{"bene": f"SYN-{yr}-{i}", "date": f"{yr}-06-15"} for i in range(n_benes)]
        benes = (rng.sample([f["bene"] for f in pool], min(n_benes, len(pool)))
                 if len(pool) >= n_benes else [rng.choice(pool)["bene"] for _ in range(n_benes)])
        date_by_bene = {f["bene"]: f["date"] for f in pool}
        assign = [benes[i % len(benes)] for i in range(n_svc)]
        if code in ESTABLISHED:
            np_share = np_share_est.get(yr, 0.0)
        elif code in NEW_PATIENT:
            np_share = np_share_new.get(yr, 0.0)
        else:
            np_share = 0.0  # ancillary (labs, vaccines, G-codes): rendered by MD
        for bene in assign:
            base_date = date_by_bene.get(bene, f"{yr}-06-15")
            d = base_date if base_date[:4] == str(yr) else f"{yr}-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}"
            eligible_nps = [npi for npi in np_ids if d >= np_enroll[npi]]
            np_rendered = bool(eligible_nps) and (rng.random() < np_share)
            rendering = rng.choice(eligible_nps) if np_rendered else cfg.subject_npi
            out.append({
                "claim_id": f"SUBJ-{cid:07d}", "bene_id": bene, "service_date": d, "year": yr,
                "billed_under_npi": cfg.subject_npi, "rendering_npi": rendering,
                "supervising_npi": cfg.subject_npi if np_rendered else "",
                "hcpcs": code, "hcpcs_desc": row["desc"], "pos": POS_OFFICE, "units": 1,
                "submitted_charge": row["submitted"], "allowed_amt": row["allowed"], "paid_amt": row["paid"],
                "new_patient": code in NEW_PATIENT,
            })
            cid += 1
    return pd.DataFrame(out)


# Scheme dispatcher: extend here when a new scheme type lands (e.g., phantom_billing).
# Each scheme function: (profile, fabric, rng, cfg) -> claims DataFrame for the SUBJECT
# (the dispatcher handles renderer-own + co-located + peers uniformly via disaggregate_real).
SCHEME_BUILDERS = {
    "incident_to": _scheme_incident_to__build_subject_claims,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, type=Path,
                    help="Path to a synth-claims JSON config (see synth_configs/README.md).")
    ap.add_argument("--export", required=True, type=Path,
                    help="Calibration export dir (has data/panels and data/billing_pre_cutoff.json).")
    ap.add_argument("--out", required=True, type=Path,
                    help="Output dir; writes <out>/synth_claims.parquet + verification report.")
    ap.add_argument("--synthea", type=Path, default=None,
                    help="Override the config's synthea_dir (rarely needed).")
    ap.add_argument("--peers", type=int, default=None,
                    help="Override the config's peer_cohort.count.")
    ap.add_argument("--peer-cap", type=int, default=None,
                    help="Override the config's peer_cohort.claims_cap_per_year_per_peer.")
    ap.add_argument("--seed", type=int, default=None,
                    help="Override the config's seed (use with extreme care — breaks reproducibility).")
    a = ap.parse_args()

    cfg = load_config(a.config)
    rng = random.Random(a.seed if a.seed is not None else cfg.seed)
    synthea_dir = a.synthea or cfg.synthea_dir
    peers_n = a.peers if a.peers is not None else int(cfg.peer_cohort.get("count", 150))
    peer_cap = a.peer_cap if a.peer_cap is not None else int(cfg.peer_cohort.get("claims_cap_per_year_per_peer", 60))

    profile = load_real_profile(a.export, cfg.cutoff_years)
    fabric = load_synthea_fabric(synthea_dir, cfg.cutoff_years)
    panels = _panels(a.export, cfg.cutoff_years)
    print(f"Config: {a.config.name} | case_id={cfg.case_id} | scheme={cfg.scheme['type']} | "
          f"subject={cfg.subject_npi}")
    print(f"Real profile rows ({cfg.case_id}): {len(profile)} | panels: {sorted(panels)} | "
          f"fabric: {sum(len(v) for v in fabric.values()):,} synthea claims")
    con = duckdb.connect()
    np_enroll = cfg.np_enrollment

    # SUBJECT — roll-up-grounded + scheme injection (dispatched by scheme.type).
    builder = SCHEME_BUILDERS[cfg.scheme["type"]]
    subj = builder(profile, fabric, rng, cfg)

    # SCHEME RENDERERS' own-NPI billing + CO-LOCATED providers: REAL panel rows, fully
    # disaggregated, NO scheme injection. (These are NOT the subject's billing; they're the
    # renderers' / co-located peers' own billing for use as within-building controls.)
    renderer_npis = list(cfg.scheme.get("renderer_npis", {}).keys())
    co_located_npis = list(cfg.scheme.get("co_located_npis", []))
    npown = disaggregate_real(panel_rows(con, panels, renderer_npis), fabric, rng, "NPOWN", np_enroll)
    colo = disaggregate_real(panel_rows(con, panels, co_located_npis), fabric, rng, "COLO", np_enroll)

    # PEER COHORT — configurable state/specialty, sampled E&M only, capped per peer per year.
    # A configurable fraction run COMPLIANT incident-to via a random real NP NPI, so
    # 'rendering != billed-under' is a NORMAL pattern (not a fraud label) for the agent.
    pc = cfg.peer_cohort
    peer_where = f"Rndrng_Prvdr_State_Abrvtn='{pc['state']}' AND Rndrng_Prvdr_Type='{pc['specialty']}'"
    exclude = [cfg.subject_npi] + renderer_npis + co_located_npis
    peer_npis = _distinct_npis(con, panels, peer_where, exclude, rng, peers_n)
    cb = cfg.compliant_background
    incident_to_for = {}
    est_rendering_frac = 0.22
    if cb and cb.get("renderer_pool"):
        rp = cb["renderer_pool"]
        rp_where = f"Rndrng_Prvdr_State_Abrvtn='{rp['state']}' AND Rndrng_Prvdr_Type='{rp['specialty']}'"
        rp_min = int(rp.get("pool_size_min", 20))
        np_pool = _distinct_npis(con, panels, rp_where, exclude, rng,
                                 max(rp_min, peers_n // 4))
        assign_frac = float(cb.get("peer_assignment_fraction", 0.30))
        est_rendering_frac = float(cb.get("established_rendering_fraction", 0.22))
        incident_to_for = {p: rng.choice(np_pool) for p in peer_npis
                           if np_pool and rng.random() < assign_frac}
    peers = disaggregate_real(panel_rows(con, panels, peer_npis), fabric, rng, "PEER",
                              np_enroll, cap_per_year=peer_cap, em_only=bool(pc.get("em_only", True)),
                              incident_to_for=incident_to_for,
                              established_rendering_fraction=est_rendering_frac)

    claims = pd.concat([subj, npown, colo, peers], ignore_index=True)
    claims = claims.sample(frac=1.0, random_state=cfg.seed).reset_index(drop=True)
    a.out.mkdir(parents=True, exist_ok=True)
    pq = a.out / "synth_claims.parquet"
    claims.to_parquet(pq, index=False)

    # ---------------- verification report ----------------
    print(f"\nWrote {len(claims):,} claims -> {pq}")
    print(f"  subject={len(subj):,}  np_own={len(npown):,}  co_located={len(colo):,}  peers={len(peers):,}")
    allnpis = set(claims["billed_under_npi"]) | set(claims["rendering_npi"]) | set(x for x in claims["supervising_npi"] if x)
    fabricated = [n for n in allnpis if len(str(n)) != 10]
    print(f"  distinct billing NPIs: {claims['billed_under_npi'].nunique()} | NPIs any role: {len(allnpis)} | "
          f"fabricated (non-10-digit): {len(fabricated)} " + ("✅ none — all real" if not fabricated else f"⚠️ {fabricated[:3]}"))
    # roll-up: subject MUST still reproduce the real profile exactly
    rr = subj.groupby(["year", "hcpcs"]).size().reset_index(name="n")
    real = {(r["year"], r["hcpcs"]): r["services"] for r in profile}
    mism = sum(1 for _, r in rr.iterrows() if real.get((r["year"], r["hcpcs"]), 0) != r["n"])
    print(f"  subject roll-up vs real partb_svc: {len(rr)-mism}/{len(rr)} cells exact" + (" ✅" if not mism else f" ⚠️ {mism} off"))
    # pre-enrollment artifact check (any renderer-attributed claim before enrollment)
    if np_enroll:
        cross = claims[(claims["rendering_npi"] != claims["billed_under_npi"]) & (claims["rendering_npi"].isin(np_enroll))]
        bad = cross[cross.apply(lambda r: r["service_date"] < np_enroll[r["rendering_npi"]], axis=1)]
        print(f"  pre-enrollment renderer attributions: {len(bad)} " + ("✅" if len(bad) == 0 else "⚠️"))
    # rendering != billed-under as a normal-pattern check (subject should NOT be the only one)
    cr = claims[claims["rendering_npi"] != claims["billed_under_npi"]]
    print(f"  rendering!=billed-under spans {cr['billed_under_npi'].nunique()} billing NPIs "
          f"(subject is 1 of them — NOT a fraud label)")
    # the smoking gun: scheme-attributed share of subject volume by year
    for yr in cfg.cutoff_years:
        s = subj[subj["year"] == yr]
        if len(s):
            print(f"    {yr}: {len(s):>4} billed-under-subject, "
                  f"{(s['rendering_npi']!=cfg.subject_npi).mean()*100:4.0f}% scheme-rendered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
