"""Permanent feature frame cache — build once, reuse across label/rule changes.

The expensive part of the pipeline (raw CMS join + Part D + HCPCS + Bauder
+ derived ratios + peer percentiles) takes ~30 min for 11 years. This module
caches the result so that label changes, rule changes, and archetype changes
only require the fast re-stamp (~2 min total).

Usage:
    from fraudlib.ranker.feature_frames import load_feature_frame, stamp_labels_and_filter

    # First call builds from raw data and caches (~3 min per year)
    df = load_feature_frame(2023)

    # Subsequent calls load from cache instantly
    df = load_feature_frame(2023)

    # Then stamp labels, apply rules, attach graph features (fast):
    df = stamp_labels_and_filter(df, 2023, rules, fraud_npis=fraud_npis)
"""
from __future__ import annotations

import gc
from pathlib import Path

import numpy as np
import pandas as pd

FEATURE_CACHE = Path("data/cache/feature_frames")

# Features derived from raw Part B columns
_DERIVED_FEATURES = [
    ("partb_pay_per_bene", lambda df: df["partb_total_medicare_payment"].clip(lower=1) / df["partb_n_beneficiaries"].clip(lower=1)),
    ("partb_svcs_per_bene", lambda df: df["partb_n_services"].clip(lower=1) / df["partb_n_beneficiaries"].clip(lower=1)),
    ("partb_dual_pct", lambda df: df["partb_bene_dual_cnt"].fillna(0) / df["partb_n_beneficiaries"].clip(lower=1)),
    ("partb_under65_pct", lambda df: df["partb_bene_age_lt_65_cnt"].fillna(0) / df["partb_n_beneficiaries"].clip(lower=1)),
    ("partb_over84_pct", lambda df: df["partb_bene_age_gt_84_cnt"].fillna(0) / df["partb_n_beneficiaries"].clip(lower=1)),
    ("partb_drug_frac", lambda df: df["partb_drug_mdcr_pymt_amt"].fillna(0) / df["partb_total_medicare_payment"].clip(lower=1)),
    ("partb_chrg_to_allowed", lambda df: df["partb_tot_sbmtd_chrg"].fillna(0) / df["partb_total_medicare_allowed"].clip(lower=1)),
    ("partb_risk_score", lambda df: df["partb_bene_avg_risk_score"]),
    ("partb_avg_age", lambda df: df["partb_bene_avg_age"]),
]


def _build_feature_frame(year: int) -> pd.DataFrame:
    """Build the full feature frame from raw CMS data. Expensive (~3 min)."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    from fraudlib.ranker import pipeline as ranker_pipeline
    from fraudlib.ranker.bauder_features import attach_bauder_features
    from scripts.experiments.discover_multisource_rules_v2 import (
        SCANNABLE_FEATURES,
        _add_peer_pcts,
    )

    CASES_PATH = Path("data/extracted/gold_set.jsonl")  # canonical (was stale data/source/)

    print(f"  Building feature frame for {year} from raw CMS data...")

    # Step 1: Load raw CMS multi-source frame
    print(f"    Building base frame from raw panels...")
    df = ranker_pipeline.load_training_frame(
        year,
        sources=("partb", "partd", "nppes", "hcpcs"),
        cases_path=CASES_PATH,
        verbose=True,
    )

    # Step 2: Derive ratios
    for feat_name, fn in _DERIVED_FEATURES:
        if feat_name not in df.columns:
            try:
                df[feat_name] = fn(df)
            except (KeyError, TypeError):
                pass

    # Step 3: Bauder features (service-level entropy, code concentration)
    df = attach_bauder_features(df, year, npi_col="npi")

    # Step 4: Peer percentiles (within specialty+state)
    available = [f for f in SCANNABLE_FEATURES if f in df.columns]
    df = _add_peer_pcts(df, available)

    # Step 5: Part D has_partd flag
    partd_cols = [c for c in df.columns if c.startswith("partd_")]
    if partd_cols:
        df["has_partd"] = df[partd_cols[0]].notna().astype(int)

    # Step 6: npi_padded (needed by stamp_labels_and_filter)
    if "npi_padded" not in df.columns and "npi" in df.columns:
        df["npi_padded"] = df["npi"].astype(str).str.zfill(10)

    df["puf_year"] = year

    return df


def load_feature_frame(year: int, attach_extras: bool = True, use_full_cache: bool = True) -> pd.DataFrame:
    """Load feature frame from cache, or build and cache if missing.

    This frame contains ALL features but NO labels, NO rules, NO graph features.
    Those are stamped on top by the training pipeline.

    Parameters
    ----------
    attach_extras : bool
        If True (default), attach the satellite caches (cl_*, dme_*, dmer_*,
        xprog_*, op_*, hcpcs_*). Set False to reproduce the May 4 baseline
        (commit 937e530), which predates these features.
    use_full_cache : bool
        If True (default), use the merged ``features_full_{year}.parquet``
        cache when it's fresher than every satellite. Skips the 5-step merge
        loop (saves ~15 sec per year). Built lazily on first call when the
        cache is missing or stale. Only consulted when ``attach_extras=True``.
        Freshness check: full cache mtime > every satellite cache mtime.
    """
    FEATURE_CACHE.mkdir(parents=True, exist_ok=True)
    cache_path = FEATURE_CACHE / f"features_{year}.parquet"
    full_path = FEATURE_CACHE / f"features_full_{year}.parquet"

    # ── Fast path: load merged full cache if fresh ──
    # Skips the base+5×attach loop entirely. Triggered only when
    # attach_extras=True (full cache includes the attaches) and the cache
    # exists with mtime ≥ every satellite cache.
    if attach_extras and use_full_cache and full_path.exists():
        sat_paths = [
            cache_path,
            FEATURE_CACHE / f"hcpcs_rich_{year}.parquet",
            FEATURE_CACHE / f"dmepos_rich_{year}.parquet",
            Path("data/cache/dmepos_features") / f"dmepos_{year}.parquet",
            Path("data/cache/openpayments_features") / f"op_{year}.parquet",
            Path("data/cache/claim_level_features") / f"claim_{year}.parquet",
        ]
        full_mtime = full_path.stat().st_mtime
        stale = any(p.exists() and p.stat().st_mtime > full_mtime for p in sat_paths)
        if not stale:
            print(f"  {year}: loading FULL merged cache ({full_path.name})")
            df = pd.read_parquet(full_path)
            for c in ("nppes_credential_bucket", "nppes_taxonomy_bucket"):
                if c in df.columns:
                    df[c] = df[c].astype(str)
            return df
        else:
            print(f"  {year}: full cache stale (satellite newer) — rebuilding")

    if cache_path.exists():
        print(f"  {year}: loading cached features ({cache_path.name})")
        df = pd.read_parquet(cache_path)
        for c in ("nppes_credential_bucket", "nppes_taxonomy_bucket"):
            if c in df.columns:
                df[c] = df[c].astype(str)
    else:
        df = _build_feature_frame(year)

        # Strip any label columns that might have been attached during base frame build
        label_cols = [c for c in df.columns if c in (
            "y", "y_eval", "fraud_case", "fraud_label", "fraud_leie",
            "case_ids", "case_fraud_types", "case_tier", "label_bucket",
            "label_source", "label_confidence", "archetype",
        )]
        if label_cols:
            df = df.drop(columns=label_cols)
            print(f"    Stripped {len(label_cols)} label columns from feature frame")

        # Save
        for c in df.select_dtypes(include="object").columns:
            df[c] = df[c].where(df[c].notna(), "").astype(str)
        for c in df.select_dtypes(include="category").columns:
            df[c] = df[c].astype(str)

        df.to_parquet(cache_path, index=False)
        print(f"  {year}: cached features ({len(df):,} rows, {len(df.columns)} cols)")
        gc.collect()

    if not attach_extras:
        return df

    # ── Attach claim-level features (cl_*) ──
    # Independent cache at data/cache/claim_level_features/claim_{year}.parquet
    try:
        from fraudlib.ranker.claim_level_features import attach_claim_features
        before = len(df.columns)
        df = attach_claim_features(df, year, npi_col="npi")
        added = [c for c in df.columns if c.startswith("cl_")]
        if added and len(df.columns) > before:
            print(f"  {year}: + {len(added)} claim-level features (cl_*)")
    except FileNotFoundError as e:
        print(f"  {year}: claim-level features unavailable ({e})")
    except Exception as e:
        print(f"  {year}: claim-level features error ({e})")

    cl_cols = [c for c in df.columns if c.startswith("cl_")]
    if cl_cols:
        df[cl_cols] = df[cl_cols].fillna(0)

    # ── Attach DMEPOS referring-provider features (dme_*) ──
    # Independent cache at data/cache/dmepos_features/dmepos_{year}.parquet
    # 2013 unavailable (CMS started referring-provider cut at 2014).
    try:
        from fraudlib.ranker.dmepos_features import attach_dmepos_features
        before = len(df.columns)
        df = attach_dmepos_features(df, year, npi_col="npi")
        added = [c for c in df.columns if c.startswith("dme_")]
        if added and len(df.columns) > before:
            print(f"  {year}: + {len(added)} DMEPOS features (dme_*)")
    except FileNotFoundError as e:
        print(f"  {year}: DMEPOS features unavailable ({e})")
    except Exception as e:
        print(f"  {year}: DMEPOS features error ({e})")

    dme_cols = [c for c in df.columns if c.startswith("dme_")]
    if dme_cols:
        df[dme_cols] = df[dme_cols].fillna(0)

    # ── Attach DMEPOS rich features (dmer_*) ──
    # 32 referrer-side concentration / category-mix / specialty-percentile
    # features built by scripts/compute_dmepos_rich_features.py. Targets the
    # kickback / brace-mill scheme blind spot (per the post-v9-retrain
    # diagnosis: 80% of v9 KICKBACK positives missed at top-10K).
    # 2013 unavailable (CMS Referring Provider PUF starts at 2014).
    try:
        dmer_path = FEATURE_CACHE / f"dmepos_rich_{year}.parquet"
        if dmer_path.exists():
            dmer = pd.read_parquet(dmer_path)
            dmer["npi"] = dmer["npi"].astype(str).str.zfill(10)
            already = set(df.columns)
            new_cols = [c for c in dmer.columns if c == "npi" or c not in already]
            dmer = dmer[new_cols]
            before = len(df.columns)
            df = df.merge(dmer, on="npi", how="left")
            added = [c for c in df.columns if c.startswith("dmer_") and c not in already]
            if added:
                df[added] = df[added].fillna(0)
                print(f"  {year}: + {len(added)} DMEPOS rich features (dmer_*)")
    except Exception as e:
        print(f"  {year}: DMEPOS rich features error ({e})")

    # ── Cross-program referral-graph proxies (xprog_*) ──
    # The DMEPOS PUF doesn't enumerate suppliers — we can't build a true
    # bipartite referral graph. But cross-program *ratios* proxy the same
    # kickback signature: a referrer whose DME orders dwarf their Part B
    # E&M billing is the canonical brace-mill recipient.
    try:
        partb_pay = pd.to_numeric(df.get("partb_total_medicare_payment", 0), errors="coerce").fillna(0)
        partb_benes = pd.to_numeric(df.get("partb_n_beneficiaries", 0), errors="coerce").fillna(0)
        dme_pay = pd.to_numeric(df.get("dme_total_payment", 0), errors="coerce").fillna(0)
        dme_benes = pd.to_numeric(df.get("dme_total_benes", 0), errors="coerce").fillna(0)
        partd_cost = pd.to_numeric(df.get("partd_total_drug_cost", df.get("partd_tot_drug_cst", 0)), errors="coerce").fillna(0)

        # DME $ as a multiple of Part B $: brace-mills often have <$50K Part B
        # billing and $500K+ in DME referrals. Log-transformed because the
        # ratio spans many orders of magnitude.
        df["xprog_dme_to_partb_ratio"] = np.where(
            partb_pay > 1, dme_pay / partb_pay.clip(lower=1), 0
        )
        df["xprog_dme_to_partb_log"] = np.log1p(df["xprog_dme_to_partb_ratio"].clip(upper=1000))

        # DME benes / Part B benes > 1 = ordering DME for people you don't see
        # clinically (ghost-bene or census-fraud pattern). Clip at 10 to
        # avoid degenerate ratios from tiny denominators.
        df["xprog_dme_bene_to_partb_bene"] = np.where(
            partb_benes > 0, (dme_benes / partb_benes.clip(lower=1)).clip(upper=10.0), 0
        )

        # Combined-payment dominance: what fraction of (partb + dme + partd_drug)
        # is from DME orders? High = primary income source is DME referrals.
        total = (partb_pay + dme_pay + partd_cost).clip(lower=1)
        df["xprog_dme_pay_dominance"] = dme_pay / total

        # Indicator: high DME, low Part B — the structural brace-mill signature.
        # ($25K+ DME orders, but <$10K Part B billing.)
        df["xprog_brace_mill_indicator"] = (
            (dme_pay > 25_000) & (partb_pay < 10_000)
        ).astype(int)

        xprog_cols = [c for c in df.columns if c.startswith("xprog_")]
        df[xprog_cols] = df[xprog_cols].replace([np.inf, -np.inf], np.nan).fillna(0)
        print(f"  {year}: + {len(xprog_cols)} cross-program features (xprog_*)")
    except Exception as e:
        print(f"  {year}: cross-program features error ({e})")

    # ── Attach Open Payments features (op_*) ──
    # Independent cache at data/cache/openpayments_features/op_{year}.parquet
    # 2013–2014 unavailable (no NPI in CMS schema). 2015+ available.
    try:
        from fraudlib.ranker.openpayments_features import attach_op_features
        before = len(df.columns)
        df = attach_op_features(df, year, npi_col="npi")
        added = [c for c in df.columns if c.startswith("op_")]
        if added and len(df.columns) > before:
            print(f"  {year}: + {len(added)} Open Payments features (op_*)")
    except FileNotFoundError as e:
        print(f"  {year}: Open Payments features unavailable ({e})")
    except Exception as e:
        print(f"  {year}: Open Payments features error ({e})")

    op_cols = [c for c in df.columns if c.startswith("op_")]
    if op_cols:
        df[op_cols] = df[op_cols].fillna(0)

    # ── Attach HCPCS rich features (hcpcs_*_pct, hcpcs_*_global_pct, code-family ratios) ──
    # Independent cache at data/cache/feature_frames/hcpcs_rich_{year}.parquet
    # ~210 columns of code-family ratios + sentinel-code shares + telehealth
    # POS share + off-specialty share, with within-specialty + global percentiles.
    # Built by scripts/compute_hcpcs_rich_features.py.
    try:
        rich_path = FEATURE_CACHE / f"hcpcs_rich_{year}.parquet"
        if rich_path.exists():
            rich = pd.read_parquet(rich_path)
            # Drop overlap helpers + provider_type (we already have specialty info)
            drop_cols = [c for c in rich.columns if c in (
                "provider_type", "total_srvcs", "total_paid", "n_codes", "benes_sum",
            )]
            rich = rich.drop(columns=drop_cols, errors="ignore")
            # Make sure NPIs are aligned (string-padded 10-digit)
            rich["npi"] = rich["npi"].astype(str).str.zfill(10)
            # Avoid duplicate columns: drop hcpcs_* already present
            already = set(df.columns)
            new_cols = [c for c in rich.columns if c == "npi" or c not in already]
            rich = rich[new_cols]
            before = len(df.columns)
            df = df.merge(rich, on="npi", how="left")
            added = [c for c in df.columns if c.startswith("hcpcs_") and c not in already]
            if added:
                df[added] = df[added].fillna(0)
                print(f"  {year}: + {len(added)} HCPCS rich features (hcpcs_*)")
    except Exception as e:
        print(f"  {year}: HCPCS rich features error ({e})")

    # ── Write merged full cache for fast subsequent loads ──
    # Skipped if attach_extras=False (we wouldn't be writing the merged frame
    # anyway) or if use_full_cache was disabled at call time. The freshness
    # check on the read side handles invalidation when satellites update.
    if attach_extras and use_full_cache:
        try:
            df_save = df.copy()
            # Parquet hates mixed object dtypes — normalize before write
            for c in df_save.select_dtypes(include="object").columns:
                df_save[c] = df_save[c].where(df_save[c].notna(), "").astype(str)
            for c in df_save.select_dtypes(include="category").columns:
                df_save[c] = df_save[c].astype(str)
            df_save.to_parquet(full_path, index=False)
            print(f"  {year}: wrote FULL merged cache "
                  f"({len(df_save):,} rows × {len(df_save.columns)} cols → {full_path.name})")
            del df_save
            gc.collect()
        except Exception as e:
            print(f"  {year}: failed to write full cache ({e})")

    return df


def stamp_labels_and_filter(
    df: pd.DataFrame,
    year: int,
    rules: list[dict],
    fraud_npis: set[str] | None = None,
    apply_filter: bool = True,
    drop_organizational_npis: bool = True,
    archetype_module: str = "schemes",
    active_only: bool = False,
    bene_floor: int = 50,
    pay_floor: float = 5000.0,
) -> pd.DataFrame:
    """Fast layer: stamp labels, apply rules, attach graph features.

    Parameters
    ----------
    apply_filter : bool
        If True (default), restrict to rows where at least one rule fires.
    drop_organizational_npis : bool
        If True (default), drop NPI Type-2 (organizational) providers.
    archetype_module : str
        - ``"schemes"`` (default) → 10 ``scheme_*`` multi-label columns from
          ``fraudlib.eval.archetypes`` (DOJ-only).
        - ``"v1"`` → 6 ``arch_*`` multi-label columns from
          ``fraudlib.eval.archetypes_v1`` (DOJ-only, prior 6-bucket vocab).
        - ``"v0"`` → 6 single-label cascade from
          ``fraudlib.eval.archetypes_v0`` (DOJ + LEIE; matches commit 937e530).
          Sets ``archetype`` directly from the cascade and emits ``arch_*``
          binary indicator columns.
    active_only : bool
        Only meaningful for ``archetype_module="v0"``. If True, demote
        LEIE-only positives below ``bene_floor`` benes or ``pay_floor`` pay.
        DOJ positives always retained. Mirrors the May 4 baseline behavior.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    from scripts.experiments.train_filtered_ranker import _apply_rules_get_fires

    df = df.copy()

    if "npi_padded" not in df.columns and "npi" in df.columns:
        df["npi_padded"] = df["npi"].astype(str).str.zfill(10)

    # ── Drop organizational NPIs (NPI Type-2) ──
    if drop_organizational_npis and "partb_entity_code" in df.columns:
        before = len(df)
        df = df[df["partb_entity_code"] != "O"].reset_index(drop=True)
        n_dropped = before - len(df)
        print(f"  {year}: dropped {n_dropped:,} organizational (Type-2) NPIs "
              f"({n_dropped/before*100:.1f}%)")

    if archetype_module == "v0":
        # ── Single-label cascade (DOJ + LEIE), matches commit 937e530 ──
        from fraudlib.eval.archetypes_v0 import ARCHETYPES as HEADS, archetype_lookup as v0_lookup
        from fraudlib.labels.ground_truth import load_positives_scoped

        arch = v0_lookup(year)
        df["archetype"] = df["npi_padded"].map(arch).fillna("")

        if active_only:
            doj_meta = load_positives_scoped(year, min_confidence="high")
            doj_npis = {str(n).zfill(10) for n in doj_meta.keys()}
            benes = df.get("partb_n_beneficiaries", pd.Series(0, index=df.index)).fillna(0)
            pay = df.get("partb_total_medicare_payment", pd.Series(0, index=df.index)).fillna(0)
            inactive = (benes < bene_floor) | (pay < pay_floor)
            leie_only = (df["archetype"] != "") & (~df["npi_padded"].isin(doj_npis))
            demote_mask = leie_only & inactive
            n_demoted = int(demote_mask.sum())
            if n_demoted:
                df.loc[demote_mask, "archetype"] = ""
                print(f"  {year}: demoted {n_demoted} inactive LEIE-only positives "
                      f"(benes<{bene_floor} or pay<${pay_floor:.0f})")

        df["y_eval"] = (df["archetype"] != "").astype(int)
        for h in HEADS:
            df[f"arch_{h}"] = (df["archetype"] == h).astype(int)
        head_counts = {h: int(df[f"arch_{h}"].sum()) for h in HEADS if df[f"arch_{h}"].sum() > 0}
    else:
        # ── Multi-label modes ──
        if archetype_module == "v1":
            from fraudlib.eval.archetypes_v1 import ARCHETYPES_V1 as HEADS, archetypes_v1_lookup as lookup_fn
            col_prefix = "arch_"
        else:
            from fraudlib.eval.archetypes import SCHEMES as HEADS, scheme_lookup as lookup_fn
            col_prefix = "scheme_"

        multi = lookup_fn(year)
        positive_npis = set(multi.keys())
        head_dicts = {h: {npi: 1 for npi, heads in multi.items() if h in heads} for h in HEADS}
        for head in HEADS:
            df[f"{col_prefix}{head}"] = df["npi_padded"].map(head_dicts[head]).fillna(0).astype(int)
        df["y_eval"] = df["npi_padded"].isin(positive_npis).astype(int)
        archetype_dict = {npi: sorted(heads)[0] for npi, heads in multi.items()}
        df["archetype"] = df["npi_padded"].map(archetype_dict).fillna("")
        head_counts = {h: int(df[f"{col_prefix}{h}"].sum()) for h in HEADS if df[f"{col_prefix}{h}"].sum() > 0}

    n_pos = int(df["y_eval"].sum())
    print(f"  {year}: {n_pos} positives ({archetype_module}: {head_counts})")
    df["fraud_case"] = df["y_eval"]
    df["y"] = df["y_eval"]
    df["label_bucket"] = df["y_eval"].map({0: "neither", 1: "case_only"})

    # Apply rules
    rule_df, any_fires = _apply_rules_get_fires(df, rules)
    df = pd.concat([df, rule_df], axis=1)
    if apply_filter:
        df = df[any_fires].reset_index(drop=True)

    # Graph fraud-neighbor features removed 2026-05-29 (leakage risk: they consumed
    # training-year fraud NPIs). They were not in the live features_full cache and
    # contributed nothing. HCPCS centrality (graph_hcpcs_*) is unaffected — it is
    # structural (co-billing), uses no labels. `fraud_npis` is retained for API
    # compatibility with callers. See WALK_FORWARD_PIPELINE.md §3 issue #6.

    n_pos = int(df["y_eval"].sum())
    tag = "stamped + filtered" if apply_filter else "stamped (unfiltered)"
    print(f"  {year}: {tag} → {len(df):,} rows, {n_pos} positives")

    return df
