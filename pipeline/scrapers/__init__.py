"""Per-source scrapers — each module fetches one external data source.

Every scraper inherits from :class:`pipeline.base.BaseScraper`. Naming
convention is ``<publisher>_<source>.py`` (e.g. ``cms_partb.py``,
``doj_press_release.py``, ``policy_oig.py``) so a glob like
``pipeline/scrapers/cms_*.py`` finds the family.

Until per-PUF splitting lands, ``download.py`` holds the legacy
multi-function CMS + LEIE + PECOS + Care Compare fetchers in a single
file. Subsequent PRs split each fetcher into its own ``BaseScraper``
subclass.
"""
