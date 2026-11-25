"""Per-head feature selection for low-positive-count scheme heads.

The walk-forward training pipeline trains XGBoost heads per fraud scheme.
For schemes with hundreds of training positives (PHANTOM, MED_UNNEC) the
full 532-column feature matrix works fine. For small heads (DME ~55
positives, TELEHEALTH ~52 positives) the events-per-variable (EPV) ratio
drops below 0.15 and XGBoost early-stops within 1-5 boosting rounds — the
head learns essentially nothing beyond noise.

This module implements the **hybrid seed + statistical fill** approach
chosen 2026-05-19 from a 4-option menu (option 4):

  1. **Domain seeds**: a hand-curated list of features known to fire on the
     target scheme (e.g. ``dmer_*`` + ``xprog_*`` for DME, ``hcpcs_tele_*``
     + off-specialty share for TELEHEALTH).
  2. **Statistical fill**: ANOVA F-statistic against the head's binary
     label, used to backfill the seed list up to ``K`` features.

This is preferable to either extreme:
  - **Pure univariate (Option 1)**: ignores feature interactions, may
    deprioritize our purpose-built ``dmer_*`` features if their solo
    F-score is weak (combinatorial signal).
  - **Pooled importance (Option 2)**: top-K from a "any positive" pool
    is dominated by PHANTOM/MED_UNNEC importances since they make up the
    majority of positives.

API contract
------------
``select_features(X, y, seeds, K=30, method="f_classif")`` returns an
ordered list of column names: seeds first (filtered to those in
``X.columns``), then F-score fill up to ``K``. If seeds already meet/exceed
K, returns the seeds as-is (truncated to K).

Performance notes
-----------------
``f_classif`` (ANOVA F-statistic) is used over ``mutual_info_classif``
because:
  - mutual_info uses k-NN entropy estimators → O(n²) in worst case
  - At ~1.5M train rows × 500 features × 8 train years this would take
    20+ minutes per head; F-score is closed-form, ~5 seconds.
  - For continuous-ish features with binary labels, F-score's
    assumptions are mild and the ranking is usually similar to mutual
    info on the top-K window.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ── Seed lists (domain-curated per scheme) ──────────────────────────────
#
# Each entry is a feature column name. Order doesn't matter — they're all
# guaranteed to appear in the final selection (assuming present in X).
#
# DME seeds: the 32 dmer_* and 5 xprog_* features we built specifically
# for DMEPOS-mill / brace-mill detection, plus base cross-program totals.
# Total ~27 seeds. The hybrid approach reserves room for the F-score fill
# to surface any non-obvious signal we missed.

DME_SEEDS = [
    # DMEPOS rich — within-specialty percentiles (catch off-distribution outliers)
    "dmer_total_pay_pct_spec",
    "dmer_pay_per_bene_pct_spec",
    "dmer_dme_share_pct_spec",
    "dmer_n_suppliers_pct_spec_rev",
    "dmer_clms_per_supplier_pct_spec",
    "dmer_sbmtd_inflation_pct_spec",
    # DMEPOS rich — category mix (brace-mill signature)
    "dmer_dme_pay_share",
    "dmer_category_dominance",
    "dmer_pos_pay_share",
    "dmer_drug_pay_share",
    # DMEPOS rich — supplier concentration (captive-supplier signal)
    "dmer_n_suppliers",
    "dmer_clms_per_supplier",
    "dmer_pay_per_supplier",
    "dmer_single_supplier_flag",
    "dmer_few_suppliers_flag",
    # DMEPOS rich — DME-category specifics
    "dmer_dme_clms_per_supplier",
    "dmer_dme_pay_per_supplier",
    "dmer_dme_pay_per_bene",
    "dmer_dme_sbmtd_inflation",
    "dmer_dme_sprsn",
    # DMEPOS rich — inflation / bene profile
    "dmer_sbmtd_inflation",
    "dmer_dual_share",
    "dmer_avg_risk_scre",
    # Cross-program ratios (the brace-mill ratio is the textbook signal)
    "xprog_dme_to_partb_ratio",
    "xprog_dme_to_partb_log",
    "xprog_dme_bene_to_partb_bene",
    "xprog_dme_pay_dominance",
    "xprog_brace_mill_indicator",
]

# TELEHEALTH seeds: direct telehealth fingerprint columns plus off-specialty
# share (genetic-testing rings cover their tracks with telehealth shells).
TELEHEALTH_SEEDS = [
    # Telehealth fingerprint (direct)
    "hcpcs_tele_pos_share",                       # POS code 02 share
    "hcpcs_tele_specific_hcpcs_share",            # tele-only HCPCS share
    "hcpcs_tele_pos_share_pct",                   # within-specialty percentile
    "hcpcs_tele_specific_hcpcs_share_pct",
    "hcpcs_tele_pos_share_global_pct",            # global percentile
    "hcpcs_tele_specific_hcpcs_share_global_pct",
    # Off-specialty signal (telemed rings billing outside stated specialty)
    "hcpcs_off_specialty_dollar_share",
    # Genetic-testing fraud (often packaged with telemed shells)
    "hcpcs_lab_genetic_share",
    # Sentinel codes (skin-substitute kickbacks, depression screens etc.
    # are common telemed-billed sentinel codes)
    "hcpcs_sentinel_dollar_share",
    "hcpcs_sentinel_depression_screen_share",
    # Psych 60-min (telehealth psychiatry overuse)
    "hcpcs_psych_60min_share",
    # Base volume + intensity signals (high volume + high tele share = scheme)
    "partb_total_medicare_payment",
    "partb_n_beneficiaries",
    "partb_svcs_per_bene",
    "partb_chrg_to_allowed",
]


SMALL_HEAD_SEEDS = {
    "DME": DME_SEEDS,
    "TELEHEALTH": TELEHEALTH_SEEDS,
}


def select_features(
    X: pd.DataFrame,
    y: np.ndarray,
    seeds: list[str],
    K: int = 30,
    method: str = "f_classif",
    random_state: int = 0,
) -> list[str]:
    """Pick top-K features = ``seeds`` first, then statistical fill.

    Parameters
    ----------
    X
        Train feature matrix. All columns must be numeric (NaNs OK).
    y
        Binary label vector, len(X).
    seeds
        Domain-curated feature names. Filtered to those present in
        ``X.columns``. Included unconditionally up to K.
    K
        Total feature budget.
    method
        ``"f_classif"`` (ANOVA F-stat, fast) or ``"mutual_info"`` (slower).
    random_state
        Seed for mutual_info (ignored by f_classif).

    Returns
    -------
    list[str]
        Selected column names. Length ≤ K. Seeds appear first in their
        original order, then F-score-ranked fill.
    """
    seed_cols = [c for c in seeds if c in X.columns]
    if len(seed_cols) >= K:
        return seed_cols[:K]

    candidates = [c for c in X.columns if c not in set(seed_cols)]
    if not candidates:
        return seed_cols

    # Compute univariate score for each candidate
    Xc = X[candidates].astype(np.float32).fillna(0).values
    if method == "mutual_info":
        from sklearn.feature_selection import mutual_info_classif
        scores = mutual_info_classif(Xc, y, random_state=random_state, discrete_features=False)
    else:
        from sklearn.feature_selection import f_classif
        scores, _ = f_classif(Xc, y)
        scores = np.nan_to_num(scores, nan=0.0)

    # Rank by score desc; fill to K
    order = np.argsort(-scores)
    fill_needed = K - len(seed_cols)
    fill = [candidates[i] for i in order[:fill_needed]]
    return seed_cols + fill
