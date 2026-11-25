"""Ground-truth loader with temporal scoping and train/test filtering.

This module is the single source of truth for the canonical positive
labels used by the ranker and rule pipelines. It reads
``data/extracted/gold_set.jsonl`` — the audit-cleaned, NPI-centric
canonical labels corpus (1,001 unique NPIs as of 2026-05-28).

Schema adapter: gold_set is NPI-centric (one record per NPI per case);
downstream callers expect case-centric records with nested
``defendants[]``. ``load_cases()`` performs the in-memory grouping so
that call sites (``archetype_lookup``, ``positives_by_split``, the 20+
scripts that import from this module) keep working unchanged.

Policy levers:

1. **Temporal scoping** — restrict positives to those whose
   ``case.active_years`` intersects ``[year - window, year + window]``.
   A 2013 Part B panel cannot be expected to catch a fraudster first
   active in 2020; counting such a miss as "missed" pollutes recall.

2. **Split filtering** — gold_set does NOT carry a ``split`` field
   (it is the canonical "all positives" set). For backward compatibility,
   when a split is requested, all gold positives are returned (treated
   as a single "all" split).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

# Canonical labels source — NPI-centric, audit-cleaned.
CASES_PATH = Path("data/extracted/gold_set.jsonl")

Split = Literal["train", "test"]


def _gold_record_to_defendant(r: dict) -> dict:
    """Convert one gold_set record into the per-defendant shape callers
    expect (nested under ``case.defendants[]``).
    """
    return {
        "npi": r.get("npi") or r.get("v4_npi"),
        "npi_confidence": r.get("npi_confidence") or r.get("v4_npi_confidence", "high"),
        "name": r.get("defendant_name") or "?",
        "specialty": r.get("defendant_specialty") or "?",
        "state": r.get("defendant_state"),
        # Pass through audit_id / specialty / NPPES fields for callers that
        # might want them — harmless for those that don't.
        "audit_id": r.get("audit_id"),
    }


def _gold_to_cases(gold_path: Path) -> list[dict]:
    """Read gold_set.jsonl and group records by ``case_id`` into a list of
    case-centric dicts with nested ``defendants[]``.

    Multiple NPIs in the same case become multiple entries in
    ``case.defendants[]``. ``case.active_years`` is the UNION of the
    member NPIs' ``all_active_years`` (since gold_set carries one
    active_years per NPI but the case-level concept is one set per case).
    """
    cases_by_id: dict[str, dict] = {}
    for line in gold_path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        case_id = r.get("case_id") or "?"
        if case_id not in cases_by_id:
            cases_by_id[case_id] = {
                "case_id": case_id,
                "case_name": r.get("case_name") or "",
                "scheme_summary": r.get("scheme_summary") or "",
                "fraud_types": list(r.get("fraud_types") or []),
                "active_years": set(),
                "payer_lines": list(r.get("payer_lines") or []),
                "outcome": r.get("outcome") or {},
                "source_url": r.get("source_url", ""),
                "defendants": [],
                # gold_set has no split field — preserve None so callers
                # can detect this and treat as "all".
                "split": None,
            }
        cases_by_id[case_id]["active_years"].update(r.get("all_active_years") or [])
        cases_by_id[case_id]["defendants"].append(_gold_record_to_defendant(r))

    # Materialize: convert active_years set→sorted list for stable output
    out = []
    for case_id, case in cases_by_id.items():
        case["active_years"] = sorted(case["active_years"])
        out.append(case)
    return out


def load_cases(cases_path: Path = CASES_PATH) -> list[dict]:
    """Load the canonical labels file (gold_set.jsonl) and return one dict
    per case, with nested ``defendants[]`` so legacy callers keep working.

    The in-memory grouping is cheap (~1,334 NPI records → ~1,100 cases).
    Callers that need the raw NPI-centric records should read
    ``data/extracted/gold_set.jsonl`` directly.
    """
    if not cases_path.exists():
        raise FileNotFoundError(
            f"Labels file not found: {cases_path}. Expected gold_set.jsonl."
        )
    return _gold_to_cases(cases_path)


def _active_in_window(case: dict, year: int, window: int) -> bool:
    ay = case.get("active_years") or []
    lo, hi = year - window, year + window
    return any(lo <= y <= hi for y in ay)


def load_positives_scoped(
    year: int,
    window: int = 3,
    split: Split | None = None,
    min_confidence: str = "high",
    cases_path: Path = CASES_PATH,
) -> dict[str, dict]:
    """Return ``{npi_str: {"name", "specialty", "case_id", "split", "active_years", "fraud_types"}}``.

    Filter rules:
    - ``case.active_years ∩ [year-window, year+window] != ∅``
    - gold_set has no ``split`` field; the ``split`` argument is preserved
      for API compatibility but has no effect (all gold positives are
      returned regardless of split). If a caller passes split="train",
      they get the same set they would for split="test".
    - ``defendant.npi_confidence == min_confidence`` and ``defendant.npi`` set

    The npi_confidence default of "high" matches prior loaders; pass
    "any" to include medium/low as well.
    """
    cases = load_cases(cases_path)
    out: dict[str, dict] = {}
    for r in cases:
        if not _active_in_window(r, year, window):
            continue
        # gold_set has no split field — split filter is a no-op.
        # (Was: ``if split is not None and r.get("split") != split: continue``)
        for d in r.get("defendants", []):
            conf = d.get("npi_confidence")
            if min_confidence == "high" and conf != "high":
                continue
            if min_confidence == "any" and conf not in {"high", "medium", "low"}:
                continue
            npi = d.get("npi")
            if not npi:
                continue
            name = (d.get("name") or "?").split()[-1]
            spec = (d.get("specialty") or "?").split(",")[0].strip()
            out[str(npi)] = {
                "name": name,
                "specialty": spec,
                "case_id": r.get("case_id", "?"),
                "split": r.get("split"),  # always None for gold_set
                "active_years": list(r.get("active_years") or []),
                "fraud_types": list(r.get("fraud_types") or []),
            }
    return out


def positives_by_split(
    year: int,
    window: int = 3,
    min_confidence: str = "high",
    cases_path: Path = CASES_PATH,
) -> dict[str, dict[str, dict]]:
    """Convenience: return ``{"train": {...}, "test": {...}}`` in one pass.

    **Behavior change (2026-05-27)**: gold_set.jsonl has no ``split`` field,
    so this function now returns the SAME set under both "train" and "test".
    The legacy split semantics (where DOJ API cases were held out as a
    novel-catch signal) no longer apply — gold_set is the audited "all
    positives" set and the split-based novel-catch evaluation was retired.
    """
    all_positives = load_positives_scoped(year, window, None, min_confidence, cases_path)
    return {"train": all_positives, "test": all_positives}
