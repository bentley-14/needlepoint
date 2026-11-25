#!/usr/bin/env python3
"""Audit cross-year dtype + schema inconsistencies in year-partitioned parquets.

Detects the bug class that produced a false negative in the Cedar claims audit:
Open Payments stored the recipient NPI as VARCHAR in 2013-2017 but DOUBLE
("1900000075.0") in 2019+, so exact-string NPI joins silently missed every later
year. This scans each year-partitioned parquet *family* (via DESCRIBE — metadata
only, no data scan) for:

  1. DTYPE DRIFT      — a column whose dtype changes across years (breaks joins /
                        aggregations; the NPI bug).
  2. NUMERIC-AS-TEXT  — an amount/count-named column stored as VARCHAR (can't
                        SUM without TRY_CAST; e.g. Total_Amount_of_Payment).
  3. PRESENCE DRIFT   — a column present in only some years (usually benign CMS
                        schema evolution; flagged for awareness).

Ranks by join/aggregation risk and writes a markdown report.

Run: python scripts/audit_data_consistency.py
     python scripts/audit_data_consistency.py --data-dir data/panels --md data_consistency_audit.md
"""
from __future__ import annotations

import argparse
import glob
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[1]
NUMERIC = {"BIGINT", "INTEGER", "DOUBLE", "DECIMAL", "HUGEINT", "FLOAT", "SMALLINT", "BOOLEAN"}
# Tokens that mark a column as an aggregatable amount/count. ``endswith`` for the
# suffix tokens so ``_cnt`` matches ``Bene_Feml_Cnt`` but NOT ``Prscrbr_Cntry``
# (country); substring for the unambiguous amount words.
_AMOUNT_SUFFIX = ("_cnt", "_amt", "_cst", "_pct", "_rate", "_srvcs", "_clms",
                  "_benes", "_suply", "_scre")
_AMOUNT_SUB = ("amount", "chrg", "alowd", "pymt", "sbmtd", "cost")


def is_npi(col: str) -> bool:
    return "npi" in col.lower()


def is_amountish(col: str) -> bool:
    c = col.lower()
    return c.endswith(_AMOUNT_SUFFIX) or any(k in c for k in _AMOUNT_SUB)


def find_families(data_dir: Path) -> dict[str, dict[int, str]]:
    fam: dict[str, dict[int, str]] = defaultdict(dict)
    for f in sorted(glob.glob(str(data_dir / "*.parquet"))):
        m = re.match(r"(.+)_(\d{4})\.parquet$", Path(f).name)
        if m:
            fam[m.group(1)][int(m.group(2))] = f
    return {k: v for k, v in fam.items() if len(v) >= 2}


def describe(path: str) -> dict[str, str]:
    return {c[0]: c[1] for c in duckdb.sql(
        f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()}


def _ranges(ymap: dict[int, str]) -> str:
    """{2013:'VARCHAR',...,2019:'DOUBLE',...} -> 'VARCHAR (2013-2017), DOUBLE (2019-2024)'."""
    by_dt: dict[str, list[int]] = defaultdict(list)
    for y, dt in sorted(ymap.items()):
        by_dt[dt].append(y)
    parts = []
    for dt, yrs in by_dt.items():
        spans, start, prev = [], yrs[0], yrs[0]
        for y in yrs[1:]:
            if y == prev + 1:
                prev = y
            else:
                spans.append(f"{start}-{prev}" if start != prev else f"{start}")
                start = prev = y
        spans.append(f"{start}-{prev}" if start != prev else f"{start}")
        parts.append(f"{dt} ({', '.join(spans)})")
    return "; ".join(parts)


def scan(families: dict[str, dict[int, str]]) -> list[dict]:
    findings = []
    for family, years in families.items():
        colmap: dict[str, dict[int, str]] = defaultdict(dict)
        for y, f in sorted(years.items()):
            try:
                for col, dt in describe(f).items():
                    colmap[col][y] = dt
            except Exception as e:  # noqa: BLE001
                print(f"  ! DESCRIBE failed for {f}: {e}")
        yrs = sorted(years)
        for col, ymap in colmap.items():
            dts = set(ymap.values())
            drift = len(dts) > 1
            numeric_as_text = (dts == {"VARCHAR"}) and is_amountish(col)
            presence = len(ymap) < len(yrs)
            if not (drift or numeric_as_text or presence):
                continue
            if is_npi(col) and drift:
                sev, kind = "CRITICAL", "join-key dtype drift"
            elif drift and ("VARCHAR" in dts) and (dts & NUMERIC) and is_amountish(col):
                sev, kind = "CRITICAL", "numeric column is text in some years"
            elif numeric_as_text:
                sev, kind = "HIGH", "numeric-named column stored as text (all years)"
            elif drift and ("VARCHAR" in dts) and (dts & NUMERIC):
                sev, kind = "HIGH", "dtype drift (text<->numeric)"
            elif drift:
                sev, kind = "MEDIUM", "dtype drift"
            else:
                sev, kind = "INFO", "present in subset of years"
            findings.append({
                "family": family, "column": col, "severity": sev, "kind": kind,
                "detail": _ranges(ymap) if drift else (
                    f"VARCHAR all years ({yrs[0]}-{yrs[-1]})" if numeric_as_text
                    else f"present {sorted(ymap)}"),
                "is_npi": is_npi(col),
            })
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "INFO": 3}
    findings.sort(key=lambda r: (order[r["severity"]], r["family"], r["column"]))
    return findings


def write_report(findings: list[dict], families: dict, md_path: Path) -> None:
    counts = defaultdict(int)
    for r in findings:
        counts[r["severity"]] += 1
    L = []
    L.append("# Cross-Year Data Consistency Audit\n")
    L.append(f"_Generated {date.today().isoformat()} by `scripts/audit_data_consistency.py` "
             f"(DESCRIBE-only; re-run any time)._\n")
    L.append("## Why\n")
    L.append("These year-partitioned parquets are joined and aggregated across years. When a "
             "column's **dtype drifts** across years, exact-match joins and `SUM`s silently "
             "fail on the off-type years. This was not hypothetical: an Open Payments query keyed "
             "on `Covered_Recipient_NPI` returned **zero** rows for 2018-2023 (NPI stored as "
             "`DOUBLE` `1900000075.0` there vs `VARCHAR` earlier), nearly flagging a real "
             "kickback pattern as fabricated. This report enumerates every such inconsistency so "
             "they can be normalized.\n")
    L.append("> **Verified against R2 (2026-06-01):** the CRITICAL/HIGH dtype issues below exist "
             "*identically* on the canonical store `r2:needlepoint-data/processed/v1/` (checked "
             "`open_payments_general` 2017/2019 + `cms_partd` 2019/2024 via DuckDB+httpfs). They are "
             "**producer-level, not local staleness** — local copies are older and leaner (R2 carries "
             "+7 provenance cols), but re-syncing from R2 would NOT fix the dtypes. The fix belongs at "
             "the scraper/ingest, then re-upload to R2.\n")
    L.append("## Summary\n")
    L.append(f"- Families scanned: **{len(families)}** "
             f"({', '.join(sorted(families))}).")
    L.append(f"- Findings: **{counts['CRITICAL']} CRITICAL**, **{counts['HIGH']} HIGH**, "
             f"{counts['MEDIUM']} MEDIUM, {counts['INFO']} INFO (schema evolution).\n")

    def table(rows):
        out = ["| Family | Column | Issue | Across years |", "|---|---|---|---|"]
        for r in rows:
            out.append(f"| `{r['family']}` | `{r['column']}` | {r['kind']} | {r['detail']} |")
        return "\n".join(out) + "\n"

    for sev, title in [("CRITICAL", "CRITICAL — join keys + numeric-as-text (break joins / SUMs)"),
                       ("HIGH", "HIGH — numeric columns stored as text / text↔numeric drift"),
                       ("MEDIUM", "MEDIUM — other dtype drift")]:
        rows = [r for r in findings if r["severity"] == sev]
        L.append(f"## {title}\n")
        L.append(table(rows) if rows else "_None._\n")

    info = [r for r in findings if r["severity"] == "INFO"]
    L.append("## INFO — columns present in only some years (likely CMS schema evolution)\n")
    if info:
        by_fam = defaultdict(list)
        for r in info:
            by_fam[r["family"]].append(r["column"])
        for fam in sorted(by_fam):
            cols = by_fam[fam]
            L.append(f"- `{fam}`: {len(cols)} columns vary in presence "
                     f"(e.g. {', '.join('`'+c+'`' for c in cols[:5])}{'…' if len(cols) > 5 else ''}).")
        L.append("")
    else:
        L.append("_None._\n")

    # Many columns in one family sharing the EXACT same drift pattern => one root
    # cause (a single bad year-ingest), fixable as one re-produce, not N fixes.
    from collections import Counter
    clusters = Counter((r["family"], r["detail"]) for r in findings
                       if r["severity"] in ("CRITICAL", "HIGH", "MEDIUM"))
    big = sorted([(f, d, n) for (f, d), n in clusters.items() if n >= 4], key=lambda x: -x[2])
    if big:
        L.append("## Notable clusters (probable single root cause)\n")
        for fam, det, n in big:
            L.append(f"- **`{fam}`: {n} columns** drift identically — `{det}`. Points to one "
                     f"bad year-ingest, fixable as a single re-produce, not {n} separate fixes.")
        L.append("")

    L.append("## How to address\n")
    L.append("**Preferred — normalize at the producer (the `pipeline.BaseScraper`).** Declare the "
             "canonical dtype in the scraper's schema dataclass and cast in the parser, so every "
             "year's parquet is uniform on write:\n")
    L.append("- **NPI / join keys** → zero-padded 10-char string (`ProviderRecordSchema.npi` "
             "convention): `df['npi'] = df[raw].astype('Int64').astype(str).str.zfill(10)`.\n"
             "- **Amounts / counts** → numeric: `df[col] = pd.to_numeric(df[col], errors='coerce')`.")
    L.append("")
    L.append("**Interim — one-time normalization with atomic write** for existing local parquets "
             "not yet re-produced. Cast the offending columns and re-store via temp-file + atomic "
             "rename (never edit in place):\n")
    L.append("```python\n"
             "import duckdb, os\n"
             "# example: normalize Covered_Recipient_NPI -> zero-padded VARCHAR\n"
             "for f in sorted(glob.glob('data/processed/open_payments_general_*.parquet')):\n"
             "    tmp = f + '.tmp'\n"
             "    duckdb.sql(f\"\"\"COPY (\n"
             "        SELECT * REPLACE (\n"
             "          lpad(CAST(CAST(Covered_Recipient_NPI AS BIGINT) AS VARCHAR), 10, '0')\n"
             "          AS Covered_Recipient_NPI)\n"
             "        FROM read_parquet('{f}')) TO '{tmp}' (FORMAT PARQUET)\"\"\")\n"
             "    os.replace(tmp, f)   # atomic\n"
             "```\n")
    L.append("**Defensive — until normalized**, consumers must match format-robustly: "
             "`CAST(npi AS VARCHAR) IN ('NNN','NNN.0')` for joins and `TRY_CAST(amt AS DOUBLE)` "
             "for sums. (Provider Cedar audit used exactly these after hitting the bug.)\n")
    L.append("## Reproduce\n```\npython scripts/audit_data_consistency.py\n```\n")

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(L))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="data/processed", type=str)
    ap.add_argument("--md", default="data_consistency_audit.md", type=str)
    args = ap.parse_args()

    data_dir = (REPO / args.data_dir) if not Path(args.data_dir).is_absolute() else Path(args.data_dir)
    families = find_families(data_dir)
    print(f"scanning {len(families)} families under {data_dir} ...")
    findings = scan(families)
    md_path = (REPO / args.md) if not Path(args.md).is_absolute() else Path(args.md)
    write_report(findings, families, md_path)

    from collections import Counter
    c = Counter(r["severity"] for r in findings)
    print(f"findings: {dict(c)}")
    for r in [r for r in findings if r["severity"] in ("CRITICAL", "HIGH")]:
        print(f"  [{r['severity']:8}] {r['family']}.{r['column']} — {r['kind']}: {r['detail']}")
    print(f"\nreport -> {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
