#!/usr/bin/env python3
"""Emit per-NPI calibration leak-detection patterns from gold_set.

Runs at SETUP time — OUTSIDE the calibration sandbox, where gold_set is still
readable — so the leak watchdog can flag THIS case's answer facts (settlement
amount/date, the DOJ press-release URL slug, the docket, the defendant/practice
name) without any defendant being hardcoded. This is what makes the calibration
clean-room generalize to any NPI.

The watchdog combines two classes:
  - UNIVERSAL (NPI-independent): gold_set field-name / extraction markers. Their
    appearance in agent output means the agent read gold_set itself (which the
    sandbox should prevent) — a leak for ANY case.
  - PER-NPI (this script): case-specific post-cutoff facts. In a fail-closed
    export these can only surface via model-recall or a sandbox escape.

Usage:
    python3 scripts/calibration_leak_tokens.py --npi 1900000007
    python3 scripts/calibration_leak_tokens.py --npi N --gold /path/gold_set.jsonl

Emits one JSON line: {"npi": "...", "answer": "<ext-regex>", "name": "<ext-regex>"}
- answer: UNIVERSAL + this case's facts. Always a leak (any mode).
- name:   defendant + practice name. A leak ONLY when the name is withheld from
          the agent; in export mode the name is in the subject (public), so the
          watchdog ignores it (CALIB_NAME_WITHHELD=0).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GOLD = REPO / "data" / "extracted" / "gold_set.jsonl"

# NPI-INDEPENDENT. gold_set field names in JSON-KEY form (i.e. "field":) so they
# match an actual gold_set RECORD DUMP, not the same words used as prose. The legal
# agents (counsel/defense) reason in FCA language and discuss "the defendant", the
# "scheme", the damages "amount", active "years" — matching those bare words caused
# repeated false positives. Requiring the quoted-key-colon form keys on a real dump.
# Plus two distinctive extraction MARKERS that never occur in normal prose.
# NOTE: generic FCA terms (qui tam / relator / whistleblower) are deliberately NOT
# here — they are the legal agents' native vocabulary, not case-specific answer facts.
UNIVERSAL = [
    r"FAST_SANITY_PASS", r"SOURCE_CONFIRMED",
    r'"defendant_name":', r'"scheme_summary":', r'"_extraction_confidence":',
    r'"_original_all_active_years":', r'"_original_scheme_summary":',
    r'"all_active_years":', r'"npi_confidence":', r'"audit_id":', r'"v4_match_score":',
    r'"amount_usd":',
]


def _amount_variants(amt) -> list[str]:
    """Comma-formatted amount variants ONLY.

    We deliberately drop two FP-prone forms (observed 2026-06-04):
      - bare digits (`250000`)  — collides with damages estimates / percentile values
      - the `$NNNk` abbreviation — collides with the loop's internal "$250K abandon
        threshold" (counsel_first_chair_review.md), which for this case equals the
        settlement amount and tripped the watchdog.
    The comma forms (`$250,000` / `250,000`) are how settlements are actually
    written and don't collide with those internal constants. A genuine settlement
    recall also surfaces the docket / date / prosecutor / practice tokens, which
    are collision-free — so the amount is a bonus signal, not the load-bearing one.
    """
    try:
        n = int(round(float(amt)))
    except (TypeError, ValueError):
        return []
    if n < 1000:
        return []
    out = [rf"\${n:,}", rf"\b{n:,}\b"]                          # $250,000 / 250,000
    # The million-abbreviation form is emitted ONLY for non-round amounts
    # ($1.5 million / $1.5M). Round whole millions ($1M, $5M...) are loop
    # vocabulary, not case knowledge: counsel's scoring rubric says "the $1M
    # 'strong' threshold", and on 2026-08-14 that matched a subject whose
    # settlement happened to be a round $1,000,000 — a false-positive LEAK halt
    # that killed the queue (CALIBRATION-2113EC3E). Same collision class as the
    # dropped $NNNk form above. The comma forms + docket/title/date tokens
    # remain the load-bearing signals.
    if n >= 1_000_000 and n % 100_000 == 0 and n % 1_000_000 != 0:
        out.append(rf"\${n / 1_000_000:g}\s?(?:million|[mM]\b)")  # $1.5 million / $1.5M
    return out



def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npi", required=True)
    ap.add_argument("--gold", default=str(GOLD))
    args = ap.parse_args(argv)
    npi = args.npi.strip()

    rec = None
    gp = Path(args.gold)
    if gp.exists():
        for line in gp.open():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(d.get("npi") or d.get("v4_npi") or "") == npi:
                rec = d
                break

    answer: list[str] = list(UNIVERSAL)
    name: list[str] = []

    if rec is not None:
        oc = rec.get("outcome") or {}
        answer += _amount_variants(oc.get("amount_usd"))
        dt = oc.get("date")
        if dt:
            answer.append(re.escape(dt))                       # 2025-08-27 (precise; high-signal)
            # NOTE: do NOT emit the bare "<Month> <Year>" form — it is far too
            # broad and FALSE-TRIPS on every run: the case_writer always prints a
            # "good law as of <Month> <Year>" training-cutoff disclaimer, which
            # collides with any settlement in that month/year (observed 2026-06-16,
            # Aspen settled 2025-08-27 → "August 2025" matched the disclaimer,
            # halting the queue on a non-leak). A genuine settlement recall surfaces
            # the ISO date, amount, URL slug, docket, or name — all still tokenized.
        su = rec.get("source_url")
        if su:
            slug = str(su).rstrip("/").split("/")[-1]
            if len(slug) > 8:
                answer.append(re.escape(slug))                 # the DOJ PR URL slug
        for k in ("case_number", "docket"):
            if rec.get(k):
                answer.append(re.escape(str(rec[k])))
        # case_name is the DOJ press-release TITLE — it states the disposition +
        # amount (e.g. "... Agrees to Pay $250,000 to Settle ..."), so it's an
        # ANSWER fact (post-cutoff), and writing it verbatim is a strong recall
        # signal. The DEFENDANT name is a NAME token (public, in the subject).
        cn = rec.get("case_name")
        if cn and len(str(cn)) > 4:
            answer.append(re.escape(str(cn)))
        dn = rec.get("defendant_name")
        if dn:
            name.append(re.escape(str(dn)))
            for p in re.split(r"\s+", str(dn)):
                if len(p) > 2:
                    name.append(re.escape(p))

    def _dedupe(xs):
        seen, out = set(), []
        for x in xs:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    print(json.dumps({
        "npi": npi,
        "answer": "|".join(_dedupe(answer)),
        "name": "|".join(_dedupe(name)),
        "in_gold_set": rec is not None,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
