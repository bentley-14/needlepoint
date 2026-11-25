"""Query the CMS LCD/NCD/Article coverage database (parsed parquets).

Backed by ``data/processed/{lcd_current,lcd_all,article_current,article_all,ncd}.parquet``
produced by ``scripts/ingest/parse_lcd_ncd.py``.

Query modes (combine flags freely):
  --hcpcs CODE              All determinations whose HCPCS list contains CODE
  --lcd-id ID               Specific LCD by id (e.g. 33312 or L33312)
  --ncd-id ID               Specific NCD by id
  --article-id ID           Specific article by id
  --mac JX                  MAC jurisdiction filter (e.g. J3, J5, J6)
  --keyword PATTERN         Regex search across coverage_text + title
  --effective-on YYYY-MM-DD Only determinations in force on this date —
                            critical for FCA cases (cite the rule
                            contemporaneous with the alleged false claim)

Data scope:
  --scope current|all|both  current (default) skips retired/historical;
                            ``all`` includes them; ``both`` runs both.
  --source lcd|ncd|article|all (default lcd)

Output:
  --format text|json|tsv    text (default) prints a readable summary;
                            json/tsv emit one record per row for piping

Examples:
    # Which LCDs cover HCPCS 87798 (molecular pathology)?
    python scripts/lcd_lookup.py --hcpcs 87798

    # Was LCD L33312 in force on 2021-06-15?
    python scripts/lcd_lookup.py --lcd-id 33312 --effective-on 2021-06-15

    # All Novitas-jurisdiction LCDs touching opioid prescribing
    python scripts/lcd_lookup.py --mac J6 --keyword "opioid|fentanyl"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
try:
    from fraudlib import r2
except Exception:  # calibration jail / standalone copy: no fraudlib → local-only, never R2
    from pathlib import Path as _RP
    class _LocalR2:
        R2_PANELS = ""
        @staticmethod
        def parquet_source(local, key): return str(local)
        @staticmethod
        def fetch_local(local, key): return _RP(local)
        @staticmethod
        def attach_r2(con): pass
        @staticmethod
        def duckdb_r2():
            import duckdb; return duckdb.connect()
    r2 = _LocalR2()
# Prefer cwd-relative data/processed (lets the same script work from a
# worktree without populated data/ — agents typically invoke from the
# project root). Falls back to the file-relative path when cwd has no
# data/processed/ dir.
_CWD_PROC = Path.cwd() / "data" / "processed"
PROC = _CWD_PROC if _CWD_PROC.exists() else REPO_ROOT / "data" / "processed"

_LCD_PARQUETS = {
    "current": PROC / "lcd_current.parquet",
    "all":     PROC / "lcd_all.parquet",
}
_ARTICLE_PARQUETS = {
    "current": PROC / "article_current.parquet",
    "all":     PROC / "article_all.parquet",
}
_NCD_PARQUET = PROC / "ncd.parquet"

# Effective-date column names per source — used by --effective-on to
# decide whether a row was in force on the target date.
_EFFECTIVE_DATE_COLS = {
    "lcd":     ("rev_eff_date", "rev_end_date", "orig_eff_date", "end_date"),
    "article": ("eff_date", "rev_end_date", "pub_date", "end_date"),
    "ncd":     ("effective_date", "termination_date"),
}


def _load(source: str, scope: str) -> pd.DataFrame:
    """Load the per-source parquet for the requested scope."""
    if source == "ncd":
        path = r2.fetch_local(_NCD_PARQUET, f"{r2.R2_PANELS}/{_NCD_PARQUET.name}")
        df = pd.read_parquet(path)
        df["__source"] = "ncd"
        return df
    elif source == "lcd":
        raw = _LCD_PARQUETS[scope]
    elif source == "article":
        raw = _ARTICLE_PARQUETS[scope]
    else:
        raise ValueError(f"Unknown source: {source}")
    path = r2.fetch_local(raw, f"{r2.R2_PANELS}/{raw.name}")
    df = pd.read_parquet(path)
    df["__source"] = source
    return df


def _strip_lcd_prefix(s: str) -> str:
    """Normalize 'L33312' / 'l33312' / '33312' to '33312' for lcd_id compare."""
    s = str(s).strip()
    if s.upper().startswith("L"):
        s = s[1:]
    return s


def _strip_ncd_prefix(s: str) -> str:
    s = str(s).strip()
    if s.upper().startswith("N"):
        s = s[1:]
    return s


def _is_iter(lst) -> bool:
    """Lists, tuples, and numpy ndarrays (what pyarrow returns) all qualify."""
    import numpy as np
    return isinstance(lst, (list, tuple, np.ndarray))


def filter_by_hcpcs(df: pd.DataFrame, hcpcs: str) -> pd.DataFrame:
    if df.empty or "hcpcs_codes" not in df.columns:
        return df.iloc[0:0]
    target = hcpcs.strip().upper()
    mask = df["hcpcs_codes"].apply(
        lambda lst: _is_iter(lst) and target in {str(c).upper() for c in lst}
    )
    return df[mask]


def filter_by_mac(df: pd.DataFrame, mac: str) -> pd.DataFrame:
    if df.empty or "mac_jurisdictions" not in df.columns:
        return df.iloc[0:0]
    target = mac.strip().upper()
    mask = df["mac_jurisdictions"].apply(
        lambda lst: _is_iter(lst) and target in {str(c).upper() for c in lst}
    )
    return df[mask]


def filter_by_id(df: pd.DataFrame, source: str, det_id: str) -> pd.DataFrame:
    if df.empty:
        return df
    if source == "lcd":
        return df[df["lcd_id"].astype(str) == _strip_lcd_prefix(det_id)]
    if source == "ncd":
        return df[df["ncd_id"].astype(str) == _strip_ncd_prefix(det_id)]
    if source == "article":
        norm = str(det_id).strip()
        if norm.upper().startswith("A"):
            norm = norm[1:]
        return df[df["article_id"].astype(str) == norm]
    return df


def filter_by_keyword(df: pd.DataFrame, pattern: str, source: str) -> pd.DataFrame:
    if df.empty:
        return df
    text_cols = [c for c in ("title", "coverage_text", "description", "keywords") if c in df.columns]
    rx = re.compile(pattern, re.IGNORECASE | re.DOTALL)
    mask = pd.Series(False, index=df.index)
    for c in text_cols:
        mask = mask | df[c].astype(str).str.contains(rx, na=False)
    return df[mask]


def filter_effective_on(df: pd.DataFrame, source: str, when: pd.Timestamp) -> pd.DataFrame:
    if df.empty:
        return df
    cols = _EFFECTIVE_DATE_COLS[source]
    # In-force iff: any populated effective-date <= when AND
    # (no termination/end date OR end-date > when).
    has_started = pd.Series(False, index=df.index)
    has_ended = pd.Series(False, index=df.index)
    for c in cols:
        if c not in df.columns:
            continue
        col = pd.to_datetime(df[c], errors="coerce")
        if c.endswith("_eff_date") or c.endswith("effective_date") or c == "pub_date":
            has_started = has_started | (col <= when)
        elif c.endswith("_end_date") or c == "termination_date":
            has_ended = has_ended | (col.notna() & (col <= when))
    return df[has_started & ~has_ended]


def _summarize(df: pd.DataFrame, source: str) -> list[dict]:
    """Distill a result frame down to the human-meaningful fields per row."""
    if df.empty:
        return []
    rows: list[dict] = []
    if source == "lcd":
        id_col, ver_col = "lcd_id", "lcd_version"
    elif source == "ncd":
        id_col, ver_col = "ncd_id", "ncd_version"
    else:
        id_col, ver_col = "article_id", "article_version"

    for _, r in df.iterrows():
        rec: dict = {
            "source": source,
            "id":      r[id_col],
            "version": r.get(ver_col, ""),
            "title":   r.get("title", ""),
        }
        for k in ("display_id", "status", "coverage_text", "description"):
            if k in df.columns:
                v = r.get(k, "")
                if isinstance(v, str) and len(v) > 600:
                    v = v[:600] + " …"
                rec[k] = v
        if source == "lcd":
            rec["orig_eff_date"] = str(r.get("orig_eff_date", "")) or None
            rec["rev_eff_date"] = str(r.get("rev_eff_date", "")) or None
            rec["end_date"] = str(r.get("end_date", "")) or None
        elif source == "ncd":
            rec["effective_date"] = str(r.get("effective_date", "")) or None
            rec["termination_date"] = str(r.get("termination_date", "")) or None
        else:
            rec["eff_date"] = str(r.get("eff_date", "")) or None
            rec["end_date"] = str(r.get("end_date", "")) or None
        for k in ("hcpcs_codes", "mac_jurisdictions"):
            if k in df.columns:
                val = r.get(k, [])
                rec[k] = list(val) if _is_iter(val) else []
        rec["last_updated"] = str(r.get("last_updated", "")) or None
        rows.append(rec)
    return rows


def _print_text(rows: list[dict]) -> None:
    if not rows:
        print("(no matching determinations)")
        return
    for r in rows:
        head = f"[{r['source'].upper()} {r.get('display_id') or r['id']} v{r['version']}] {r['title']}"
        print(head)
        if r['source'] == 'lcd':
            print(f"  eff: orig={r['orig_eff_date']}  rev={r['rev_eff_date']}  end={r['end_date']}")
        elif r['source'] == 'ncd':
            print(f"  eff: {r['effective_date']}   term: {r['termination_date']}")
        else:
            print(f"  eff: {r['eff_date']}   end: {r['end_date']}")
        if r.get("mac_jurisdictions"):
            print(f"  MAC: {', '.join(r['mac_jurisdictions'])}")
        if r.get("hcpcs_codes"):
            codes = r["hcpcs_codes"]
            shown = ", ".join(codes[:10])
            more = f" (+{len(codes) - 10} more)" if len(codes) > 10 else ""
            print(f"  HCPCS: {shown}{more}")
        body = r.get("coverage_text") or r.get("description") or ""
        if body:
            print(f"  text: {body}")
        print()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--hcpcs", type=str)
    p.add_argument("--lcd-id", type=str)
    p.add_argument("--ncd-id", type=str)
    p.add_argument("--article-id", type=str)
    p.add_argument("--mac", type=str)
    p.add_argument("--keyword", type=str)
    p.add_argument("--effective-on", type=str)
    p.add_argument("--scope", choices=("current", "all", "both"), default="current")
    p.add_argument("--source", choices=("lcd", "ncd", "article", "all"), default="lcd")
    p.add_argument("--format", choices=("text", "json", "tsv"), default="text")
    p.add_argument("--limit", type=int, default=20)
    args = p.parse_args()

    sources = ["lcd", "ncd", "article"] if args.source == "all" else [args.source]
    scopes = ["current", "all"] if args.scope == "both" else [args.scope]
    when = pd.to_datetime(args.effective_on) if args.effective_on else None

    all_rows: list[dict] = []
    for src in sources:
        scope_iter = scopes if src != "ncd" else ["all"]  # NCD has no scope split
        for scope in scope_iter:
            df = _load(src, scope)
            if df.empty:
                continue
            if args.hcpcs:
                df = filter_by_hcpcs(df, args.hcpcs)
            if args.mac and src != "ncd":
                df = filter_by_mac(df, args.mac)
            if args.lcd_id and src == "lcd":
                df = filter_by_id(df, "lcd", args.lcd_id)
            if args.ncd_id and src == "ncd":
                df = filter_by_id(df, "ncd", args.ncd_id)
            if args.article_id and src == "article":
                df = filter_by_id(df, "article", args.article_id)
            if args.keyword:
                df = filter_by_keyword(df, args.keyword, src)
            if when is not None:
                df = filter_effective_on(df, src, when)
            rows = _summarize(df, src)
            all_rows.extend(rows)

    all_rows = all_rows[:args.limit]

    if args.format == "json":
        print(json.dumps(all_rows, indent=2, default=str))
    elif args.format == "tsv":
        if not all_rows:
            return
        cols = list(all_rows[0].keys())
        print("\t".join(cols))
        for r in all_rows:
            print("\t".join(str(r.get(c, "")) for c in cols))
    else:
        _print_text(all_rows)


if __name__ == "__main__":
    main()
