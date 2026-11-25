"""Shared library for the NeedlePoint pipeline (see ``docs/PIPELINE_SCHEMATIC.md``).

    labels/   label loaders (gold_set, case-based positives)
    sources/  feature loaders (CMS panels)
    ranker/   walk-forward ML ranker
    entity/   panel-wide entity and ring resolution
    rules/    rule features
    eval/     shared evaluation helpers
    ingest/   source ingestion helpers
    r2.py     data access: local files, a private bucket, or the public dataset
"""
