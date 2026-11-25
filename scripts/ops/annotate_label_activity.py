#!/usr/bin/env python3
"""Annotate entity_labels.jsonl with T-MSIS window-activity (training_usable).

The activity audit (2026-07-16) found ~90/162 labels had NO Medicaid billing in
their predict-before-enforcement window ([enf-3, enf-1] clamped to 2018-2024):
pre-T-MSIS-floor enforcement, pharmacy NCPDP-gap orgs, or Medicare-only orgs.
This stamps each label with `tmsis_window_activity` so the trainer
(train_entity_head.load_enforcement_years) can exclude no-signal labels.

Idempotent + safe: timestamped .bak first, atomic replace, additive field only.
Runs as label-refresh step 7 or standalone.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
LABELS = REPO_ROOT / "data/extracted/entity_labels.jsonl"
TMSIS = REPO_ROOT / "data/raw/medicaid/provider_spending_full.parquet"
PHARMA = ("PHARMACY", "DRUGS", "RX", "APOTHECARY")


def main() -> int:
    if not LABELS.exists():
        print("no entity_labels.jsonl — nothing to annotate")
        return 0
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    shutil.copy2(LABELS, LABELS.with_suffix(f".jsonl.bak_activity_{ts}"))
    rows = [json.loads(l) for l in LABELS.open() if l.strip()]
    con = duckdb.connect()
    ids = ",".join(f"'{r['org_npi']}'" for r in rows if r.get("org_npi"))
    act = con.execute(f"""
        SELECT BILLING_PROVIDER_NPI_NUM npi, SUBSTR(CLAIM_FROM_MONTH,1,4) yr,
               SUM(TOTAL_PAID) pay
        FROM read_parquet('{TMSIS}') WHERE BILLING_PROVIDER_NPI_NUM IN ({ids})
        GROUP BY 1,2""").fetchdf()
    by: dict[str, dict[int, float]] = {}
    for _, r in act.iterrows():
        by.setdefault(r["npi"], {})[int(r["yr"])] = float(r["pay"])
    counts = {"good": 0, "thin": 0, "none": 0}
    for r in rows:
        npi = str(r.get("org_npi") or "")
        disp = r.get("disposition") or {}
        date = disp.get("date") if isinstance(disp, dict) else None
        enf = int(str(date)[:4]) if date else None
        yrs = by.get(npi, {})
        win = [y for y in range(enf - 3, enf) if 2018 <= y <= 2024] if enf else []
        wpay = sum(yrs.get(y, 0) for y in win)
        nyrs = sum(1 for y in win if yrs.get(y, 0) > 0)
        if nyrs and wpay >= 100_000:
            usable = "good"; counts["good"] += 1
        elif nyrs:
            usable = "thin"; counts["thin"] += 1
        else:
            counts["none"] += 1
            nm = str(r.get("defendant_name", "")).upper()
            if enf and enf - 1 < 2018:
                usable = "none_pre_tmsis_floor"
            elif any(p in nm for p in PHARMA):
                usable = "none_pharmacy_ncpdp_gap"
            else:
                usable = "none_no_medicaid_window_activity"
        r["tmsis_window_activity"] = {
            "window_years": win, "active_years": nyrs,
            "window_paid": round(wpay, 0), "training_usable": usable}
    tmp = LABELS.with_suffix(".jsonl.tmp")
    with tmp.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    os.replace(tmp, LABELS)
    print(f"annotated {len(rows)} labels: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
