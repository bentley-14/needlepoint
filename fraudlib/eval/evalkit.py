"""Shared evaluation helpers for signal-check experiments.

Consolidates the ``load_positives`` / ``rank_of`` / ``precision_at_k``
patterns duplicated across ``scripts/backtest.py`` and
``scripts/exp_generalize.py``, and adds a handful of baseline-scoring
utilities used by the experiments under ``scripts/experiments/``.

The goal is a single, well-typed surface that experiment scripts can
import — keeping each experiment's body small enough to read in one
sitting.

Nothing here mutates global state; every function returns fresh
objects.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

DEFAULT_CASES_PATH = Path("data/extracted/gold_set.jsonl")


# ---------------------------------------------------------------------------
# Ground-truth loading
# ---------------------------------------------------------------------------

def load_positives(
    year: int,
    cases_path: Path | str = DEFAULT_CASES_PATH,
    split: str = "test",
) -> dict[str, dict]:
    """Return high-confidence positives active in ``year`` as ``{npi: meta}``.

    Only returns defendants from cases whose ``split`` field matches
    ``split`` (default ``"test"``).  gold_set.jsonl marks every case
    as ``"train"`` or ``"test"``; the 44 test cases are the honest
    held-out eval set that must never be trained on.

    Pass ``split=None`` to return all cases regardless of split (e.g.
    for prospective scoring where no holdout is needed).

    ``meta`` carries ``name``, ``case_source``, ``specialty``, ``state``.
    """
    path = Path(cases_path)
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    out: dict[str, dict] = {}
    for r in rows:
        if split is not None and r.get("split") != split:
            continue
        if year not in r.get("active_years", []):
            continue
        case_id = r.get("case_id", "?")
        for d in r.get("defendants", []):
            if d.get("npi_confidence") != "high":
                continue
            npi = d.get("npi")
            if not npi:
                continue
            surname = d.get("name", "?").split()[-1]
            spec_raw = (d.get("specialty") or "?").split(",")[0].strip()
            state = (d.get("state") or "").strip().upper()
            out[npi] = {
                "name": f"{surname} ({spec_raw.lower()})",
                "case_source": case_id,
                "specialty": spec_raw,
                "state": state,
            }
    return out


def all_case_npis(cases_path: Path | str = DEFAULT_CASES_PATH) -> set[str]:
    """Return every defendant NPI across the case library (any confidence).

    Used by experiments that want to exclude already-known fraud NPIs
    from novel-candidate sets (e.g. the prospective catch arm).
    """
    path = Path(cases_path)
    known: set[str] = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        for d in r.get("defendants", []):
            npi = d.get("npi")
            if npi:
                known.add(str(npi))
    return known


# ---------------------------------------------------------------------------
# Ranking metrics
# ---------------------------------------------------------------------------

def rank_of(
    df: pd.DataFrame,
    score_col: str,
    pos_set: Iterable[int],
    npi_col: str = "Rndrng_NPI",
) -> dict[int, int]:
    """Return ``{npi: rank}`` (1-indexed) under descending ``score_col``.

    Ties resolve in pandas' stable order. ``NaN`` scores sort last so
    they never displace a real positive. Positives not present in ``df``
    are simply absent from the returned dict — callers should check.
    """
    pos = {int(n) for n in pos_set}
    ordered = df.sort_values(score_col, ascending=False, na_position="last").reset_index(drop=True)
    ordered["_rank"] = np.arange(1, len(ordered) + 1)
    hits = ordered[ordered[npi_col].isin(pos)]
    return {int(row[npi_col]): int(row["_rank"]) for _, row in hits.iterrows()}


def precision_at_k(ranks: dict[int, int], k: int, n_positives: int) -> float:
    """Fraction of ``n_positives`` whose rank is ``<= k``."""
    if n_positives <= 0:
        return 0.0
    return sum(1 for r in ranks.values() if r <= k) / n_positives


def recall_at_k(ranks: dict[int, int], k: int, n_positives: int) -> float:
    """Alias of :func:`precision_at_k` for the small-positive regime.

    When positives are rare relative to ``k``, precision@k and recall@k
    share a numerator (hits above rank ``k``); this helper exists so
    callers can name the metric they're actually computing.
    """
    if n_positives <= 0:
        return 0.0
    return sum(1 for r in ranks.values() if r <= k) / n_positives


def top_k_set(
    df: pd.DataFrame,
    score_col: str,
    k: int,
    npi_col: str = "Rndrng_NPI",
) -> set[int]:
    """Return the NPIs in the top ``k`` rows ordered by ``score_col`` desc."""
    top = df.sort_values(score_col, ascending=False, na_position="last").head(k)
    return {int(n) for n in top[npi_col].dropna()}


# ---------------------------------------------------------------------------
# Baseline score columns
# ---------------------------------------------------------------------------

_COMPOSITE_RATIOS: dict[str, tuple[str, str]] = {
    "svcs_per_bene": ("Tot_Srvcs", "Tot_Benes"),
    "pay_per_bene":  ("Tot_Mdcr_Pymt_Amt", "Tot_Benes"),
    "pay_per_svc":   ("Tot_Mdcr_Pymt_Amt", "Tot_Srvcs"),
}
_COMPOSITE_RAW = ("Tot_Mdcr_Pymt_Amt", "Tot_Benes")


def _specialty_state_group(df: pd.DataFrame) -> list[str]:
    cols = [c for c in ("Rndrng_Prvdr_Type", "Rndrng_Prvdr_State_Abrvtn") if c in df.columns]
    return cols


def _zscore_within(df: pd.DataFrame, col: str, group_cols: list[str]) -> pd.Series:
    """Z-score ``col`` within ``group_cols``, NaN-safe. Singleton groups → 0."""
    g = df.groupby(group_cols)[col]
    mu = g.transform("mean")
    sd = g.transform("std")
    z = (df[col] - mu) / sd.replace(0, np.nan)
    return z.fillna(0.0)


def compute_baseline_scores(df: pd.DataFrame, random_seed: int = 0) -> pd.DataFrame:
    """Return ``df`` with four trivial score columns appended.

    Columns added:

    - ``score_dollars`` — raw ``Tot_Mdcr_Pymt_Amt`` (highest-paid wins).
    - ``score_specialty_dollars`` — peer-percentile rank of
      ``Tot_Mdcr_Pymt_Amt`` within ``(Rndrng_Prvdr_Type,
      Rndrng_Prvdr_State_Abrvtn)``. This is the "specialty p99 dollars"
      trivial baseline the brief refers to.
    - ``score_random`` — uniform noise, seeded via ``random_seed``.
    - ``score_composite_z`` — mean z-score across
      ``svcs_per_bene, pay_per_bene, pay_per_svc, Tot_Mdcr_Pymt_Amt,
      Tot_Benes`` within specialty+state. Pure peer anomaly, no rules.

    Pure function — does not mutate ``df``.
    """
    out = df.copy()
    group_cols = _specialty_state_group(out)

    # score_dollars
    out["score_dollars"] = out["Tot_Mdcr_Pymt_Amt"].astype("float64")

    # score_specialty_dollars: within-peer percentile rank
    if group_cols:
        out["score_specialty_dollars"] = (
            out.groupby(group_cols)["Tot_Mdcr_Pymt_Amt"].rank(pct=True)
        )
    else:
        out["score_specialty_dollars"] = out["Tot_Mdcr_Pymt_Amt"].rank(pct=True)

    # score_random (deterministic)
    rng = np.random.default_rng(random_seed)
    out["score_random"] = rng.random(len(out))

    # score_composite_z: build ratios, z-score within peer, mean
    for name, (num, den) in _COMPOSITE_RATIOS.items():
        if num in out.columns and den in out.columns and name not in out.columns:
            out[name] = out[num] / out[den].replace(0, np.nan)

    z_cols = list(_COMPOSITE_RATIOS.keys()) + list(_COMPOSITE_RAW)
    z_cols = [c for c in z_cols if c in out.columns]
    if group_cols and z_cols:
        zs = pd.DataFrame(
            {c: _zscore_within(out, c, group_cols) for c in z_cols},
            index=out.index,
        )
        out["score_composite_z"] = zs.mean(axis=1)
    else:
        out["score_composite_z"] = 0.0

    return out


# ---------------------------------------------------------------------------
# NPI-symmetric holdout
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NpiSymmetricSplit:
    """Three NPI-disjoint frames produced by :func:`npi_symmetric_holdout`."""

    train: pd.DataFrame
    val: pd.DataFrame
    test: pd.DataFrame
    summary: dict


def _npi_in_val(npi: str | int, mod: int) -> bool:
    """Deterministic NPI → val-bucket assignment (SHA-1 first 8 hex digits)."""
    h = int(hashlib.sha1(str(npi).encode()).hexdigest()[:8], 16)
    return (h % mod) == 0


def npi_symmetric_holdout(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    test_npis: Iterable[str | int],
    npi_col: str = "npi",
    val_hash_mod: int = 5,
) -> NpiSymmetricSplit:
    """Split ``train_df`` into NPI-disjoint train/val and drop test NPIs from both.

    The split is symmetric in the sense that ``train``, ``val``, and ``test``
    NPIs are pairwise disjoint:

    1. NPIs in ``test_npis`` are removed from ``train_df`` entirely so the model
       never sees their feature rows under any year.
    2. The remaining rows are bucketed by ``hash(npi) % val_hash_mod``. Rows
       whose NPI hashes to ``0`` go to ``val``; the rest go to ``train``.
       Because the bucketing is deterministic and grouped on NPI, an NPI is
       wholly in train OR wholly in val — never both.

    The pattern was first written inline in
    ``scripts/experiments/grid_search_baseline_2019.py`` (v3 / Option A); this
    helper extracts it so other experiments can reuse the exact same split.

    Parameters
    ----------
    train_df :
        Long frame with an ``npi_col`` column. Rows are typically (NPI, year).
    test_df :
        Test-year frame, returned unchanged. Included so the call site has
        one place to look for the holdout configuration.
    test_npis :
        NPIs that participate in the test set. These are dropped from
        ``train_df`` regardless of which puf_year they sit in.
    npi_col :
        Column name carrying the NPI in both frames. Default ``"npi"``.
    val_hash_mod :
        Hash modulus for the val bucket. Default ``5`` (i.e., 20% val).

    Returns
    -------
    NpiSymmetricSplit
        ``train``, ``val``, ``test`` frames plus a ``summary`` dict with row
        counts, NPI counts, and disjointness assertions.
    """
    if val_hash_mod < 2:
        raise ValueError(f"val_hash_mod must be ≥ 2 (got {val_hash_mod})")
    if npi_col not in train_df.columns:
        raise KeyError(f"train_df missing required column {npi_col!r}")
    if npi_col not in test_df.columns:
        raise KeyError(f"test_df missing required column {npi_col!r}")

    test_npi_set = {str(n) for n in test_npis}

    train_npi_str = train_df[npi_col].astype(str)
    test_holdout_mask = train_npi_str.isin(test_npi_set)
    n_dropped_test = int(test_holdout_mask.sum())
    remaining = train_df.loc[~test_holdout_mask].reset_index(drop=True)

    val_mask = remaining[npi_col].astype(str).map(lambda n: _npi_in_val(n, val_hash_mod))
    train_out = remaining.loc[~val_mask].reset_index(drop=True)
    val_out = remaining.loc[val_mask].reset_index(drop=True)

    train_npis = set(train_out[npi_col].astype(str).unique())
    val_npis = set(val_out[npi_col].astype(str).unique())
    test_df_npis = set(test_df[npi_col].astype(str).unique())

    summary = {
        "n_train_rows": len(train_out),
        "n_val_rows": len(val_out),
        "n_test_rows": len(test_df),
        "n_train_npis": len(train_npis),
        "n_val_npis": len(val_npis),
        "n_test_npis": len(test_df_npis),
        "n_dropped_test_holdout": n_dropped_test,
        "val_hash_mod": val_hash_mod,
        "train_val_overlap": len(train_npis & val_npis),
        "train_test_overlap": len(train_npis & test_df_npis),
        "val_test_overlap": len(val_npis & test_df_npis),
    }
    assert summary["train_val_overlap"] == 0, "train/val NPI overlap — split is broken"
    assert summary["train_test_overlap"] == 0, "train/test NPI overlap — holdout is broken"
    return NpiSymmetricSplit(train=train_out, val=val_out, test=test_df, summary=summary)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def summarize_positives(
    ranks_by_method: dict[str, dict[int, int]],
    positives: dict[str, dict],
    print_to_stdout: bool = True,
) -> pd.DataFrame:
    """Build a comparison table of positive ranks across methods.

    ``ranks_by_method`` maps method label → ``{npi_int: rank}`` (as
    returned by :func:`rank_of`). The returned frame has one row per
    positive and one column per method, plus ``name`` and
    ``case_source``. Missing ranks render as ``"-"`` in the printed
    table but remain ``NaN`` in the returned frame so callers can run
    arithmetic.
    """
    methods = list(ranks_by_method.keys())
    rows = []
    for npi_s, meta in positives.items():
        npi = int(npi_s)
        row: dict[str, object] = {
            "npi": npi_s,
            "name": meta.get("name", "?"),
            "case_source": meta.get("case_source", "?"),
        }
        for m in methods:
            row[m] = ranks_by_method[m].get(npi)
        rows.append(row)
    frame = pd.DataFrame(rows)

    if print_to_stdout:
        header = f"{'NPI':<12} {'defendant':<28} " + " ".join(f"{m:>14}" for m in methods)
        print(header)
        for _, r in frame.iterrows():
            cells = []
            for m in methods:
                v = r[m]
                cells.append(f"{int(v):>14}" if pd.notna(v) else f"{'-':>14}")
            print(f"{str(r['npi']):<12} {str(r['name'])[:28]:<28} " + " ".join(cells))

    return frame
