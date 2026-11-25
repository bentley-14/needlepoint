"""Scheduled-job runners for data ingestion.

One module per cron-driven workflow. Each module is invoked by a
corresponding ``.github/workflows/<source>-<cadence>.yml`` file and
shares no state with the others — they're independent entry points.

Shared helpers (snapshot diffing, manifest rewrites, etc.) live in
``pipeline.scheduled_scripts.utils``.
"""
