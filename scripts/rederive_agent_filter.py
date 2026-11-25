"""Rederive the agent-filter rules from v9 labels + current features.

The existing ``floor_0.50`` agent filter (6 hand-tuned rules in
``walk_forward_v4.py::_agent_filter_drop_mask``) was designed before
the v9 label correction and before we built the ``dmer_*``/``xprog_*``
features. The diagnostic showed:
  - R1 (partd_clms<500) kills 35% of v9 positives
  - R2 (state ≤2 pos) kills 0
  - R3 (spec 0 pos) kills 0
  - R4 (pay floor) kills 3.7%
  - R5 (svcs/bene<1.5) kills 2.4%
  - R6 (≤5 HCPCS no opioid) kills 6.1%
  → combined 36.6% positive loss, 61.5% negative drop

Goal: discover a NEW rule set that hits similar negative-drop with much
lower positive loss, using the current features (including dmer_*, xprog_*,
hcpcs_rich).

Algorithm
---------
1. Load 2013-2020 train frames (v9 stamped). Build candidate-feature list.
2. For each candidate feature F + direction (drop-low or drop-high), search
   over threshold values from the negative-side distribution. For each
   threshold T, compute:
     - drop_rate = fraction of negatives where rule fires
     - pos_loss  = fraction of positives where rule fires
   Pick the T* that maximizes ``drop_rate / (pos_loss + epsilon)`` subject
   to ``pos_loss ≤ MAX_RULE_POS_LOSS`` (default 5%).
3. Greedy combine: start with no rules, add the rule that drops the most
   *remaining* negatives without exceeding cumulative ``MAX_TOTAL_POS_LOSS``
   (default 20%) AND no single scheme dropping below ``MIN_SCHEME_PRESERVE``
   (default 70%).
4. Evaluate the final ruleset on 2022 score panel; compare to existing
   ``floor_0.50`` and ``floor_0.50_dmeexempt``.

Output: discovered ruleset JSON + comparison report.

Usage:
    python scripts/rederive_agent_filter.py [--max-pos-loss 0.20] [--min-scheme-preserve 0.70]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fraudlib.ranker.feature_frames import load_feature_frame, stamp_labels_and_filter
from fraudlib.eval.archetypes import scheme_lookup

TRAIN_YEARS = [2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020]
SCORE_YEAR = 2022
SCHEMES = ("PHANTOM_BILLING", "MED_UNNECESSARY", "KICKBACK", "OPIOID",
           "UPCODING", "HHA_HOSPICE", "DME", "TELEHEALTH", "LAB_FRAUD", "NPI_THEFT")

# Candidate features for filter rules. Each entry: (column, direction).
# direction = "low" → drop rows where col < T; "high" → drop rows where col > T.
# Most volume/intensity filters drop the LOW side (kick out small fish).
CANDIDATE_FEATURES = [
    # Part B volume — drop providers with very little Medicare billing
    ("partb_total_medicare_payment", "low"),
    ("partb_n_beneficiaries", "low"),
    ("partb_n_services", "low"),
    ("partb_n_hcpcs_codes", "low"),
    # Part D volume — drop providers with very little prescribing (but
    # carefully — DMEPOS-mill recipients have low Part D legitimately)
    ("partd_tot_clms", "low"),
    ("partd_total_drug_cost", "low"),
    # DMEPOS volume — small DMEPOS orderers
    ("dme_total_payment", "low"),
    ("dme_total_benes", "low"),
    # Service-intensity — drop providers with near-zero intensity
    ("partb_svcs_per_bene", "low"),
    ("partb_pay_per_bene", "low"),
    # Bauder concentration — drop providers with extreme code-concentration on
    # the LOW side (single-code providers with no risk pattern); leave high
    # concentration alone (those are interesting)
    ("bauder_code_entropy", "high"),  # very high entropy = generalist, low fraud risk
]


def _load_train_v9_panel() -> pd.DataFrame:
    """Load + stamp 2013-2020 train frames with v9 labels, concatenate."""
    print(f"Loading {len(TRAIN_YEARS)} train years with v9 labels...")
    rules_path = Path("fraudlib/rules/rules_v3.json")
    rules = json.loads(rules_path.read_text())["rules"]

    # Build fraud_npis (top-6, train years) for graph features — same as walk_forward
    TOP6 = {"PHANTOM_BILLING", "MED_UNNECESSARY", "KICKBACK", "OPIOID", "UPCODING", "HHA_HOSPICE"}
    fraud_npis = set()
    for y in TRAIN_YEARS:
        lookup = scheme_lookup(y)
        for npi, schemes in lookup.items():
            if schemes & TOP6:
                fraud_npis.add(npi)
    print(f"  fraud_npis (top-6, train years): {len(fraud_npis):,}")

    frames = []
    for y in TRAIN_YEARS:
        t0 = time.time()
        df = load_feature_frame(y, attach_extras=True)
        df = stamp_labels_and_filter(df, y, rules, fraud_npis=fraud_npis,
                                      archetype_module="schemes", active_only=True)
        # Keep only cols we need: candidate features, y_eval, scheme_* labels
        keep = ["y_eval"] + [c for (c, _) in CANDIDATE_FEATURES if c in df.columns]
        keep += [f"scheme_{s}" for s in SCHEMES if f"scheme_{s}" in df.columns]
        df_sub = df[keep].copy()
        df_sub["puf_year"] = y
        frames.append(df_sub)
        del df
        print(f"  {y}: stamped {len(df_sub):,} rows, {int(df_sub['y_eval'].sum())} pos "
              f"({time.time()-t0:.1f}s)")

    full = pd.concat(frames, ignore_index=True)
    print(f"  combined: {len(full):,} rows, {int(full['y_eval'].sum())} positives")
    return full


def _best_threshold(df: pd.DataFrame, col: str, direction: str,
                    max_pos_loss: float = 0.05) -> tuple[float, float, float] | None:
    """Find threshold T* maximizing ``drop / (pos_loss + 1e-6)`` s.t. ``pos_loss ≤ max_pos_loss``.

    Returns ``(T, drop_rate, pos_loss)`` or ``None`` if no valid threshold exists.
    """
    if col not in df.columns:
        return None
    vals = pd.to_numeric(df[col], errors="coerce")
    if vals.isna().all():
        return None

    # Candidate thresholds: percentile grid over the *negative* distribution
    # (so we're searching the values that would actually drop negatives).
    neg_vals = vals[df["y_eval"] == 0]
    pct_grid = [5, 10, 20, 30, 40, 50, 60, 70, 80]
    candidates = np.percentile(neg_vals.dropna(), pct_grid)
    # Dedupe and sort
    candidates = np.unique(candidates)

    y = df["y_eval"].values
    n_neg = int((y == 0).sum())
    n_pos = int((y == 1).sum())

    best = None
    for T in candidates:
        if direction == "low":
            mask = (vals < T).fillna(False).values  # NaN doesn't fire
        else:  # "high"
            mask = (vals > T).fillna(False).values
        n_drop = int(mask.sum())
        n_drop_pos = int((mask & (y == 1)).sum())
        drop_rate = n_drop / max(n_neg, 1)  # fraction of negatives dropped
        pos_loss = n_drop_pos / max(n_pos, 1)
        if pos_loss > max_pos_loss:
            continue
        # Efficiency = negatives dropped per positive lost (or just drop_rate if no pos lost)
        if pos_loss < 1e-6:
            efficiency = drop_rate * 1000  # essentially infinite — heavily favor zero-loss rules
        else:
            efficiency = drop_rate / pos_loss
        if best is None or efficiency > best[3]:
            best = (T, drop_rate, pos_loss, efficiency)
    if best is None:
        return None
    return (best[0], best[1], best[2])


def _greedy_combine(df: pd.DataFrame, candidate_rules: list[dict],
                    max_total_pos_loss: float = 0.20,
                    min_scheme_preserve: float = 0.70) -> list[dict]:
    """Greedy: pick rule that drops most *remaining* negatives without violating constraints.

    Returns list of chosen rules with applied thresholds, in order added.
    """
    y = df["y_eval"].values
    pos_mask = (y == 1)
    n_pos_total = int(pos_mask.sum())

    # Track which rows are still "alive" (not yet dropped by any picked rule)
    alive = np.ones(len(df), dtype=bool)
    chosen = []
    cumulative_pos_loss = 0.0

    # Per-scheme tracking
    scheme_pos_total = {}
    scheme_pos_alive = {}
    for s in SCHEMES:
        col = f"scheme_{s}"
        if col in df.columns:
            scheme_pos_total[s] = int((df[col].values == 1).sum())
            scheme_pos_alive[s] = scheme_pos_total[s]

    while True:
        best_rule = None
        best_drop = 0
        for rule in candidate_rules:
            if any(c["col"] == rule["col"] and c["direction"] == rule["direction"] for c in chosen):
                continue
            # Apply rule to alive rows
            vals = pd.to_numeric(df[rule["col"]], errors="coerce")
            if rule["direction"] == "low":
                fires = (vals < rule["T"]).fillna(False).values
            else:
                fires = (vals > rule["T"]).fillna(False).values
            fires_alive = fires & alive
            # Pos loss in alive rows
            new_pos_loss = int((fires_alive & pos_mask).sum())
            new_drop = int(fires_alive.sum())
            new_neg_drop = new_drop - new_pos_loss
            # Total cumulative pos loss after adding this rule
            total_pos_lost = int(((~alive | fires) & pos_mask).sum())
            new_cum_pos_loss = total_pos_lost / max(n_pos_total, 1)
            if new_cum_pos_loss > max_total_pos_loss:
                continue
            # Per-scheme check
            scheme_ok = True
            for s in SCHEMES:
                col = f"scheme_{s}"
                if col not in df.columns or scheme_pos_total.get(s, 0) == 0:
                    continue
                s_mask = (df[col].values == 1)
                s_killed = int((fires_alive & s_mask).sum())
                s_remaining = scheme_pos_alive[s] - s_killed
                if scheme_pos_total[s] > 0 and (s_remaining / scheme_pos_total[s]) < min_scheme_preserve:
                    scheme_ok = False
                    break
            if not scheme_ok:
                continue
            if new_neg_drop > best_drop:
                best_drop = new_neg_drop
                best_rule = {
                    **rule,
                    "fires_alive": fires_alive,
                    "neg_drop": new_neg_drop,
                    "pos_loss": new_pos_loss,
                    "cum_pos_loss": new_cum_pos_loss,
                }
        if best_rule is None:
            break
        # Lock in
        alive = alive & ~best_rule["fires_alive"]
        for s in SCHEMES:
            col = f"scheme_{s}"
            if col in df.columns:
                s_mask = (df[col].values == 1)
                s_killed = int((best_rule["fires_alive"] & s_mask).sum())
                scheme_pos_alive[s] -= s_killed
        chosen.append({
            "col": best_rule["col"],
            "direction": best_rule["direction"],
            "T": float(best_rule["T"]),
            "neg_dropped": int(best_rule["neg_drop"]),
            "pos_lost": int(best_rule["pos_loss"]),
            "cum_pos_loss": float(best_rule["cum_pos_loss"]),
        })
        print(f"  + {best_rule['col']} {('< ' if best_rule['direction'] == 'low' else '> ')}"
              f"{best_rule['T']:.4g}: drops {best_rule['neg_drop']:,} neg, "
              f"loses {best_rule['pos_loss']} pos (cum: {best_rule['cum_pos_loss']*100:.1f}%)")
    return chosen


def _evaluate_on_score(score_df: pd.DataFrame, ruleset: list[dict]) -> dict:
    """Apply discovered ruleset to 2022 score frame, report performance."""
    y = score_df["y_eval"].values
    n_total = len(score_df)
    n_pos = int((y == 1).sum())
    n_neg = n_total - n_pos

    dropped = np.zeros(len(score_df), dtype=bool)
    per_rule = []
    for rule in ruleset:
        col = rule["col"]
        if col not in score_df.columns:
            per_rule.append({**rule, "fires": 0, "kills_pos": 0})
            continue
        vals = pd.to_numeric(score_df[col], errors="coerce")
        if rule["direction"] == "low":
            fires = (vals < rule["T"]).fillna(False).values
        else:
            fires = (vals > rule["T"]).fillna(False).values
        kills_pos = int((fires & (y == 1)).sum())
        per_rule.append({**rule, "fires": int(fires.sum()), "kills_pos": kills_pos})
        dropped = dropped | fires

    n_drop = int(dropped.sum())
    n_drop_pos = int((dropped & (y == 1)).sum())

    # Per-scheme breakdown
    per_scheme = {}
    for s in SCHEMES:
        col = f"scheme_{s}"
        if col in score_df.columns:
            s_mask = (score_df[col].values == 1)
            s_total = int(s_mask.sum())
            s_lost = int((dropped & s_mask).sum())
            if s_total > 0:
                per_scheme[s] = {
                    "total": s_total,
                    "lost": s_lost,
                    "preserved_pct": round(100 * (s_total - s_lost) / s_total, 1),
                }
    return {
        "n_total": n_total,
        "n_pos": n_pos,
        "n_neg": n_neg,
        "n_dropped": n_drop,
        "drop_pct": round(100 * n_drop / n_total, 1),
        "neg_drop_pct": round(100 * (n_drop - n_drop_pos) / max(n_neg, 1), 1),
        "pos_loss_pct": round(100 * n_drop_pos / max(n_pos, 1), 1),
        "n_pos_preserved": n_pos - n_drop_pos,
        "per_rule": per_rule,
        "per_scheme": per_scheme,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pos-loss", type=float, default=0.20,
                    help="Total positive-loss budget (default 0.20 = 20%)")
    ap.add_argument("--min-scheme-preserve", type=float, default=0.70,
                    help="Min per-scheme positive preservation (default 0.70)")
    ap.add_argument("--rule-pos-loss", type=float, default=0.05,
                    help="Per-rule positive-loss budget (default 0.05 = 5%)")
    args = ap.parse_args()

    # ── PHASE 1: Train ──
    train_df = _load_train_v9_panel()
    n_train_pos = int(train_df["y_eval"].sum())
    n_train_neg = len(train_df) - n_train_pos
    print(f"\nTrain: {len(train_df):,} rows, {n_train_pos} pos, {n_train_neg:,} neg")

    # ── PHASE 2: Per-feature threshold search ──
    print("\n=== Per-feature threshold search (max per-rule pos loss 5%) ===")
    candidate_rules = []
    for col, direction in CANDIDATE_FEATURES:
        result = _best_threshold(train_df, col, direction, max_pos_loss=args.rule_pos_loss)
        if result is None:
            print(f"  {col} {direction}: no valid threshold")
            continue
        T, drop_rate, pos_loss = result
        candidate_rules.append({
            "col": col,
            "direction": direction,
            "T": float(T),
            "drop_rate": float(drop_rate),
            "pos_loss": float(pos_loss),
        })
        print(f"  {col} {direction:>4}: T={T:.4g}, drops {drop_rate*100:.1f}% neg, "
              f"loses {pos_loss*100:.1f}% pos")

    # ── PHASE 3: Greedy combine ──
    print(f"\n=== Greedy combination "
          f"(max total pos loss {args.max_pos_loss*100:.0f}%, "
          f"min scheme preserve {args.min_scheme_preserve*100:.0f}%) ===")
    chosen = _greedy_combine(train_df, candidate_rules,
                              max_total_pos_loss=args.max_pos_loss,
                              min_scheme_preserve=args.min_scheme_preserve)
    print(f"\nChosen {len(chosen)} rules:")
    for r in chosen:
        print(f"  {r['col']} {('< ' if r['direction'] == 'low' else '> ')}{r['T']:.4g}")

    # ── PHASE 4: Evaluate on 2022 score frame ──
    print(f"\n=== Loading score year {SCORE_YEAR} for evaluation ===")
    rules_path = Path("fraudlib/rules/rules_v3.json")
    rules = json.loads(rules_path.read_text())["rules"]
    TOP6 = {"PHANTOM_BILLING", "MED_UNNECESSARY", "KICKBACK", "OPIOID", "UPCODING", "HHA_HOSPICE"}
    fraud_npis = set()
    for y in TRAIN_YEARS:
        lookup = scheme_lookup(y)
        for npi, sch in lookup.items():
            if sch & TOP6:
                fraud_npis.add(npi)
    score_df = load_feature_frame(SCORE_YEAR, attach_extras=True)
    score_df = stamp_labels_and_filter(score_df, SCORE_YEAR, rules, fraud_npis=fraud_npis,
                                        archetype_module="schemes", active_only=True)
    print(f"  {SCORE_YEAR}: {len(score_df):,} rows, {int(score_df['y_eval'].sum())} pos")

    eval_new = _evaluate_on_score(score_df, chosen)

    print("\n=== Discovered ruleset on 2022 score frame ===")
    print(f"  Rows: {eval_new['n_total']:,} ({eval_new['n_pos']} pos)")
    print(f"  Dropped: {eval_new['n_dropped']:,} ({eval_new['drop_pct']}% of all rows, "
          f"{eval_new['neg_drop_pct']}% of negatives)")
    print(f"  Positive loss: {eval_new['pos_loss_pct']}% "
          f"({eval_new['n_pos'] - eval_new['n_pos_preserved']}/{eval_new['n_pos']} lost)")
    print(f"\n  Per-scheme preservation:")
    for s, stats in eval_new["per_scheme"].items():
        print(f"    {s:<22} {stats['total']-stats['lost']}/{stats['total']} "
              f"= {stats['preserved_pct']}% preserved")

    # ── PHASE 5: Compare against existing filters ──
    print("\n=== Comparison: existing floor_0.50 ===")
    # Reproduce existing floor_0.50 thresholds from train
    spec_pos = train_df.groupby(train_df.index)["y_eval"].sum()  # placeholder; we don't have provider_type in subset
    # For an apples comparison, we'd need to re-load with provider_type/state/partb_n_hcpcs_codes/etc.
    # Skip this for now — the diagnostic already gave us the exact floor_0.50 numbers.

    print(f"  floor_0.50 (from prior diagnostic):")
    print(f"    drop_pct=61.6%, neg_drop_pct=~62%, pos_loss=36.6% (30/82 lost)")
    print(f"\n  NEW discovered ruleset:")
    print(f"    drop_pct={eval_new['drop_pct']}%, neg_drop_pct={eval_new['neg_drop_pct']}%, "
          f"pos_loss={eval_new['pos_loss_pct']}%")

    # ── Save artifacts ──
    out = {
        "config": {
            "max_pos_loss": args.max_pos_loss,
            "min_scheme_preserve": args.min_scheme_preserve,
            "rule_pos_loss": args.rule_pos_loss,
        },
        "candidate_rules": candidate_rules,
        "chosen_rules": chosen,
        "evaluation_2022": eval_new,
    }
    out_path = Path("results/rederived_agent_filter.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
