"""Parse the CMS Medicare Coverage Database zips into queryable parquets.

Source: data/raw/lcd_ncd/{ncd,current_lcd,all_lcd,current_article,all_article}.zip
Each outer zip contains an inner ``*_csv.zip`` with relational CSVs:
  - ``lcd.csv`` / ``article.csv`` / ``ncd_trkg.csv`` — main records
  - ``lcd_x_hcpc_code.csv`` / ``article_x_hcpc_code.csv`` — HCPCS mappings
  - ``lcd_x_contractor.csv`` / ``article_x_contractor.csv`` — MAC mappings
  - ``contractor.csv`` — MAC contractor metadata (LBN, MAC number)

This parser denormalizes per (lcd_id, lcd_version) (row-per-version), with
many-to-many fields as list columns:
  - ``hcpcs_codes`` — list of HCPCS codes the determination covers
  - ``mac_jurisdictions`` — list of MAC numbers (e.g. ["J3", "J5"])

NCDs ship without an HCPCS join in the CSV format; we still produce a
parquet keyed on (NCD_id, NCD_vrsn_num) with the substantive text fields.

Usage:
    python scripts/ingest/parse_lcd_ncd.py                # parse all 5 zips
    python scripts/ingest/parse_lcd_ncd.py --only ncd     # one source only
"""
from __future__ import annotations

import argparse
import io
import sys
import zipfile
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = REPO_ROOT / "data" / "raw" / "lcd_ncd"
OUT_DIR = REPO_ROOT / "data" / "processed"


def _read_inner_csvs(outer_zip: Path, inner_zip_name: str) -> dict[str, pd.DataFrame]:
    """Open outer zip, extract inner ``*_csv.zip``, return dict of csv_name → DataFrame.

    Inner CSV is decoded with latin-1 to handle the occasional non-ASCII byte
    in coverage text. Empty fields stay as empty strings (not NaN) so list
    columns serialize predictably.
    """
    with zipfile.ZipFile(outer_zip) as outer:
        with outer.open(inner_zip_name) as inner_fp:
            inner_bytes = inner_fp.read()

    out: dict[str, pd.DataFrame] = {}
    with zipfile.ZipFile(io.BytesIO(inner_bytes)) as inner:
        for name in inner.namelist():
            if not name.endswith(".csv"):
                continue
            with inner.open(name) as fp:
                df = pd.read_csv(
                    fp,
                    encoding="latin-1",
                    low_memory=False,
                    dtype=str,
                ).fillna("")
            out[name] = df
    return out


def _to_list(series: pd.Series) -> list[str]:
    """Convert a Series of strings to a unique list preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for v in series:
        v = (v or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def parse_ncd(outer_zip: Path) -> pd.DataFrame:
    csvs = _read_inner_csvs(outer_zip, "ncd_csv.zip")
    trkg = csvs["ncd_trkg.csv"].copy()

    # Optional benefit-category cross-ref
    bnft_xref = csvs.get("ncd_trkg_bnft_xref.csv", pd.DataFrame())
    bnft_ref = csvs.get("ncd_bnft_ctgry_ref.csv", pd.DataFrame())

    if not bnft_xref.empty and not bnft_ref.empty:
        bnft = bnft_xref.merge(bnft_ref, on="bnft_ctgry_cd", how="left")
        bnft_grouped = (
            bnft.groupby(["NCD_id", "NCD_vrsn_num"])["bnft_ctgry_desc"]
            .apply(_to_list)
            .rename("benefit_categories")
        )
        trkg = trkg.merge(bnft_grouped, on=["NCD_id", "NCD_vrsn_num"], how="left")
    else:
        trkg["benefit_categories"] = [[] for _ in range(len(trkg))]

    # Rename to the unified schema
    out = pd.DataFrame({
        "ncd_id":         trkg["NCD_id"],
        "ncd_version":    trkg["NCD_vrsn_num"],
        "manual_section": trkg["NCD_mnl_sect"],
        "title":          trkg["NCD_mnl_sect_title"],
        "effective_date": pd.to_datetime(trkg["NCD_efctv_dt"], errors="coerce"),
        "implement_date": pd.to_datetime(trkg["NCD_impltn_dt"], errors="coerce"),
        "termination_date": pd.to_datetime(trkg["NCD_trmntn_dt"], errors="coerce"),
        "item_service_desc": trkg["itm_srvc_desc"],
        "coverage_text":  trkg["indctn_lmtn"],
        "xref_text":      trkg["xref_txt"],
        "other_text":     trkg["othr_txt"],
        "transmittal_num": trkg["trnsmtl_num"],
        "transmittal_url": trkg["trnsmtl_url"],
        "publication_code": trkg["pblctn_cd"],
        "keywords":       trkg["ncd_keyword"],
        "last_updated":   pd.to_datetime(trkg["last_updt_tmstmp"], errors="coerce"),
        "benefit_categories": trkg.get("benefit_categories", [[] for _ in range(len(trkg))]),
        "source_zip":     outer_zip.name,
    })
    return out


def _parse_lcd_or_article(
    outer_zip: Path,
    inner_zip_name: str,
    main_csv: str,
    hcpc_csv: str,
    contractor_csv: str,
    id_col: str,
    version_col: str,
) -> pd.DataFrame:
    csvs = _read_inner_csvs(outer_zip, inner_zip_name)
    main = csvs[main_csv].copy()

    # ── HCPCS list per (id, version) ──
    hcpcs_codes_grouped: pd.Series
    if hcpc_csv in csvs and not csvs[hcpc_csv].empty:
        hcpcs_df = csvs[hcpc_csv][[id_col, version_col, "hcpc_code_id"]]
        hcpcs_codes_grouped = (
            hcpcs_df.groupby([id_col, version_col])["hcpc_code_id"]
            .apply(_to_list)
            .rename("hcpcs_codes")
        )
    else:
        hcpcs_codes_grouped = pd.Series(dtype=object, name="hcpcs_codes")

    # ── MAC jurisdictions per (id, version) via contractor join ──
    mac_grouped: pd.Series
    if contractor_csv in csvs and "contractor.csv" in csvs and not csvs[contractor_csv].empty:
        ctr_xref = csvs[contractor_csv][[id_col, version_col, "contractor_id"]]
        ctr = csvs["contractor.csv"][["contractor_id", "contractor_number", "contractor_bus_name"]]
        joined = ctr_xref.merge(ctr, on="contractor_id", how="left")
        mac_grouped = (
            joined.groupby([id_col, version_col])["contractor_number"]
            .apply(_to_list)
            .rename("mac_jurisdictions")
        )
    else:
        mac_grouped = pd.Series(dtype=object, name="mac_jurisdictions")

    merged = main.merge(hcpcs_codes_grouped, on=[id_col, version_col], how="left")
    merged = merged.merge(mac_grouped, on=[id_col, version_col], how="left")

    # Fill missing lists
    for col in ("hcpcs_codes", "mac_jurisdictions"):
        merged[col] = merged[col].apply(lambda v: v if isinstance(v, list) else [])

    merged["source_zip"] = outer_zip.name
    return merged


def parse_lcd(outer_zip: Path, inner_zip_name: str) -> pd.DataFrame:
    df = _parse_lcd_or_article(
        outer_zip,
        inner_zip_name=inner_zip_name,
        main_csv="lcd.csv",
        hcpc_csv="lcd_x_hcpc_code.csv",
        contractor_csv="lcd_x_contractor.csv",
        id_col="lcd_id",
        version_col="lcd_version",
    )

    out = pd.DataFrame({
        "lcd_id":          df["lcd_id"],
        "lcd_version":     df["lcd_version"],
        "title":           df["title"],
        "display_id":      df.get("display_id", ""),
        "status":          df.get("status", ""),
        "orig_eff_date":   pd.to_datetime(df.get("orig_det_eff_date", ""), errors="coerce"),
        "end_date":        pd.to_datetime(df.get("ent_det_end_date", ""), errors="coerce"),
        "rev_eff_date":    pd.to_datetime(df.get("rev_eff_date", ""), errors="coerce"),
        "rev_end_date":    pd.to_datetime(df.get("rev_end_date", ""), errors="coerce"),
        "date_retired":    pd.to_datetime(df.get("date_retired", ""), errors="coerce"),
        "coverage_text":   df.get("indication", ""),
        "diagnoses_support": df.get("diagnoses_support", ""),
        "noncoverage_text": df.get("diagnoses_dont_support", ""),
        "coding_guidelines": df.get("coding_guidelines", ""),
        "documentation_requirements": df.get("doc_reqs", ""),
        "utilization_guidelines": df.get("util_guide", ""),
        "summary_of_evidence": df.get("summary_of_evidence", ""),
        "analysis_of_evidence": df.get("analysis_of_evidence", ""),
        "bibliography":    df.get("bibliography", ""),
        "icd10_doc":       df.get("icd10_doc", ""),
        "keywords":        df.get("keywords", ""),
        "hcpcs_codes":     df["hcpcs_codes"],
        "mac_jurisdictions": df["mac_jurisdictions"],
        "last_updated":    pd.to_datetime(df.get("last_updated", ""), errors="coerce"),
        "source_zip":      df["source_zip"],
    })
    return out


def parse_article(outer_zip: Path, inner_zip_name: str) -> pd.DataFrame:
    df = _parse_lcd_or_article(
        outer_zip,
        inner_zip_name=inner_zip_name,
        main_csv="article.csv",
        hcpc_csv="article_x_hcpc_code.csv",
        contractor_csv="article_x_contractor.csv",
        id_col="article_id",
        version_col="article_version",
    )

    out = pd.DataFrame({
        "article_id":      df["article_id"],
        "article_version": df["article_version"],
        "article_type":    df.get("article_type", ""),
        "title":           df["title"],
        "display_id":      df.get("display_id", ""),
        "status":          df.get("status", ""),
        "pub_date":        pd.to_datetime(df.get("article_pub_date", ""), errors="coerce"),
        "eff_date":        pd.to_datetime(df.get("article_eff_date", ""), errors="coerce"),
        "end_date":        pd.to_datetime(df.get("article_end_date", ""), errors="coerce"),
        "rev_end_date":    pd.to_datetime(df.get("article_rev_end_date", ""), errors="coerce"),
        "date_retired":    pd.to_datetime(df.get("date_retired", ""), errors="coerce"),
        "description":     df.get("description", ""),
        "comments":        df.get("other_comments", ""),
        "icd10_doc":       df.get("icd10_doc", ""),
        "keywords":        df.get("keywords", ""),
        "hcpcs_codes":     df["hcpcs_codes"],
        "mac_jurisdictions": df["mac_jurisdictions"],
        "last_updated":    pd.to_datetime(df.get("last_updated", ""), errors="coerce"),
        "source_zip":      df["source_zip"],
    })
    return out


_JOBS = {
    "ncd":             ("ncd.zip",             parse_ncd,     None,                "ncd.parquet"),
    "lcd_current":     ("current_lcd.zip",     parse_lcd,     "current_lcd_csv.zip",     "lcd_current.parquet"),
    "lcd_all":         ("all_lcd.zip",         parse_lcd,     "all_lcd_csv.zip",         "lcd_all.parquet"),
    "article_current": ("current_article.zip", parse_article, "current_article_csv.zip", "article_current.parquet"),
    "article_all":     ("all_article.zip",     parse_article, "all_article_csv.zip",     "article_all.parquet"),
}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--only", choices=list(_JOBS.keys()), help="Run only one source")
    p.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    p.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = p.parse_args()

    jobs = [args.only] if args.only else list(_JOBS.keys())
    args.out_dir.mkdir(parents=True, exist_ok=True)

    for key in jobs:
        outer_name, fn, inner_name, out_name = _JOBS[key]
        outer = args.raw_dir / outer_name
        if not outer.exists():
            print(f"[skip {key}] {outer} not found")
            continue
        print(f"[{key}] parsing {outer.name} ...")
        if key == "ncd":
            df = fn(outer)
        else:
            df = fn(outer, inner_name)
        out_path = args.out_dir / out_name
        df.to_parquet(out_path, index=False)
        print(f"[{key}]   wrote {len(df):,} rows → {out_path.name} "
              f"({out_path.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
