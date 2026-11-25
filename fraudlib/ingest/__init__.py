"""Raw-document ingest utilities — being migrated out to ``pipeline/``.

This package today holds the surviving CMS / providers / policy / extract
fetchers. The producer half of all of these will move under ``pipeline/``
in subsequent PRs;
the loader half moves to ``fraudlib/sources/``. Label loaders already
migrated to ``fraudlib/labels/``.
"""
