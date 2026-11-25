#!/usr/bin/env python3
"""Ring-enrichment — individual → company/ring → collectibility + novelty.

LOOP-SIDE investigation tool (NOT the ML ranker). Per the qui tam counsel recalibration
(the qui-tam target-value framework): our detector finds anomalous *individuals*
well, but the valuable qui-tam target is the *entity/ring* that RETAINED the money
and is not already known to the government. This turns a flagged individual into a
company/ring lead and judges it:

  1. DAC Org_PAC_ID shared-entity  — co-billers under the SAME billing entity
     (physician_compare_lookup; catches group/ring rings).
  2. PECOS reassignment vehicle    — the group(s) this INDIVIDUAL reassigns billing
     to + who else reassigns there (pecos_lookup; catches reassignment-OUT schemes
     like Spruce -> Wound Co Nutmeg that the DAC day-job group misses).
  3. Ring score                    — how many ring members are themselves ranker-
     anomalous (join to the walk-forward scored panel). >=2 anomalous = a RING, not
     a lone outlier (much harder to explain innocently).
  4. Novelty screen                — is the entity/ring already public/charged?
     (2026-takedown roster + CourtListener docket search). "gov-known" = value
     discount; "no public case" = potentially novel (sealed cases stay invisible).

Emits the collectible ENTITY as the target candidate, not the rendering cog.

Usage:
    python scripts/ring_enrichment.py --npi 1900000068
    python scripts/ring_enrichment.py --npi 1900000068 --year 2024 --format json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent          # scripts/
_ROOT = _HERE.parent                             # repo root
for _p in (str(_HERE), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pecos_lookup                               # noqa: E402  (same dir)
import physician_compare_lookup as pc             # noqa: E402
from fraudlib import r2                           # noqa: E402

# A ring member counts as "anomalous" if it lands in the top ANOMALY_PCT of the
# ranker panel by score_rrf. 0.02 = top 2% (~top 4,250 of ~212k).
ANOMALY_PCT = 0.02


# ── ranker panel join ────────────────────────────────────────────────────────
def _scored_panel(year: int) -> str | None:
    g = sorted(glob.glob(str(_ROOT / f"results/walk_forward_v4_top5_y{year}_*/scored_full_{year}.parquet")))
    g = g or r2.remote_glob(f"results/current/walk_forward_v4_top5_y{year}_*/scored_full_{year}.parquet")
    return g[0] if g else None


def _panel_ranks(npis: list[str], year: int) -> tuple[dict[str, dict], int]:
    """{npi: {rank, pct, name, specialty, state, pay}} for the npis present + panel N."""
    path = _scored_panel(year)
    npis = [str(n) for n in dict.fromkeys(npis) if n]
    if not path or not npis:
        return {}, 0
    ids = ",".join(f"'{n}'" for n in npis)
    df = r2.duckdb_r2().execute(
        f"""WITH r AS (
              SELECT CAST(npi AS VARCHAR) npi,
                     trim(UPPER(COALESCE(partb_rndrng_prvdr_first_name,''))||' '||
                          UPPER(COALESCE(partb_rndrng_prvdr_last_org_name,''))) nm,
                     partb_provider_type sp, partb_state st,
                     partb_total_medicare_payment pay,
                     ROW_NUMBER() OVER (ORDER BY score_rrf DESC) rnk,
                     COUNT(*) OVER () tot
              FROM read_parquet('{path}'))
            SELECT * FROM r WHERE npi IN ({ids})""").fetchdf()
    out: dict[str, dict] = {}
    tot = 0
    for _, row in df.iterrows():
        tot = int(row.tot)
        out[row.npi] = {"rank": int(row.rnk), "pct": int(row.rnk) / int(row.tot),
                        "name": (row.nm or "").strip(), "specialty": row.sp,
                        "state": row.st, "pay": float(row.pay or 0)}
    return out, tot


# ── entity sources ───────────────────────────────────────────────────────────
def _dac_entity(npi: str) -> dict:
    """DAC Org_PAC_ID: the NPI's own group(s) + other NPIs sharing the same Org_PAC_ID."""
    try:
        con = r2.duckdb_r2()
        src = pc._source(None)
        own = pc._query(con, src, f"CAST(npi AS VARCHAR) = '{npi}'")
    except Exception as e:  # R2 / DAC unavailable — degrade gracefully
        return {"_error": f"DAC lookup failed: {e}", "groups": [], "members": []}
    groups, pac_ids = [], []
    for row in own:
        pid = row.get("Org_PAC_ID")
        if pid and str(pid) not in pac_ids:
            pac_ids.append(str(pid))
            groups.append({"org_pac_id": str(pid), "name": row.get("Org_Lgl_Nm"),
                           "members_declared": row.get("Num_Org_Mem")})
    members: list[str] = []
    if pac_ids:
        ids = ",".join(f"'{p}'" for p in pac_ids)
        try:
            shared = pc._query(con, src,
                               f"CAST(Org_PAC_ID AS VARCHAR) IN ({ids}) "
                               f"AND CAST(npi AS VARCHAR) <> '{npi}' LIMIT 300")
            members = [str(r["npi"]) for r in shared if r.get("npi")]
        except Exception:
            pass
    return {"groups": groups, "members": members}


def _reassignment_entity(npi: str) -> dict:
    """PECOS: group(s) this individual reassigns billing to + co-reassigned NPIs."""
    try:
        rows = pecos_lookup._api_call([("filter[Individual NPI]", npi), ("size", "500")],
                                      url=pecos_lookup.REASSIGNMENT_API_URL)
    except Exception as e:
        return {"_error": f"PECOS reassignment lookup failed: {e}", "groups": [], "members": []}
    if isinstance(rows, dict):  # {_error: ...}
        return {"_error": rows.get("_error", "reassignment API error"), "groups": [], "members": []}
    groups: dict[str, dict] = {}
    for r in rows:
        gp = r.get("Group PAC ID")
        if gp and str(gp) not in groups:
            groups[str(gp)] = {"group_pac_id": str(gp),
                               "name": r.get("Group Legal Business Name"),
                               "state": r.get("Group State Code")}
    # who else reassigns to those groups (the ring via the billing vehicle)
    members: list[str] = []
    for gp in list(groups)[:3]:
        try:
            co = pecos_lookup._api_call([("filter[Group PAC ID]", gp), ("size", "500")],
                                        url=pecos_lookup.REASSIGNMENT_API_URL)
            if isinstance(co, list):
                members += [str(r.get("Individual NPI")) for r in co
                            if r.get("Individual NPI") and str(r.get("Individual NPI")) != npi]
        except Exception:
            pass
    return {"groups": list(groups.values()), "members": sorted(set(members))}


# ── novelty screen ───────────────────────────────────────────────────────────
def _roster_entries() -> list[str]:
    """One lowercased string PER takedown defendant (so name-token matching is
    per-entry — avoids matching 'carlos' in one row + 'reyes' in another)."""
    p = _ROOT / "investigations/2026-takedown/roster.json"
    if not p.exists():
        return []
    try:
        return [(d.get("defendant", "") + " " + d.get("case", "")).lower()
                for d in json.load(open(p))]
    except Exception:
        return []


# Generic corporate/role tokens that cause name collisions — dropped before matching.
# NB: keep domain-descriptive words (wound, medic, kidney, clinic, …) as distinctive.
_NAME_STOP = {"llc", "inc", "pa", "pllc", "pc", "corp", "co", "ltd", "the", "of",
              "and", "md", "do", "dds", "dpm", "dr", "mr", "ms", "mrs", "p"}


def _distinctive_tokens(name: str) -> list[str]:
    toks = re.split(r"[^a-z0-9]+", str(name).lower())
    return [t for t in toks if len(t) >= 3 and t not in _NAME_STOP]


def _name_matches(name: str, text: str) -> bool:
    """True only if ALL distinctive tokens of `name` appear in `text` — cuts the
    single-token collisions (e.g. 'reyes' matching an unrelated 'Serrano Reyes')."""
    toks = _distinctive_tokens(name)
    return bool(toks) and all(t in text for t in toks)


def _cl_token() -> str:
    try:
        for ln in open(_ROOT / ".env"):
            if ln.startswith(("COURTLISTENER_TOKEN", "COURTLISTENER_API_TOKEN")):
                return ln.split("=", 1)[1].strip().strip('"\'')
    except FileNotFoundError:
        pass
    return os.environ.get("COURTLISTENER_TOKEN", "")


def _courtlistener(query: str) -> list[dict]:
    tok = _cl_token()
    H = {"Authorization": f"Token {tok}"} if tok else {}
    url = "https://www.courtlistener.com/api/rest/v4/search/?" + urllib.parse.urlencode(
        {"type": "d", "q": query})
    try:
        d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=25))
        return [{"case": r.get("caseName"), "docket": r.get("docketNumber"),
                 "court": r.get("court"), "filed": r.get("dateFiled"),
                 "terminated": r.get("dateTerminated"), "nature": r.get("suitNature")}
                for r in (d.get("results") or [])[:5]]
    except Exception:
        return []


def novelty_screen(candidates: list[tuple[str, str]]) -> dict:
    """candidates: [(label, name)] for the entity + key members. Heuristic novelty
    verdict + raw hits for human judgment. Matching requires ALL distinctive name
    tokens (per roster entry / per CourtListener caseName), not any single token,
    to cut false positives; sealed cases stay invisible."""
    entries = _roster_entries()
    hits: list[dict] = []
    for label, name in candidates:
        if not name:
            continue
        if any(_name_matches(name, e) for e in entries):
            hits.append({"who": f"{label}: {name}", "source": "2026 takedown roster"})
        for c in _courtlistener(f'"{name}"'):
            if _name_matches(name, str(c.get("case") or "").lower()):
                hits.append({"who": f"{label}: {name}", "source": "CourtListener", **c})
    verdict = ("GOV-KNOWN — public/charged docket or takedown hit (value discount)"
               if hits else
               "no public case found — potentially NOVEL (NB: sealed cases are invisible)")
    return {"verdict": verdict, "hits": hits}


# ── orchestration ────────────────────────────────────────────────────────────
def enrich_ring(npi: str, year: int = 2024) -> dict:
    npi = str(npi).strip()
    dac = _dac_entity(npi)
    rea = _reassignment_entity(npi)

    member_npis = sorted(set(dac.get("members", [])) | set(rea.get("members", [])) | {npi})
    ranks, panel_n = _panel_ranks(member_npis, year)

    def _anom(n: str) -> bool:
        r = ranks.get(n)
        return bool(r and r["pct"] <= ANOMALY_PCT)

    members = []
    for n in member_npis:
        r = ranks.get(n, {})
        members.append({"npi": n, "is_seed": n == npi, "anomalous": _anom(n),
                        "rank": r.get("rank"), "pct": r.get("pct"),
                        "name": r.get("name"), "specialty": r.get("specialty"),
                        "state": r.get("state"), "pay": r.get("pay")})
    members.sort(key=lambda m: (m["rank"] is None, m["rank"] or 0))
    n_anom = sum(1 for m in members if m["anomalous"])

    # Collectible entity: prefer the reassignment vehicle (money flows there),
    # else the DAC group. This is the target candidate — not the rendering cog.
    entities = ([{"kind": "reassignment_vehicle", **g} for g in rea.get("groups", [])] +
                [{"kind": "dac_group", **g} for g in dac.get("groups", [])])
    entity_names = [(e["kind"], e.get("name")) for e in entities if e.get("name")]
    seed_name = (ranks.get(npi) or {}).get("name")
    novelty = novelty_screen(entity_names + ([("seed_provider", seed_name)] if seed_name else []))

    return {
        "seed_npi": npi,
        "seed": ranks.get(npi),
        "panel_year": year,
        "panel_n": panel_n,
        "entities": entities,
        "ring_members": members,
        "n_ring_members": len(members),
        "n_anomalous": n_anom,
        "ring_flag": n_anom >= 2,
        "collectible_entity": (entities[0] if entities else None),
        "novelty": novelty,
        "notes": [x for x in (dac.get("_error"), rea.get("_error")) if x],
    }


def _fmt(res: dict) -> str:
    L = []
    s = res.get("seed") or {}
    L.append(f"═══ RING ENRICHMENT — NPI {res['seed_npi']}  {s.get('name','(not in panel)')}"
             f"{'  ['+s['specialty']+', '+str(s.get('state'))+']' if s.get('specialty') else ''} ═══")
    if res["seed"]:
        L.append(f"  seed ranker rank: #{s['rank']} / {res['panel_n']}  (top {s['pct']*100:.2f}%)")
    if res["notes"]:
        L.append("  source notes: " + " · ".join(res["notes"]))
    L.append("\n— ENTITIES (candidate collectible defendants) —")
    if not res["entities"]:
        L.append("  (none found — no DAC Org_PAC_ID or PECOS reassignment group)")
    for e in res["entities"]:
        pid = e.get("group_pac_id") or e.get("org_pac_id") or "—"
        L.append(f"  [{e['kind']}] {e.get('name','—')}  (PAC {pid}"
                 f"{', '+e.get('state') if e.get('state') else ''})")
    L.append(f"\n— RING ({res['n_anomalous']} anomalous of {res['n_ring_members']} members; "
             f"RING_FLAG={res['ring_flag']}) —")
    for m in res["ring_members"][:20]:
        tag = "◉ANOM" if m["anomalous"] else ("·" if m["rank"] else "  (not in panel)")
        rk = f"#{m['rank']}" if m["rank"] else "—"
        L.append(f"  {tag:8} {m['npi']}  {m.get('name') or '':28.28}  {rk:>8}  "
                 f"{(m.get('specialty') or '')[:22]}{'  [SEED]' if m['is_seed'] else ''}")
    if len(res["ring_members"]) > 20:
        L.append(f"  … +{len(res['ring_members'])-20} more")
    L.append(f"\n— NOVELTY —  {res['novelty']['verdict']}")
    for h in res["novelty"]["hits"][:8]:
        extra = (f"{h.get('case','')} [{h.get('docket','')} {h.get('court','')} "
                 f"term {h.get('terminated')}]") if h.get("source") == "CourtListener" else ""
        L.append(f"  • {h['who']} — {h['source']} {extra}")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ring-enrichment: individual → company/ring → collectibility + novelty")
    ap.add_argument("--npi", required=True, help="10-digit rendering-provider NPI (the flagged individual)")
    ap.add_argument("--year", type=int, default=2024, help="ranker panel year for the ring score (default 2024)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)
    if not (len(args.npi.strip()) == 10 and args.npi.strip().isdigit()):
        print(f"ERROR: --npi must be a 10-digit NPI; got {args.npi!r}", file=sys.stderr)
        return 2
    res = enrich_ring(args.npi, args.year)
    print(json.dumps(res, indent=2, default=str) if args.format == "json" else _fmt(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
