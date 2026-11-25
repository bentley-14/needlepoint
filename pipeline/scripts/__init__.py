"""Operational scripts that read/rewrite pipeline artifacts on R2.

One-shot backfills (stamp legacy parquets for the BaseScraper contract)
and schema-driven dtype recasts. Run as modules, e.g.
``python -m pipeline.scripts.recast_r2_dtypes --source cms_partd``.
"""
