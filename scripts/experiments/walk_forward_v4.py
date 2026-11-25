"""Walk-forward training: top-5 schemes + ATTACH_EXTRAS + agent filter floor_0.50.

Adapted from walk_forward_full.py (2026-05-16; predecessor retired 2026-06-09 —
see git history). Differences vs that predecessor:

1. **Top-5 schemes only.** Train + score 5 heads (PHANTOM_BILLING,
   MED_UNNECESSARY, KICKBACK, OPIOID, UPCODING). The bottom-5 schemes
   (HHA_HOSPICE, LAB_FRAUD, DME, TELEHEALTH, NPI_THEFT) are dropped from
   training heads — too few positives for a separate XGB head.

2. **Exclude NPIs tagged only with dropped schemes.** A handful of NPIs in
   gold_set are tagged exclusively with bottom-5 schemes; we drop them from
   train/val/score panels entirely (neither positive nor negative).
   NPIs with both top-5 and bottom-5 tags stay (counted as positives via
   their top-5 tags only).

3. **ATTACH_EXTRAS=True.** Adds the full maximal-sane satellite set (cl_*,
   dme_*, dmer_*, op_*, xprog_*, gnn_pca_*, graph_hcpcs_* centrality) on top
   of the base partb_/partd_/hcpcs_ features.

4. **Agent filter floor_0.50.** Pre-training panel filter derived from
   train-year positives only:
     - State exclusion: states with <=2 train positives
     - Specialty exclusion: specialties with 0 train positives
     - Per-specialty payment floor: drop if partb_pay < 0.50 * P10 of
       specialty's train positives
     - Plus partd_tot_clms<500, services-per-bene<1.5, low-HCPCS+zero-opioid
   Preserves ~50% of train positives, drops ~75% of negatives.

5. **Full ensemble.** XGBoost (5 heads) + multitask neural net +
   per-specialty XGB, blended via RRF. Cohort isolation forests OFF.

Usage:
    OMP_NUM_THREADS=6 python scripts/experiments/walk_forward_v4.py
"""
from __future__ import annotations

import gc
import json
import os
import sys
import warnings
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
warnings.filterwarnings("ignore")

from fraudlib.ranker.feature_frames import load_feature_frame, stamp_labels_and_filter
from fraudlib.labels.v4_gold import DEFAULT_V4_PATH
from fraudlib.eval.archetypes import _FRAUD_TYPE_TO_SCHEMES
from scripts.experiments.run_ablation import _feature_matrix
from scripts.experiments.run_tier1_combined import _train_specialty_model
from scripts.experiments.discover_multisource_rules_v2 import (
    SPECIALTY_BUCKETS,
    _bucket_mask,
)

# ── CONFIG ──
ARCHETYPE_MODULE = "schemes"     # base lookup is still 10-scheme
# Top-5 scheme heads (HHA_HOSPICE dropped 2026-05-28 — see SCHEMES_DROPPED).
SCHEMES_TRAIN_FULL = (
    "PHANTOM_BILLING",
    "MED_UNNECESSARY",
    "KICKBACK",
    "OPIOID",
    "UPCODING",
)
SCHEMES_TRAIN_SMALL: tuple[str, ...] = ()   # disabled — satellites hurt R@K
SCHEMES_TRAIN = SCHEMES_TRAIN_FULL + SCHEMES_TRAIN_SMALL
SCHEMES_DROPPED: tuple[str, ...] = (
    "HHA_HOSPICE",
    "DME",
    "TELEHEALTH",
    "LAB_FRAUD",
    "NPI_THEFT",
)
SCHEMES_TOP6 = SCHEMES_TRAIN

# Kept for backward compat; not currently exercised since SCHEMES_TRAIN_SMALL = ()
SMALL_HEAD_K = 30
ARCHETYPES = SCHEMES_TRAIN       # walk_forward loops this for heads
LABEL_PREFIX = "scheme_"

ATTACH_EXTRAS = True             # cl_/dme_/op_ satellite features ON
ACTIVE_ONLY = True
USE_COHORT = False               # isolation forests OFF per user
USE_PERSPECIALTY = False         # per-specialty XGB OFF (locked 2026-05-30): a perspec=0
                                 # sweep beats (3,1,0.3) at every k + removes the cardiology
                                 # top-100 flood. False skips training the arm entirely.
USE_AGENT_FILTER = True          # pre-training panel filter
AGENT_FILTER_NAME = "floor_0.50"   # 2026-05-28: reverted from _rederived → baseline-comparable filter (61.6% drop, matches committed baseline's agentfilter-floor_0.50)

# Module-level vars set by main() from --filter-from / --events-jsonl CLI args.
# When --filter-from is provided, AGENT_FILTER_NAME is overridden to
# "from_json" and _AGENT_FILTER_FROM_PATH points to the JSON file with the
# agent-derived rules (RuleDerivationOutput schema).
_AGENT_FILTER_FROM_PATH: "Path | None" = None
_EVENTS_JSONL_PATH: "Path | None" = None
EARLY_STOPPING_ROUNDS = 50       # bumped from 20 — val 2021 only has ~27 pos,
                                 # AUC-PR is noisy with that few; give the model
                                 # 50 rounds to find signal beyond noise

RULES_PATH = Path("fraudlib/rules/rules_v3.json")

# Ablation toggle: set NO_RULE_FEATURES=1 to exclude rule__* columns from the
# training feature matrix. Labels, y_eval, and the agent filter are UNAFFECTED.
# When active, appends "_norules" to OUT_DIR so baseline results are never overwritten.
NO_RULE_FEATURES: bool = os.environ.get("NO_RULE_FEATURES") == "1"

_OUT_TAG = (
    f"top{len(SCHEMES_TRAIN)}"
    f"_extras-{ATTACH_EXTRAS}"
    f"_agentfilter-{AGENT_FILTER_NAME if USE_AGENT_FILTER else 'off'}"
    f"_perspec-{USE_PERSPECIALTY}"
    f"_es{EARLY_STOPPING_ROUNDS}"
)
OUT_DIR = Path(f"results/walk_forward_v4_{_OUT_TAG}")
RRF_K = 60
# Canonical blend, LOCKED 2026-05-30: (3,1,0) — XGB-heavy + neural; per-specialty DROPPED.
# A perspec=0 weight sweep across 2015-2023 (post-hoc re-blend on the persisted component
# scores) showed: dropping perspec beats (3,1,0.3) at every k (mean R@10k 0.446 vs 0.432,
# R@5k +3.4pp); XGB+neural at ~3:1 beats pure XGB by +5.5pp R@10k (neural is NOT redundant —
# perspec was masking it); and it removes the cardiology top-100 flood. USE_PERSPECIALTY is
# False above, so the per-specialty arm + SPECIALTY_BUCKETS (full import) are inert.
# See WALK_FORWARD_PIPELINE.md §6 / §9.5.
BLEND_W = {"xgb": 3.0, "neural": 1.0, "perspec": 0.0}
NEURAL_SUBSAMPLE = 300_000
MAX_NEG_PER_YEAR = 500_000

CAT_COLS = {"nppes_credential_bucket", "nppes_taxonomy_bucket"}


def _log(msg: str):
    print(msg, flush=True)


def _emit_event(type_: str, payload: dict) -> None:
    """Append a STAGE_ML_TRAINING event to events.jsonl if --events-jsonl was set.

    No-op when _EVENTS_JSONL_PATH is None (script run directly, not via orchestrator).
    """
    if _EVENTS_JSONL_PATH is None:
        return
    import datetime as _dt
    event = {
        "ts": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.") +
              f"{_dt.datetime.utcnow().microsecond // 1000:03d}Z",
        "stage": "ml_training",
        "type": type_,
        "payload": payload,
    }
    try:
        with _EVENTS_JSONL_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception as e:
        _log(f"  [warn] failed to write event to {_EVENTS_JSONL_PATH}: {e}")


# ── Module-level: compute EXCLUDED_NPIS and top-6 NPI map from v4 gold ──

def _compute_excluded_npis() -> tuple[set[str], dict[str, set[str]]]:
    """Walk v4 gold once. Return:
      excluded — NPIs tagged with ≥1 bottom-4 scheme AND 0 top-6 schemes
      npi_to_top6 — {npi: {top-6 schemes it's tagged with}}
    """
    top6 = set(SCHEMES_TOP6)
    dropped = set(SCHEMES_DROPPED)
    npi_to_schemes: dict[str, set[str]] = {}
    for line in DEFAULT_V4_PATH.open():
        r = json.loads(line)
        npi = str(r.get("npi") or r.get("v4_npi") or "").zfill(10)
        if not npi or npi == "0" * 10:
            continue
        for ft in r.get("fraud_types") or []:
            for s in _FRAUD_TYPE_TO_SCHEMES.get(ft.lower().strip(), ["PHANTOM_BILLING"]):
                npi_to_schemes.setdefault(npi, set()).add(s)
    excluded: set[str] = set()
    npi_to_top6: dict[str, set[str]] = {}
    for npi, schemes in npi_to_schemes.items():
        t6 = schemes & top6
        if t6:
            npi_to_top6[npi] = t6
        elif schemes & dropped:
            excluded.add(npi)
    return excluded, npi_to_top6


EXCLUDED_NPIS, NPI_TO_TOP6_SCHEMES = _compute_excluded_npis()


def _label_for(df: pd.DataFrame, head: str) -> np.ndarray:
    """Return binary label vector for ``head``."""
    col = f"{LABEL_PREFIX}{head}"
    if col in df.columns:
        return df[col].astype(int).values
    return (df["archetype"] == head).astype(int).values


# ── Agent filter (per floor_0.50, derived from train positives) ──

def _build_agent_filter(train_df: pd.DataFrame) -> dict:
    """Return {spec_le0, state_le2, spec_payment_floor} from train positives."""
    spec_pos = train_df.groupby("partb_provider_type")["y_eval"].sum()
    spec_le0 = set(spec_pos[spec_pos == 0].index.dropna().astype(str)) - {"nan"}

    state_pos = train_df.groupby("partb_state")["y_eval"].sum()
    state_le2 = set(state_pos[state_pos <= 2].index.dropna().astype(str)) - {"nan"}

    spec_payment_floor: dict[str, float] = {}
    for spec, pos_count in spec_pos.items():
        if pos_count >= 5:
            pos_pay = train_df[
                (train_df["partb_provider_type"] == spec) & (train_df["y_eval"] == 1)
            ]["partb_total_medicare_payment"]
            if len(pos_pay) >= 5:
                spec_payment_floor[str(spec)] = float(np.percentile(pos_pay.fillna(0), 10))
    return {
        "spec_le0": spec_le0,
        "state_le2": state_le2,
        "spec_payment_floor": spec_payment_floor,
    }


def _agent_filter_drop_mask(df: pd.DataFrame, rules: dict) -> pd.Series:
    """Return mask of rows to DROP under the configured AGENT_FILTER_NAME."""
    specs = df["partb_provider_type"].astype(str)
    states = df["partb_state"].astype(str)
    partd_clms = pd.to_numeric(df.get("partd_tot_clms", 0), errors="coerce").fillna(0)
    partb_pay = pd.to_numeric(df["partb_total_medicare_payment"], errors="coerce").fillna(0)
    svcs_per_bene = pd.to_numeric(df.get("partb_svcs_per_bene", 0), errors="coerce").fillna(0)
    benes = pd.to_numeric(df["partb_n_beneficiaries"], errors="coerce").fillna(0)
    hcpcs_n = pd.to_numeric(df.get("partb_n_hcpcs_codes", 0), errors="coerce").fillna(0)
    opi_share = pd.to_numeric(df.get("partd_opioid_clm_share", 0), errors="coerce").fillna(0)

    if AGENT_FILTER_NAME == "floor_0.65":
        # 5 rules — preserve ~65% of train positives, drop ~50% of negatives
        m1 = partd_clms < 200
        m2 = states.isin(rules["state_le2"])
        m3 = (svcs_per_bene < 1.5) & (benes > 0)
        m4 = specs.isin(rules["spec_le0"])
        m5 = pd.Series(False, index=df.index)
        for spec, floor in rules["spec_payment_floor"].items():
            m5 = m5 | ((specs == spec) & (partb_pay < 0.25 * floor))
        return m1 | m2 | m3 | m4 | m5

    elif AGENT_FILTER_NAME == "floor_0.50":
        # 6 rules — preserve ~50% of train positives, drop ~75% of negatives
        m1 = partd_clms < 500
        m2 = states.isin(rules["state_le2"])
        m3 = specs.isin(rules["spec_le0"])
        m4 = pd.Series(False, index=df.index)
        for spec, floor in rules["spec_payment_floor"].items():
            m4 = m4 | ((specs == spec) & (partb_pay < 0.5 * floor))
        m5 = (svcs_per_bene < 1.5) & (benes > 0)
        m6 = (hcpcs_n <= 5) & (opi_share == 0) & (hcpcs_n > 0)
        return m1 | m2 | m3 | m4 | m5 | m6

    elif AGENT_FILTER_NAME == "floor_0.50_rederived":
        # Rederived 2026-05-19 by scripts/rederive_agent_filter.py.
        # 6 rules discovered by greedy threshold search on v9-stamped 2013-2020
        # train data with per-rule pos-loss ≤5%, total pos-loss ≤20%,
        # per-scheme preserve ≥70%. Evaluation on 2022:
        #   drop=35.7%, pos_loss=14.6% (12/82) — vs old floor_0.50: 61.6% drop,
        #   36.6% pos_loss (30/82). Score panel grows from 50 → 70 positives.
        partb_pay_per_bene = pd.to_numeric(df.get("partb_pay_per_bene", 0), errors="coerce").fillna(0)
        m1 = partb_pay < 23_550                       # raw payment floor (drops 20% neg, 3.7% pos in train)
        m2 = partb_pay_per_bene < 128.3              # per-bene payment floor
        m3 = (hcpcs_n < 5) & (hcpcs_n > 0)           # very narrow code scope (carve-out for hcpcs_n=0 NaN guard)
        m4 = (svcs_per_bene < 1.128) & (benes > 0)   # very low service intensity
        m5 = benes < 48                               # tiny panel
        m6 = pd.to_numeric(df.get("partb_n_services", 0), errors="coerce").fillna(0) < 149
        return m1 | m2 | m3 | m4 | m5 | m6

    elif AGENT_FILTER_NAME == "floor_0.50_dmeexempt":
        # Same as floor_0.50 but R1 (partd_clms < 500) is EXEMPTED for providers
        # with substantial DMEPOS / telehealth activity. The 2026-05-19 diagnostic
        # showed R1 was killing 35% of v9 positives at score, including:
        #   DME: 75%, TELEHEALTH: 67%, KICKBACK: 43%, UPCODING: 53%
        # All schemes our newly-engineered dmer_*/xprog_* features target. The
        # exemption restores those positives to the score panel.
        dme_pay = pd.to_numeric(df.get("dme_total_payment", 0), errors="coerce").fillna(0)
        dmer_pct_spec = pd.to_numeric(df.get("dmer_total_pay_pct_spec", 0), errors="coerce").fillna(0)
        brace_mill = pd.to_numeric(df.get("xprog_brace_mill_indicator", 0), errors="coerce").fillna(0)
        tele_pos_share = pd.to_numeric(df.get("hcpcs_tele_pos_share", 0), errors="coerce").fillna(0)

        # A provider is EXEMPT from R1 if ANY of these fire:
        high_dme_or_tele = (
            (dme_pay > 25_000) |              # ≥$25K raw DME orders
            (dmer_pct_spec > 0.90) |          # top-10% DMEPOS within specialty
            (brace_mill == 1) |                # explicit brace-mill (Part B<$10K, DME>$25K)
            (tele_pos_share > 0.30)            # >30% telehealth POS share
        )
        m1 = (partd_clms < 500) & ~high_dme_or_tele

        # R2-R6 unchanged from floor_0.50
        m2 = states.isin(rules["state_le2"])
        m3 = specs.isin(rules["spec_le0"])
        m4 = pd.Series(False, index=df.index)
        for spec, floor in rules["spec_payment_floor"].items():
            m4 = m4 | ((specs == spec) & (partb_pay < 0.5 * floor))
        m5 = (svcs_per_bene < 1.5) & (benes > 0)
        m6 = (hcpcs_n <= 5) & (opi_share == 0) & (hcpcs_n > 0)
        return m1 | m2 | m3 | m4 | m5 | m6

    elif AGENT_FILTER_NAME == "from_json":
        # Agent-derived filter — load rules from the JSON path set by --filter-from.
        # Schema: RuleDerivationOutput with .rules = [{"feature","direction","threshold",...}, ...]
        if _AGENT_FILTER_FROM_PATH is None:
            raise RuntimeError("AGENT_FILTER_NAME='from_json' but no --filter-from path set")
        with _AGENT_FILTER_FROM_PATH.open() as f:
            data = json.load(f)
        mask = pd.Series(False, index=df.index)
        for rule in data.get("rules", []):
            col = rule.get("feature")
            direction = rule.get("direction")
            threshold = rule.get("threshold")
            if not col or col not in df.columns or direction not in ("low", "high") or threshold is None:
                continue
            vals = pd.to_numeric(df[col], errors="coerce").fillna(0)
            if direction == "low":
                mask = mask | (vals < threshold)
            else:
                mask = mask | (vals > threshold)
        return mask

    else:
        raise ValueError(f"Unknown AGENT_FILTER_NAME: {AGENT_FILTER_NAME}")


# ── Loading + label stamping ──

def _build_fraud_npis(years: list[int]) -> set[str]:
    """Top-6 fraud NPIs across train years (for graph features)."""
    from fraudlib.eval.archetypes import scheme_lookup
    npis: set[str] = set()
    for y in years:
        # NPIs the global lookup says are positive in year y...
        lookup = scheme_lookup(y)
        for npi, schemes in lookup.items():
            # ...filtered to those with at least one top-6 scheme
            if schemes & set(SCHEMES_TOP6):
                npis.add(npi)
    return npis


def _load_stamped(year: int, rules: list[dict], fraud_npis: set[str]) -> pd.DataFrame:
    """Load + stamp labels + drop EXCLUDED_NPIS rows. Agent filter applied later."""
    df = load_feature_frame(year, attach_extras=ATTACH_EXTRAS)
    df = stamp_labels_and_filter(
        df, year, rules, fraud_npis=fraud_npis,
        archetype_module=ARCHETYPE_MODULE,
        active_only=ACTIVE_ONLY,
    )

    # Drop NPIs tagged ONLY with bottom-4 schemes
    if EXCLUDED_NPIS:
        before = len(df)
        df = df[~df["npi_padded"].isin(EXCLUDED_NPIS)].reset_index(drop=True)
        n_dropped = before - len(df)
        if n_dropped:
            _log(f"    {year}: dropped {n_dropped} EXCLUDED rows (bottom-4-only NPIs)")

    # Re-stamp y_eval to count ONLY top-6 positives
    # (Default y_eval=1 for ANY scheme; we want top-6 ONLY.)
    n_pos_before = int(df["y_eval"].sum())
    in_top6 = df["npi_padded"].isin(set(NPI_TO_TOP6_SCHEMES.keys()))
    df["y_eval"] = ((df["y_eval"] == 1) & in_top6).astype(int)
    n_pos_after = int(df["y_eval"].sum())
    if n_pos_before != n_pos_after:
        _log(f"    {year}: y_eval re-stamped: {n_pos_before} → {n_pos_after} (top-6 only)")

    for c in CAT_COLS:
        if c in df.columns:
            df[c] = df[c].astype(str)
    # Downcast float64 feature cols → float32 AT LOAD (memory): a full-budget
    # MAX_NEG_PER_YEAR train pool is ~3M rows × 544 cols; at float64 that matrix is ~13.6GB
    # and OOMs a 16GB box, at float32 it is ~6.8GB. XGBoost `hist` (256-bin) and the neural
    # net both operate in float32 internally, so this is lossless for them and sub-noise for
    # ranking. See WALK_FORWARD_PIPELINE.md §9.13.
    _f64 = df.select_dtypes("float64").columns
    if len(_f64):
        df[_f64] = df[_f64].astype(np.float32)
    return df


def run_round(
    train_years: list[int],
    val_year: int,
    score_year: int,
    round_num: int,
    rules: list[dict],
) -> dict:
    out_path = OUT_DIR / f"top2000_{score_year}.csv"
    full_path = OUT_DIR / f"scored_full_{score_year}.parquet"

    _log(f"\n{'='*60}")
    _log(f"ROUND {round_num}: Train {train_years} | Val {val_year} | Score {score_year}")
    _log(f"  Train schemes: {SCHEMES_TRAIN}")
    _log(f"  Dropped:       {SCHEMES_DROPPED}")
    _log(f"  Agent filter:  {AGENT_FILTER_NAME if USE_AGENT_FILTER else 'OFF'}")
    _log(f"  ATTACH_EXTRAS: {ATTACH_EXTRAS}")
    _log(f"  EXCLUDED_NPIS: {len(EXCLUDED_NPIS)}")
    _log(f"{'='*60}")

    fraud_npis = _build_fraud_npis(train_years)
    _log(f"  fraud_npis (top-6, train years): {len(fraud_npis)}")

    _log("  Loading score frame...")
    score_frame = _load_stamped(score_year, rules, fraud_npis)
    score_pos = set(score_frame.loc[score_frame["y_eval"] == 1, "npi_padded"])
    n_pos_panel = int(score_frame["y_eval"].sum())

    _log("  Loading val frame...")
    val_frame = _load_stamped(val_year, rules, fraud_npis)
    val_pos = set(val_frame.loc[val_frame["y_eval"] == 1, "npi_padded"])
    holdout = val_pos | score_pos
    val_df = val_frame[~val_frame["npi_padded"].isin(score_pos)].reset_index(drop=True)
    del val_frame
    gc.collect()

    _log("  Loading train...")
    train_dfs = []
    for y in train_years:
        df = _load_stamped(y, rules, fraud_npis)
        df = df[~df["npi_padded"].isin(holdout)].reset_index(drop=True)
        pos = df[df["y_eval"] == 1]
        neg = df[df["y_eval"] == 0]
        if len(neg) > MAX_NEG_PER_YEAR:
            neg = neg.sample(n=MAX_NEG_PER_YEAR, random_state=42)
        chunk = pd.concat([pos, neg], ignore_index=True)
        _log(f"    {y}: {len(pos)} pos + {len(neg):,} neg = {len(chunk):,}")
        train_dfs.append(chunk)
        del df, pos, neg
        gc.collect()

    train_df = pd.concat(train_dfs, ignore_index=True)
    del train_dfs
    gc.collect()

    # ── Agent filter: build from train, apply to all panels ──
    filter_stats = {}
    if USE_AGENT_FILTER:
        _log(f"\n  Building agent filter ({AGENT_FILTER_NAME})...")
        agent_rules = _build_agent_filter(train_df)
        _log(f"    spec_le0:           {len(agent_rules['spec_le0'])} specialties")
        _log(f"    state_le2:          {len(agent_rules['state_le2'])} states")
        _log(f"    spec_payment_floor: {len(agent_rules['spec_payment_floor'])} specialties")

        for name, frame in [("train", train_df), ("val", val_df), ("score", score_frame)]:
            mask = _agent_filter_drop_mask(frame, agent_rules)
            n_total = len(frame)
            n_drop = int(mask.sum())
            n_pos_lost = int(((frame["y_eval"] == 1) & mask).sum())
            n_pos_kept = int(frame["y_eval"].sum() - n_pos_lost)
            filter_stats[name] = {
                "before": n_total,
                "dropped": n_drop,
                "after": n_total - n_drop,
                "pos_lost": n_pos_lost,
                "pos_kept": n_pos_kept,
                "drop_pct": round(n_drop / n_total, 3),
            }
            _log(f"    {name}: dropped {n_drop:,}/{n_total:,} ({n_drop/n_total:.1%}); "
                 f"pos_lost={n_pos_lost}, pos_kept={n_pos_kept}")

        train_df = train_df[~_agent_filter_drop_mask(train_df, agent_rules)].reset_index(drop=True)
        val_df = val_df[~_agent_filter_drop_mask(val_df, agent_rules)].reset_index(drop=True)
        score_frame = score_frame[~_agent_filter_drop_mask(score_frame, agent_rules)].reset_index(drop=True)
        gc.collect()

    _log(f"\n  Train: {len(train_df):,} ({int(train_df['y_eval'].sum())} pos)")
    _log(f"  Val ({val_year}): {len(val_df):,} ({int(val_df['y_eval'].sum())} pos)")
    _log(f"  Score ({score_year}): {len(score_frame):,} ({int(score_frame['y_eval'].sum())} pos)")

    # ── Feature matrix ──
    X_train_full, feat_cols = _feature_matrix(train_df)
    feat_cols = [c for c in feat_cols if c not in CAT_COLS]
    # Ablation: exclude rule__* features when NO_RULE_FEATURES=1.
    # Labels (y_eval, scheme_*) and the agent filter are derived from separate
    # columns and are NOT affected by this drop.
    if NO_RULE_FEATURES:
        rule_cols_dropped = [c for c in feat_cols if c.startswith("rule__")]
        feat_cols = [c for c in feat_cols if not c.startswith("rule__")]
        _log(f"NO_RULE_FEATURES=1: excluded {len(rule_cols_dropped)} rule__ feature columns; OUT_DIR={OUT_DIR}")
    # .astype(float32) after reindex: fill_value=0 can reintroduce float64 columns for any
    # feature absent from a given year, so re-pin the whole matrix to float32 (see _load_stamped).
    X_train_full = X_train_full.reindex(columns=feat_cols, fill_value=0).astype(np.float32)
    X_val_full, _ = _feature_matrix(val_df)
    X_val_full = X_val_full.reindex(columns=feat_cols, fill_value=0).astype(np.float32)
    _log(f"  Feature matrix: {len(feat_cols)} cols (ATTACH_EXTRAS={ATTACH_EXTRAS})")

    # ── A: Per-scheme XGBoost ──
    # SCHEMES_TRAIN_SMALL heads use feature-selected matrices (K=SMALL_HEAD_K)
    # built from domain seeds + F-score fill; SCHEMES_TRAIN_FULL heads use
    # the full feature matrix.
    _log(f"\n  Training XGBoost ({len(ARCHETYPES)} heads: {ARCHETYPES})...")
    import xgboost as xgb
    from fraudlib.ranker.feature_select import select_features, SMALL_HEAD_SEEDS
    xgb_models: dict[str, dict] = {}   # arch -> {"clf": clf, "feats": list[str] | None}
    for arch in ARCHETYPES:
        y_tr_arch = _label_for(train_df, arch)
        y_vl_arch = _label_for(val_df, arch)
        n_pos_arch = int(y_tr_arch.sum())
        if n_pos_arch < 5:
            _log(f"    {arch:<22} skipped ({n_pos_arch} pos)")
            continue

        # Feature-selected matrix for small heads, full matrix otherwise
        if arch in SCHEMES_TRAIN_SMALL:
            selected = select_features(
                X_train_full, y_tr_arch,
                seeds=SMALL_HEAD_SEEDS.get(arch, []),
                K=SMALL_HEAD_K,
                method="f_classif",
            )
            X_tr_used = X_train_full[selected]
            X_vl_used = X_val_full[selected]
            head_feats = selected
            _log(f"    {arch:<22} feature-selected: {len(selected)} cols "
                 f"(seeds={len([c for c in SMALL_HEAD_SEEDS.get(arch, []) if c in X_train_full.columns])}, "
                 f"f_fill={len(selected) - len([c for c in SMALL_HEAD_SEEDS.get(arch, []) if c in X_train_full.columns])})")
        else:
            X_tr_used = X_train_full
            X_vl_used = X_val_full
            head_feats = None

        scale = ((len(y_tr_arch) - n_pos_arch) / max(n_pos_arch, 1)) * 2.0
        clf = xgb.XGBClassifier(
            n_estimators=300, max_depth=8, learning_rate=0.1,
            min_child_weight=1, subsample=0.8, colsample_bytree=0.8,
            scale_pos_weight=scale, tree_method="hist",
            enable_categorical=True, early_stopping_rounds=EARLY_STOPPING_ROUNDS,
            eval_metric="aucpr", n_jobs=-1, verbosity=0, random_state=0,
        )
        clf.fit(X_tr_used, y_tr_arch, eval_set=[(X_vl_used, y_vl_arch)], verbose=False)
        xgb_models[arch] = {
            "clf": clf, "feats": head_feats,
            "best_iter": int(clf.best_iteration), "best_score": float(clf.best_score),
            "n_pos": int(n_pos_arch),
        }
        _log(f"    {arch:<22} trained (n_pos={n_pos_arch}, best_iter={clf.best_iteration})")
    _log(f"    {len(xgb_models)} models")

    # ── B: Neural ──
    _log("\n  Training Neural...")
    pos_mask = train_df["y_eval"] == 1
    n_neg = min(NEURAL_SUBSAMPLE, int((~pos_mask).sum()))
    neg_idx = train_df[~pos_mask].sample(n=n_neg, random_state=42).index
    sub_idx = pos_mask[pos_mask].index.append(neg_idx).sort_values()

    X_tr_np = X_train_full.iloc[sub_idx].fillna(0).values.astype(np.float32)
    train_sub_labels = train_df.iloc[sub_idx]
    y_tr = {a: _label_for(train_sub_labels, a).astype(np.float32) for a in ARCHETYPES}
    del train_sub_labels

    X_vl_np = X_val_full.fillna(0).values.astype(np.float32)
    y_vl = {a: _label_for(val_df, a).astype(np.float32) for a in ARCHETYPES}

    has_neural = False
    try:
        from fraudlib.ranker.multitask import (
            MultitaskConfig, train_multitask, score_multitask,
        )
        mt_config = MultitaskConfig(
            hidden_dims=(128, 64, 32), dropout=0.3,
            lr=1e-3, batch_size=4096, n_epochs=50, patience=8,
        )
        mt_model = train_multitask(
            X_tr_np, y_tr, X_vl_np, y_vl,
            archetypes=ARCHETYPES, config=mt_config, verbose=False,
        )
        has_neural = True
        _log("    Neural OK")
    except Exception as e:
        _log(f"    Neural FAILED: {e}")
        mt_model = None
    del X_tr_np, y_tr, X_vl_np, y_vl
    gc.collect()

    # ── C: Cohort forests (OFF) ──
    forests = []
    _log("\n  Cohort: SKIPPED (USE_COHORT=False)")

    # ── D: Per-specialty XGBoost ──
    if USE_PERSPECIALTY:
        _log("\n  Training per-specialty rankers...")
        spec_models = {}
        for spec, terms in SPECIALTY_BUCKETS.items():
            m = _train_specialty_model(spec, terms, train_df, val_df, feat_cols)
            if m is not None:
                spec_models[spec] = m
        _log(f"    {len(spec_models)} per-specialty models")
    else:
        spec_models = {}

    del train_df, X_train_full, X_val_full
    gc.collect()

    # ── Score ──
    _log(f"\n  Scoring {score_year}...")
    frame = score_frame
    X_score, _ = _feature_matrix(frame)
    X_score = X_score.reindex(columns=feat_cols, fill_value=0)

    # Score per-head, using each head's own feature subset (small heads
    # restrict to their feature_select output; full heads use X_score as-is).
    # Capture per-head probas (for the head-weighting ablation) and the three
    # per-source scores (so the top-level blend can be re-weighted post-hoc
    # without a re-run). See WALK_FORWARD_PIPELINE.md §7.
    xgb_s = np.zeros(len(X_score), dtype=np.float64)
    head_probas = {}
    for arch, head in xgb_models.items():
        clf = head["clf"]
        feats = head["feats"]
        X_h = X_score[feats] if feats else X_score
        p = clf.predict_proba(X_h)[:, 1]
        head_probas[arch] = p
        xgb_s += p
    rank_x = (-pd.Series(xgb_s)).rank(method="min").values.astype(int)
    rrf = BLEND_W["xgb"] / (RRF_K + rank_x)

    mt_s = np.full(len(frame), np.nan)
    if has_neural:
        X_score_np = X_score.fillna(0).values.astype(np.float32)
        mt_s = score_multitask(mt_model, X_score_np, aggregation="sum")
        rank_m = (-pd.Series(mt_s)).rank(method="min").values.astype(int)
        rrf = rrf + BLEND_W["neural"] / (RRF_K + rank_m)
        del X_score_np

    spec_s = np.full(len(frame), np.nan)
    if spec_models:
        spec_s = np.full(len(frame), -1.0)
        for spec, model in spec_models.items():
            mask = _bucket_mask(frame, SPECIALTY_BUCKETS[spec])
            if mask.any():
                sub_X = X_score.iloc[mask.values]
                spec_s[mask.values] = model.predict_proba(sub_X)[:, 1]
        rank_sp = (-pd.Series(spec_s)).rank(method="min").values.astype(int)
        rrf = rrf + BLEND_W["perspec"] / (RRF_K + rank_sp)

    # ── Recall ──
    y_eval = frame["y_eval"].values
    order = np.argsort(-rrf)
    ranked_y = y_eval[order]

    result = {
        "round": round_num,
        "train_years": train_years,
        "val_year": val_year,
        "score_year": score_year,
        "n_pos_score_panel": n_pos_panel,
        "n_pos_score_filtered": int(y_eval.sum()),
        "has_neural": has_neural,
        "n_xgb_models": len(xgb_models),
        "n_perspecialty": len(spec_models),
        "n_features": len(feat_cols),
        "filter_stats": filter_stats,
        "config": {
            "archetype_module": ARCHETYPE_MODULE,
            "schemes_train": list(SCHEMES_TRAIN),
            "schemes_dropped": list(SCHEMES_DROPPED),
            "attach_extras": ATTACH_EXTRAS,
            "active_only": ACTIVE_ONLY,
            "use_cohort": USE_COHORT,
            "use_perspecialty": USE_PERSPECIALTY,
            "use_agent_filter": USE_AGENT_FILTER,
            "agent_filter_name": AGENT_FILTER_NAME,
        },
    }

    for k in (100, 250, 500, 1000, 2000, 5000, 10000):
        tp = int(ranked_y[:k].sum())
        total = int(y_eval.sum())
        result[f"recall@{k}"] = round(tp / total, 4) if total else 0.0
        result[f"tp@{k}"] = tp

    _log(f"\n  Panel recall (denominator = positives in FILTERED 2022 panel):")
    for k in (100, 500, 1000, 2000, 5000, 10000):
        _log(f"    recall@{k}: {result[f'recall@{k}']:.3f} ({result[f'tp@{k}']}/{int(y_eval.sum())})")

    # Per-scheme recall@10K
    top10k_mask = np.zeros(len(frame), dtype=bool)
    top10k_mask[order[:10000]] = True
    arch_recall = {}
    _log(f"\n  Per-scheme R@10K:")
    for arch in ARCHETYPES:
        col = f"{LABEL_PREFIX}{arch}"
        if col in frame.columns:
            arch_mask = (frame[col] == 1).values
        else:
            arch_mask = (frame["archetype"] == arch).values
        total = int(arch_mask.sum())
        caught = int((arch_mask & top10k_mask).sum())
        if total > 0:
            arch_recall[arch] = {"caught": caught, "total": total, "rate": round(caught / total, 3)}
            _log(f"    {arch:<22} {caught:>3}/{total:>4} = {caught/total:.0%}")
    result["per_scheme_r10k"] = arch_recall

    # ── Save ──
    frame["score_rrf"] = rrf
    # Per-source scores + per-head probas — enable post-hoc top-level reweighting
    # and the head-weighting ablation without re-running (WALK_FORWARD_PIPELINE.md §7).
    frame["score_xgb_sum"] = xgb_s
    frame["score_neural_sum"] = mt_s
    frame["score_perspec"] = spec_s
    for arch, p in head_probas.items():
        frame[f"head__{arch}"] = p
    # Sidecar: per-head training metadata for val-AUPRC / n_pos weighting schemes.
    head_meta = {arch: {"best_iter": h["best_iter"], "best_score": h["best_score"],
                        "n_pos": h["n_pos"]} for arch, h in xgb_models.items()}
    (OUT_DIR / f"components_meta_{score_year}.json").write_text(json.dumps(head_meta, indent=2))
    for c in frame.select_dtypes(include="object").columns:
        frame[c] = frame[c].where(frame[c].notna(), "").astype(str)
    for c in frame.select_dtypes(include="category").columns:
        frame[c] = frame[c].astype(str).astype("category")
    frame.to_parquet(full_path, index=False)
    _log(f"\n  Saved full panel: {full_path.name} ({len(frame):,} rows, +{3 + len(head_probas)} component cols)")

    top2k = frame.sort_values("score_rrf", ascending=False).head(2000)
    top2k.to_csv(out_path, index=False)
    _log(f"  Saved top-2000: {out_path.name} ({int(top2k['y_eval'].sum())} pos in top-2k)")

    del frame, score_frame, top2k, xgb_models
    if has_neural:
        del mt_model
    gc.collect()
    return result


def main():
    global AGENT_FILTER_NAME, _AGENT_FILTER_FROM_PATH, _EVENTS_JSONL_PATH, OUT_DIR
    import argparse

    # ── R2 staging (opt-in; default off — zero behavior change when unset) ──
    _r2_staging = os.environ.get("NP_R2_STAGING") == "1"
    _r2_run_id: "str | None" = None
    if _r2_staging:
        from fraudlib.ranker.r2_working_set import sweep_orphans, stage, manifest as r2_manifest, purge as r2_purge

    ap = argparse.ArgumentParser(description="Walk-forward ML training for the operationalized fraud pipeline")
    ap.add_argument("--score-year", type=int, default=2022,
                    help="Score year (default 2022). Train years = 2013..(score_year-2), val = score_year-1.")
    ap.add_argument("--filter-from", type=Path, default=None,
                    help="Path to agent-derived filter JSON (RuleDerivationOutput schema). "
                         "When provided, overrides AGENT_FILTER_NAME to 'from_json'.")
    ap.add_argument("--events-jsonl", type=Path, default=None,
                    help="Path to events.jsonl (orchestrator's run dir). Emits STAGE_ML_TRAINING events.")
    args = ap.parse_args()

    score_year = args.score_year
    train_years = list(range(2013, score_year - 1))   # e.g., 2023 → [2013..2021]
    val_year = score_year - 1
    rounds = [(train_years, val_year, score_year)]
    round_offset = score_year - 2014  # matches the prior round_offset=8 for 2022

    if args.filter_from is not None:
        if not args.filter_from.exists():
            raise FileNotFoundError(f"--filter-from path does not exist: {args.filter_from}")
        _AGENT_FILTER_FROM_PATH = args.filter_from.resolve()
        AGENT_FILTER_NAME = "from_json"

    if args.events_jsonl is not None:
        _EVENTS_JSONL_PATH = args.events_jsonl.resolve()

    # Recompute OUT_DIR to include the score year so multiple years coexist cleanly
    out_tag_with_year = (
        f"top{len(SCHEMES_TRAIN)}_y{score_year}_extras-{ATTACH_EXTRAS}"
        f"_agentfilter-{AGENT_FILTER_NAME if USE_AGENT_FILTER else 'off'}"
        f"_perspec-{USE_PERSPECIALTY}_es{EARLY_STOPPING_ROUNDS}"
        + ("_norules" if NO_RULE_FEATURES else "")
        + os.environ.get("WF_OUT_SUFFIX", "")
    )
    OUT_DIR = Path(f"results/walk_forward_v4_{out_tag_with_year}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rules = json.loads(RULES_PATH.read_text())["rules"]

    _log(
        f"=== CONFIG: top{len(SCHEMES_TRAIN)} schemes + ATTACH_EXTRAS={ATTACH_EXTRAS} + "
        f"agent_filter={AGENT_FILTER_NAME if USE_AGENT_FILTER else 'OFF'} + "
        f"perspec={USE_PERSPECIALTY} + cohort={USE_COHORT} + score_year={score_year} === "
    )
    if _AGENT_FILTER_FROM_PATH:
        _log(f"  Filter rules: {_AGENT_FILTER_FROM_PATH}")
    _log(f"EXCLUDED_NPIS (bottom-4 only): {len(EXCLUDED_NPIS)}")
    _log(f"NPI_TO_TOP6 (training positives): {len(NPI_TO_TOP6_SCHEMES)}")
    _log(f"Train years: {train_years} | Val: {val_year} | Score: {score_year}")

    # ── Pipeline contract check (drift guard; WALK_FORWARD_PIPELINE.md §1) ──
    # Warns on any drift from fraudlib/pipeline_contract.yaml + writes a per-run
    # manifest into OUT_DIR; hard-errors only on never-deviate invariants.
    from fraudlib.pipeline_contract import verify_pipeline_contract
    try:
        from fraudlib.ranker.feature_frames import CASES_PATH as _CASES_PATH
        _label_src = str(_CASES_PATH)
    except Exception:
        _label_src = "data/extracted/gold_set.jsonl"
    for _w in verify_pipeline_contract({
        "label_source": _label_src,
        "gnn_form": "pca", "gnn_pca_dims": 16,
        "blend_weights": dict(BLEND_W),
        "rrf_k": RRF_K,
        "heads_train_full": list(SCHEMES_TRAIN_FULL),
        "scale_pos_weight": "(n_neg / n_pos) * 2.0",
        "early_stopping_rounds": EARLY_STOPPING_ROUNDS,
        "agent_filter": AGENT_FILTER_NAME if USE_AGENT_FILTER else None,
        "neural_device": "auto",
        "neural_seed": 0,
    }, run_dir=OUT_DIR):
        _log(f"  [contract] {_w}")

    # Emit STAGE_START to events.jsonl if path provided
    if _EVENTS_JSONL_PATH:
        _emit_event(
            "STAGE_START",
            {"score_year": score_year, "agent_filter": AGENT_FILTER_NAME,
             "n_train_years": len(train_years), "out_dir": str(OUT_DIR)},
        )

    # ── R2 staging: sweep orphans + stage the working set before the round loop ──
    # GATED on NP_R2_STAGING=1; default off → zero behavior change on the existing path.
    if _r2_staging:
        sweep_orphans()
        _log("\n=== Staging working set from R2 ===")
        _r2_items = r2_manifest([score_year], attach_extras=ATTACH_EXTRAS)
        _r2_run_id, _r2_fetched = stage(_r2_items)
        _log(f"  run_id={_r2_run_id} | staged {len(_r2_fetched)} new files "
             f"({len(_r2_items) - len(_r2_fetched)} already local)")

    _log("\n=== Checking feature frame caches ===")
    missing = [y for y in range(2013, score_year + 1)
               if not (Path("data/cache/feature_frames") / f"features_full_{y}.parquet").exists()]
    if missing:
        _log(f"  Building missing caches: {missing}")
        for y in missing:
            load_feature_frame(y)
            gc.collect()
    else:
        _log("  All feature frames cached.")

    try:
        all_results = []
        for i, (train_yrs, val_yr, score_yr) in enumerate(rounds):
            result = run_round(train_yrs, val_yr, score_yr, round_offset + i, rules)
            all_results.append(result)
            if _EVENTS_JSONL_PATH:
                _emit_event(
                    "STAGE_PROGRESS",
                    {"pct": 100 * (i + 1) / len(rounds),
                     "msg": f"completed round {i+1}/{len(rounds)} score_year={score_yr}"},
                )

        # Save summary
        summary_path = OUT_DIR / "recall_summary.json"
        summary_path.write_text(json.dumps(all_results, indent=2))
        _log(f"\nSummary -> {summary_path}")

        _log(f"\n{'='*60}")
        _log("FINAL SUMMARY")
        _log(f"{'='*60}")
        for r in all_results:
            _log(f"  Round {r['round']}: score_year={r['score_year']}, "
                 f"pos_in_filtered_panel={r['n_pos_score_filtered']}/{r['n_pos_score_panel']}, "
                 f"R@1k={r['recall@1000']:.3f}, R@5k={r['recall@5000']:.3f}, "
                 f"R@10k={r['recall@10000']:.3f}")

        # Emit STAGE_COMPLETE for the orchestrator/UI
        if _EVENTS_JSONL_PATH and all_results:
            last = all_results[-1]
            _emit_event(
                "STAGE_COMPLETE",
                {
                    "out_dir": str(OUT_DIR),
                    "scored_panel": str(OUT_DIR / f"scored_full_{last['score_year']}.parquet"),
                    "recall_at_10000": last.get("recall@10000"),
                    "n_pos_filtered": last.get("n_pos_score_filtered"),
                    "n_pos_unfiltered": last.get("n_pos_score_panel"),
                },
            )
    finally:
        # Purge the files this run staged (success OR failure); respects NP_KEEP_CACHE=1.
        if _r2_staging and _r2_run_id is not None:
            r2_purge(_r2_run_id)
            _log("  R2 staging purge complete.")


if __name__ == "__main__":
    raise SystemExit(main() or 0)
