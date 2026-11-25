"""Feature loaders — read parquets from ``data/panels`` and ``data/cache``.

Pure consumer code. Functions here must never call the network; producer
code lives under ``pipeline/`` and is invoked via Makefile targets or
``python -m pipeline.X``. A loader hitting a missing parquet should
raise ``FileNotFoundError`` with the regen incantation, not silently fetch.

Empty at PR 0 — subsequent PRs introduce ``load_{nppes,pecos,...}_features``
here as their producer halves move into ``pipeline/ingest/providers``.
"""
