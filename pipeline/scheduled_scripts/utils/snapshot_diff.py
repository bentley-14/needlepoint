"""Generic snapshot-vs-snapshot diff for ProviderRecordSchema parquets.

Given two parquet files keyed on the same identifier column (default
``npi``), compute:

* npis added (in new, not in previous)
* npis removed (in previous, not in new)
* npis present in both with at least one non-stamp column changed
* per-column drift percentages over the overlap

The output is a JSON-serializable ``dict`` plus a Markdown formatter for
human-friendly reports (GitHub Actions step summaries, Slack posts,
issue comments, etc.).

Designed to be reusable: any source whose canonical parquet has an
``npi`` join key and ``BaseScraperSchema`` stamps can use this. The
stamps (``source``, ``fetched_at_utc``, ``schema_version``) are ignored
when deciding whether an NPI's record "changed" — those always drift
between snapshots by construction.

Implementation
--------------
NPPES has ~9.5M rows x 20 cols. A pandas DataFrame load of both
snapshots blows past 5 GB RSS in pyarrow-backed strings, OOMing the
standard 7 GB GitHub Actions runner. Pandas-with-column-projection
keeps memory low but is too slow (16 set_index passes over 9.5M rows
each). We use DuckDB instead — single SQL query over both parquets,
streaming join + null-aware ``IS DISTINCT FROM`` comparison, peak RSS
under 1 GB on the NPPES files, runtime ~15 s.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq

# Columns that change every run by construction — they're ignored when
# deciding whether an NPI's row "changed".
_IGNORE_COLS: frozenset[str] = frozenset(
    {
        "source",
        "fetched_at_utc",
        "schema_version",
    }
)


def _column_names(path: Path) -> list[str]:
    return list(pq.read_metadata(str(path)).schema.names)


def _quote_ident(name: str) -> str:
    """Quote a SQL identifier safely. Schema column names should never
    contain a double quote, but defensively escape one if it appears."""
    return '"' + name.replace('"', '""') + '"'


def compute_snapshot_diff(
    prev_path: Path,
    new_path: Path,
    *,
    key: str = "npi",
    top_n: int = 10,
) -> dict[str, Any]:
    """Return a structured diff between two snapshot parquets."""
    prev_path = str(prev_path)
    new_path = str(new_path)

    # Row counts come from parquet metadata — no full scan required.
    rows_prev = int(pq.read_metadata(prev_path).num_rows)
    rows_new = int(pq.read_metadata(new_path).num_rows)

    prev_cols = set(_column_names(Path(prev_path)))
    new_cols = set(_column_names(Path(new_path)))
    cols = sorted(
        c for c in new_cols if c in prev_cols and c not in _IGNORE_COLS and c != key
    )

    key_q = _quote_ident(key)

    con = duckdb.connect(":memory:")
    # Cap thread count so we don't OOM the runner — DuckDB defaults to
    # all available cores which is overkill for a single join.
    con.execute("PRAGMA threads=2")

    # Cardinality + adds/removes/in-both in one shot per side.
    npis_added = con.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT {key_q} FROM read_parquet(?) WHERE {key_q} IS NOT NULL
            EXCEPT
            SELECT {key_q} FROM read_parquet(?) WHERE {key_q} IS NOT NULL
        )
        """,
        [new_path, prev_path],
    ).fetchone()[0]

    npis_removed = con.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT {key_q} FROM read_parquet(?) WHERE {key_q} IS NOT NULL
            EXCEPT
            SELECT {key_q} FROM read_parquet(?) WHERE {key_q} IS NOT NULL
        )
        """,
        [prev_path, new_path],
    ).fetchone()[0]

    npis_in_both = con.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT {key_q} FROM read_parquet(?) WHERE {key_q} IS NOT NULL
            INTERSECT
            SELECT {key_q} FROM read_parquet(?) WHERE {key_q} IS NOT NULL
        )
        """,
        [prev_path, new_path],
    ).fetchone()[0]

    # Per-column drift + any-column-changed in one streaming join.
    # IS DISTINCT FROM handles NULLs correctly: NULL IS DISTINCT FROM NULL → FALSE.
    select_exprs: list[str] = []
    any_change_terms: list[str] = []
    for c in cols:
        cq = _quote_ident(c)
        alias = _quote_ident(f"chg_{c}")
        select_exprs.append(
            f"SUM(CASE WHEN n.{cq} IS DISTINCT FROM p.{cq} THEN 1 ELSE 0 END) AS {alias}"
        )
        any_change_terms.append(f"(n.{cq} IS DISTINCT FROM p.{cq})")

    select_exprs.append(
        f"SUM(CASE WHEN ({' OR '.join(any_change_terms)}) THEN 1 ELSE 0 END) AS \"any_change\""
    )

    sql = f"""
        SELECT {', '.join(select_exprs)}
        FROM read_parquet(?) n
        INNER JOIN read_parquet(?) p USING ({key_q})
    """
    row = con.execute(sql, [new_path, prev_path]).fetchone()
    col_names = [d[0] for d in con.description]

    per_column_drift: dict[str, dict[str, Any]] = {}
    npis_with_changes = 0
    for name, val in zip(col_names, row):
        if name == "any_change":
            npis_with_changes = int(val or 0)
            continue
        # Strip the chg_ prefix to recover the original column name.
        original = name[len("chg_") :] if name.startswith("chg_") else name
        n_changed = int(val or 0)
        per_column_drift[original] = {
            "changed": n_changed,
            "pct": round(100 * n_changed / npis_in_both, 4)
            if npis_in_both
            else 0.0,
        }

    top = sorted(
        per_column_drift.items(),
        key=lambda kv: kv[1]["changed"],
        reverse=True,
    )[:top_n]

    return {
        "prev_path": prev_path,
        "new_path": new_path,
        "rows_prev": rows_prev,
        "rows_new": rows_new,
        "npis_added": int(npis_added or 0),
        "npis_removed": int(npis_removed or 0),
        "npis_in_both": int(npis_in_both or 0),
        "npis_with_changes": npis_with_changes,
        "per_column_drift": per_column_drift,
        "top_drift": [{"column": c, **stats} for c, stats in top],
    }


def format_markdown_report(diff: dict[str, Any], title: str = "Snapshot diff") -> str:
    """Render a Markdown summary suitable for $GITHUB_STEP_SUMMARY."""
    tag = diff.get("snapshot_tag", "?")
    if diff.get("first_run"):
        return (
            f"# {title} — {tag}\n\n"
            "First run. No previous snapshot to diff against.\n"
        )

    overlap = max(diff["npis_in_both"], 1)
    changed_pct = 100 * diff["npis_with_changes"] / overlap

    lines = [f"# {title} — {tag}", ""]
    lines += [
        "| Metric | Count |",
        "|---|---:|",
        f"| Rows previous | {diff['rows_prev']:,} |",
        f"| Rows new | {diff['rows_new']:,} |",
        f"| Δ rows | {diff['rows_new'] - diff['rows_prev']:+,} |",
        f"| NPIs added | {diff['npis_added']:,} |",
        f"| NPIs removed | {diff['npis_removed']:,} |",
        f"| NPIs in both | {diff['npis_in_both']:,} |",
        f"| NPIs with ≥1 column changed | {diff['npis_with_changes']:,} ({changed_pct:.2f}%) |",
        "",
        "## Top drifting columns",
        "",
        "| Column | NPIs changed | % of overlap |",
        "|---|---:|---:|",
    ]
    for r in diff["top_drift"]:
        lines.append(f"| `{r['column']}` | {r['changed']:,} | {r['pct']}% |")
    lines.append("")
    return "\n".join(lines)
