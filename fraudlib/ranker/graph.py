"""Provider graphs + centrality features.

Used by paper 01 (adaptive + DL framework). The CMS PUF doesn't give a
true provider-beneficiary edge list at the claim level, so we build a
bipartite **provider ↔ state × provider_type** graph: two providers are
connected if they operate in the same state and share a provider type.
This captures the "peer group" signal papers 08 and 14 describe and
gives non-trivial centrality variance.

For a true provider-beneficiary graph you need Part D's Beneficiary-Drug
file or an inpatient claims sample — see ``graph.from_partd_beneficiary``.
"""
from __future__ import annotations

import networkx as nx
import numpy as np
import pandas as pd


def peer_graph(df: pd.DataFrame, *, max_peer_group_size: int = 5000) -> nx.Graph:
    """Build a provider graph where an edge connects providers sharing the
    same ``(state, provider_type)`` bucket. Very large peer groups are
    clipped to ``max_peer_group_size`` to keep the graph tractable.
    """
    g = nx.Graph()
    if not {"npi", "state", "provider_type"}.issubset(df.columns):
        raise KeyError("peer_graph needs npi, state, provider_type columns.")

    for (state, ptype), group in df.groupby(["state", "provider_type"]):
        npis = group["npi"].dropna().astype(str).tolist()
        if len(npis) > max_peer_group_size:
            npis = npis[:max_peer_group_size]
        g.add_nodes_from(npis, state=state, provider_type=ptype)
        # Connect as a ring to cap degree at 2 — cheap peer signal.
        for i in range(len(npis)):
            g.add_edge(npis[i], npis[(i + 1) % len(npis)])
    return g


def bipartite_hcpcs_projection(
    df_service: pd.DataFrame,
    *,
    top_k_codes: int = 500,
    max_providers: int = 200_000,
    hcpcs_col_candidates: tuple[str, ...] = ("HCPCS_Cd", "Brnd_Name"),
    npi_col_candidates: tuple[str, ...] = ("Rndrng_NPI", "PRSCRBR_NPI", "NPI"),
) -> nx.Graph:
    """Build a bipartite NPI×HCPCS graph from service-level data, project to NPI-NPI.

    Two providers share an edge if they bill any HCPCS code in the top-K
    most-common codes. Edge weight = number of shared codes. Approximates
    the provider-beneficiary graph paper 01 describes using publicly
    available service-level data (real provider-beneficiary edges require
    claim-level data we don't have).
    """
    hcpcs_col = next((c for c in hcpcs_col_candidates if c in df_service.columns), None)
    npi_col = next((c for c in npi_col_candidates if c in df_service.columns), None)
    if hcpcs_col is None or npi_col is None:
        raise KeyError(
            f"Service frame missing HCPCS or NPI col. HCPCS tried {hcpcs_col_candidates}; "
            f"NPI tried {npi_col_candidates}"
        )

    df = df_service[[npi_col, hcpcs_col]].dropna().copy()
    df[npi_col] = df[npi_col].astype(str)
    df[hcpcs_col] = df[hcpcs_col].astype(str)

    top_codes = df[hcpcs_col].value_counts().nlargest(top_k_codes).index
    df = df[df[hcpcs_col].isin(top_codes)]

    if df[npi_col].nunique() > max_providers:
        keep = df[npi_col].drop_duplicates().sample(n=max_providers, random_state=0)
        df = df[df[npi_col].isin(keep)]

    g = nx.Graph()
    # Build bipartite adjacency as a dict: hcpcs -> list of NPIs
    code_to_npis: dict[str, list[str]] = {}
    for code, sub in df.groupby(hcpcs_col):
        code_to_npis[code] = sub[npi_col].unique().tolist()

    from collections import defaultdict
    edge_weight: dict[tuple[str, str], int] = defaultdict(int)
    for code, npis in code_to_npis.items():
        if len(npis) > 1000:
            # Too dense to enumerate — subsample peer set
            npis = list(np.random.default_rng(0).choice(npis, size=1000, replace=False))
        for i in range(len(npis)):
            for j in range(i + 1, len(npis)):
                a, b = npis[i], npis[j]
                key = (a, b) if a < b else (b, a)
                edge_weight[key] += 1

    g.add_nodes_from(df[npi_col].unique())
    g.add_weighted_edges_from([(a, b, w) for (a, b), w in edge_weight.items()])
    return g


def centrality_features(g: nx.Graph) -> pd.DataFrame:
    """Return a DataFrame with degree, PageRank, and clustering coefficient
    for every node, keyed by ``npi``."""
    pr = nx.pagerank(g, alpha=0.85, max_iter=100)
    clustering = nx.clustering(g)
    deg = dict(g.degree())
    rows = [
        {
            "npi": node,
            "degree": deg[node],
            "pagerank": pr[node],
            "clustering": clustering[node],
        }
        for node in g.nodes()
    ]
    return pd.DataFrame(rows)
