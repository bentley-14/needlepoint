"""Referral / patient-sharing graph loader (stub).

Our current provider-graph features in ``fraudlib/ranker/graph.py`` rely on
a synthetic peer graph built from shared HCPCS codes. Real directed
referral edges — "beneficiary seen by provider A, then by provider B within
N days" — come from one of two places:

  * **CMS Physician Shared Patient Patterns (PSPP)** — free via NBER mirror,
    covers 2009-2015, four time windows (30/60/90/180 days).
  * **CareSet DocGraph Hop Teaming PUF** — free for 2014-2020 on request,
    paid for 2021+. Same edge structure as PSPP.


Expected normalized schema after loading:

    referring_npi : str     # NPI seen first in the window (edge source)
    rendered_npi  : str     # NPI seen second (edge destination)
    n_patients    : int     # distinct shared beneficiaries in the year
    n_transactions: int     # total shared encounter pairs (>= n_patients)
    year          : int     # calendar year of underlying claims
    window_days   : int     # 30 / 60 / 90 / 180

Intended call sites:

    * ``fraudlib.ranker.graph.referral_graph(df_edges)``  — to be added; wraps
      the DataFrame in an ``nx.DiGraph`` for PageRank / centrality.
    * ``fraudlib.ranker.graph.bipartite_hcpcs_projection`` stays as the
      graceful fallback when this loader raises.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

# Canonical on-disk layout once data is procured. Nothing lives here yet.
RAW_DIR = Path("data/raw/docgraph")
PROCESSED_DIR = Path("data/processed/docgraph")


def load_docgraph(
    *,
    year: int | None = None,
    window_days: int = 180,
) -> pd.DataFrame:
    """Load the DocGraph / PSPP referral edge list for a given year and window.

    Returns a DataFrame with columns ``referring_npi``, ``rendered_npi``,
    ``n_patients``, ``n_transactions``, ``year``, ``window_days``.

    Not yet implemented: the data is not procured (PSPP 2009-2015 via NBER,
    DocGraph Teaming 2014-2020 via CareSet on request, paid 2021+).

    Call sites should wrap this in try/except to fall back to the synthetic
    HCPCS projection in :func:`fraudlib.ranker.graph.bipartite_hcpcs_projection`
    until real data lands.
    """
    raise NotImplementedError(
        "Referral-graph data is not yet procured. "
        "Sources: PSPP (NBER, free, 2009-2015) "
        "and DocGraph Hop Teaming (CareSet, free 2014-2020). "
        f"Expected file: {PROCESSED_DIR}/docgraph_{year}_{window_days}d.parquet"
    )


def load_docgraph_or_none(
    *,
    year: int | None = None,
    window_days: int = 180,
) -> pd.DataFrame | None:
    """Graceful-fallback variant: returns ``None`` instead of raising when the
    data is not available on disk. Use this at ranker call sites so the
    pipeline keeps running on the synthetic peer graph until we procure
    DocGraph.
    """
    try:
        return load_docgraph(year=year, window_days=window_days)
    except (NotImplementedError, FileNotFoundError):
        return None
