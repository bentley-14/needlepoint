"""Medicare Care Compare (née Physician Compare) ingest + features.

CMS publishes the Doctors and Clinicians National Downloadable File
(``DAC_NationalDownloadableFile.csv``) as part of the Care Compare
program. One row per clinician-enrollment-record (so a physician
practicing at three group addresses has three rows). Fields we care
about:

  * ``NPI``
  * ``Ind_PAC_ID`` — stable clinician-level identity that persists
    across job changes (NPPES doesn't guarantee this).
  * ``Org_PAC_ID`` / ``Org_Lgl_Nm`` — group practice affiliation.
  * ``Num_Org_Mem`` — size of the group practice.
  * ``Hosp_Afl_1..5`` / ``Hosp_Afl_LBN_1..5`` — hospital affiliations
    (up to 5 per clinician enrollment).
  * ``pri_spec`` — primary specialty.
  * ``sec_spec_1..4`` — secondary specialties (multi-specialty
    practitioners flag differently in fraud patterns; e.g. "internal
    medicine + pain management" is unusual).

Why we care:
  * Group-practice size is a peer-comparison lever (solo practitioner
    with 95th percentile volume reads very differently from a
    partner in a 20-provider group pulling similar volume).
  * Hospital affiliations narrow down the facility side of the
    provider-facility bipartite graph, which matters for SNF /
    inpatient-fraud rules authored in future sprints.
  * Secondary specialties catch misrepresentation schemes (claiming
    specialty A while practicing mostly B — "specialty-billing
    arbitrage").

Landing page:
    https://data.cms.gov/provider-data/dataset/mj5m-pzi6

Only 2019+ is published; pre-2019 panels get silent-skip treatment.

Downstream: :func:`load_care_compare_features` returns one row per NPI
(collapsed across enrollment rows — group affiliations and hospital
affiliations become list-valued columns). Wired into
``query_tools.open_partb`` via ``with_care_compare=True``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

PROCESSED = Path("data/registries")
RAW = Path("data/raw") / "care_compare"

_HOSP_AFL_COLS = [f"Hosp_Afl_LBN_{i}" for i in range(1, 6)]
_SEC_SPEC_COLS = [f"sec_spec_{i}" for i in range(1, 5)]


def _csv_to_parquet(csv_path: Path, out_path: Path) -> Path:
    """Project the raw Care Compare CSV to the curated columns and write
    parquet. No feature derivation here — that happens in
    :func:`load_care_compare_features` so the raw snapshot stays
    reusable.
    """
    keep = [
        "NPI", "Ind_PAC_ID", "Org_PAC_ID", "Org_Lgl_Nm", "Num_Org_Mem",
        "pri_spec",
    ] + _SEC_SPEC_COLS + _HOSP_AFL_COLS

    df = pd.read_csv(csv_path, dtype=str, low_memory=False).fillna("")
    # Care Compare column casing has wobbled year over year; normalize
    # by doing a case-insensitive match first.
    upper_map = {c.upper(): c for c in df.columns}
    keep_present = [upper_map[k.upper()] for k in keep if k.upper() in upper_map]
    df = df[keep_present].rename(
        columns={upper_map[k.upper()]: k for k in keep if k.upper() in upper_map}
    )
    df["NPI"] = df["NPI"].astype(str).str.strip().str.zfill(10)
    df = df[df["NPI"].str.len() == 10]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    return out_path


def load_care_compare_features(
    parquet_path: Optional[Path] = None,
) -> pd.DataFrame:
    """Collapse the enrollment-row Care Compare parquet to per-NPI features.

    Output columns (all keyed by ``NPI``):
      * ``ind_pac_id`` — first non-empty Ind_PAC_ID for the NPI.
      * ``cc_primary_specialty`` — modal ``pri_spec`` for the NPI.
      * ``cc_secondary_specialties`` — list[str] of unique non-empty
        ``sec_spec_*`` values across all enrollment rows.
      * ``cc_n_secondary_specialties`` — int.
      * ``cc_group_practice_pac_id`` — largest-group affiliation (by
        ``Num_Org_Mem``) for the NPI. Ties broken by lexical.
      * ``cc_group_practice_name`` — corresponding ``Org_Lgl_Nm``.
      * ``cc_largest_group_size`` — int. Size of the largest group the
        NPI is enrolled with.
      * ``cc_n_group_affiliations`` — int. Distinct ``Org_PAC_ID``
        count for the NPI.
      * ``cc_hospital_affiliations`` — list[str] of unique hospital
        legal business names across all Hosp_Afl_LBN_* fields.
      * ``cc_n_hospital_affiliations`` — int.
    """
    parquet_path = parquet_path or (PROCESSED / "care_compare.parquet")
    if not parquet_path.exists():
        raise FileNotFoundError(
            f"{parquet_path} — run fetch_care_compare() first"
        )

    raw = pd.read_parquet(parquet_path)

    # Normalize missing-column scaffolding so aggregation below is
    # uniform across CMS casing drifts.
    for col in ["Ind_PAC_ID", "Org_PAC_ID", "Org_Lgl_Nm", "Num_Org_Mem",
                "pri_spec"] + _SEC_SPEC_COLS + _HOSP_AFL_COLS:
        if col not in raw.columns:
            raw[col] = ""

    raw["Num_Org_Mem_int"] = pd.to_numeric(
        raw["Num_Org_Mem"].replace("", "0"), errors="coerce"
    ).fillna(0).astype(int)

    def _mode_str(s: pd.Series) -> str:
        vals = [v for v in s.astype(str) if v]
        if not vals:
            return ""
        # pandas .mode() returns a Series; take lexically-first on ties.
        return pd.Series(vals).mode().iloc[0]

    def _unique_list(values: pd.Series) -> list[str]:
        seen: list[str] = []
        for v in values.astype(str):
            v = v.strip()
            if v and v not in seen:
                seen.append(v)
        return seen

    # Aggregate per NPI.
    rows = []
    for npi, g in raw.groupby("NPI", sort=False):
        ind_pac = next((v for v in g["Ind_PAC_ID"].astype(str) if v), "")
        primary = _mode_str(g["pri_spec"])

        sec_values = pd.concat([g[c] for c in _SEC_SPEC_COLS], ignore_index=True)
        sec_list = _unique_list(sec_values)

        hosp_values = pd.concat([g[c] for c in _HOSP_AFL_COLS], ignore_index=True)
        hosp_list = _unique_list(hosp_values)

        # Largest group the NPI is enrolled with.
        g_sorted = g.sort_values("Num_Org_Mem_int", ascending=False)
        largest_row = g_sorted.iloc[0]
        largest_pac = str(largest_row["Org_PAC_ID"]).strip()
        largest_name = str(largest_row["Org_Lgl_Nm"]).strip()
        largest_size = int(largest_row["Num_Org_Mem_int"])

        n_group = int(
            g["Org_PAC_ID"].astype(str).str.strip().replace("", pd.NA).nunique()
        )

        rows.append({
            "NPI": npi,
            "ind_pac_id": ind_pac,
            "cc_primary_specialty": primary,
            "cc_secondary_specialties": sec_list,
            "cc_n_secondary_specialties": len(sec_list),
            "cc_group_practice_pac_id": largest_pac,
            "cc_group_practice_name": largest_name,
            "cc_largest_group_size": largest_size,
            "cc_n_group_affiliations": n_group,
            "cc_hospital_affiliations": hosp_list,
            "cc_n_hospital_affiliations": len(hosp_list),
        })

    return pd.DataFrame(rows)
