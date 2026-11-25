#!/usr/bin/env python3
"""Read-only gold_set.jsonl audit — report + proposals, NEVER writes the file.

Checks (all deterministic, local):
  A. structure     — parse, required fields, exact duplicates
  B. npi_validity  — 10-digit + NPI Luhn (80840 prefix) + NPPES presence
                     + entity-type consistency vs nppes_entity_type
  C. surface       — the standing rule: every case-NPI must have SOME Medicare
                     billing surface (Part B provider-year panels, any year)
  D. years         — covered_years vs observed Part-B billing years overlap
  E. vocabulary    — outcome.type values censused (flag singletons/odd)
  F. sources       — source_url present, host census
  G. confidence    — npi_confidence / name_match distributions, worst listed
  H. contamination — battery negative_controls + adjudicated TNs must NOT be
                     in gold_set

Output: validation/goldset_audit/audit_<date>.md + findings.jsonl (proposals).
"""
from __future__ import annotations
import json, glob, re, sys, datetime
from pathlib import Path
from collections import Counter, defaultdict

import duckdb

REPO = Path(__file__).resolve().parent.parent
GOLD = REPO / "data" / "extracted" / "gold_set.jsonl"
NPPES = REPO / "data" / "registries" / "nppes_bulk.parquet"
BATTERY = REPO / "validation" / "battery" / "battery.json"
OUTDIR = REPO / "validation" / "goldset_audit"


def npi_luhn_ok(npi: str) -> bool:
    if not re.fullmatch(r"\d{10}", npi or ""):
        return False
    digits = [int(c) for c in "80840" + npi[:9]]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (total + int(npi[9])) % 10 == 0


def main() -> int:
    recs = [json.loads(l) for l in GOLD.open() if l.strip()]
    findings: list[dict] = []
    def flag(sev, check, npi, case_id, msg):
        findings.append({"severity": sev, "check": check, "npi": npi,
                         "case_id": case_id, "detail": msg})

    # A. structure
    seen = Counter((r.get("case_id"), str(r.get("npi"))) for r in recs)
    for k, n in seen.items():
        if n > 1:
            flag("HIGH", "duplicate", k[1], k[0], f"exact (case_id,npi) appears {n}x")
    npis = sorted({str(r.get("npi")) for r in recs})

    # B. npi validity + NPPES
    bad_luhn = [n for n in npis if not npi_luhn_ok(n)]
    for n in bad_luhn:
        flag("HIGH", "npi_luhn", n, None, "fails NPI check-digit (80840 Luhn)")
    con = duckdb.connect()
    vals = ",".join("('%s')" % n for n in npis)
    con.execute(f"CREATE TEMP TABLE g(npi VARCHAR); INSERT INTO g VALUES {vals}")
    npcols = [c for c in con.execute(f"SELECT * FROM read_parquet('{NPPES}') LIMIT 0").df().columns]
    npi_col = next(c for c in npcols if c.lower() in ("npi",))
    ent_col = next((c for c in npcols if "entity" in c.lower()), None)
    sel_ent = f", ANY_VALUE(n.\"{ent_col}\") ent" if ent_col else ""
    hit = con.execute(f"""SELECT g.npi{sel_ent} FROM g JOIN read_parquet('{NPPES}') n
                          ON CAST(n."{npi_col}" AS VARCHAR)=g.npi GROUP BY g.npi""").df()
    present = set(hit.npi)
    for n in npis:
        if n not in present:
            flag("MED", "nppes_missing", n, None, "not in NPPES bulk (deactivated or bad NPI)")
    ent_by_npi = dict(zip(hit.npi, hit.ent)) if ent_col else {}
    if ent_col:
        for r in recs:
            n = str(r.get("npi")); want = str(r.get("nppes_entity_type") or "")
            got = str(ent_by_npi.get(n, ""))
            if want and got and want[:1] != got[:1]:
                flag("MED", "entity_type_mismatch", n, r.get("case_id"),
                     f"gold_set says {want!r}, NPPES says {got!r}")

    # C+D. Medicare surface + year overlap (Part B provider panels, all local years)
    panels = sorted(glob.glob(str(REPO / "data/panels/cms_partb_20*.parquet")))
    import os
    cache = Path(os.path.expanduser("~/.cache/fraud-detection/r2_tables"))
    have = {re.search(r"(\d{4})", Path(p).name).group(1) for p in panels}
    for p in sorted(glob.glob(str(cache / "cms_partb_20*.parquet"))):
        y = re.search(r"(\d{4})", Path(p).name).group(1)
        if y not in have and "svc" not in Path(p).name:
            panels.append(p); have.add(y)
    panels = [p for p in panels if "svc" not in Path(p).name]
    # Part D + DMEPOS surfaces too — a prescriber- or DME-fraud defendant has no
    # Part-B lines at all; "no surface" must mean no B AND no D AND no DME.
    extra = []
    for prog, col in (("cms_partd", "Prscrbr_NPI"), ("cms_dmepos", "Rfrg_NPI")):
        for pp in sorted(glob.glob(str(cache / f"{prog}_20*.parquet"))) + sorted(glob.glob(str(REPO / f"data/panels/{prog}_20*.parquet"))):
            if "detailed" in Path(pp).name: continue
            extra.append((pp, col))
    years_by_npi: dict[str, set] = defaultdict(set)
    other_surface: dict[str, set] = defaultdict(set)
    for p in panels:
        y = int(re.search(r"(\d{4})", Path(p).name).group(1))
        d = con.execute(f"SELECT DISTINCT CAST(Rndrng_NPI AS VARCHAR) n FROM read_parquet('{p}') WHERE Rndrng_NPI IN (SELECT npi FROM g)").df()
        for n in d.n:
            years_by_npi[n].add(y)
    for pp, col in extra:
        try:
            cols = [c for c in con.execute(f"SELECT * FROM read_parquet('{pp}') LIMIT 0").df().columns]
            cc = col if col in cols else next((c for c in cols if c.lower().endswith("npi")), None)
            if not cc: continue
            d = con.execute(f'SELECT DISTINCT CAST("{cc}" AS VARCHAR) n FROM read_parquet(\'{pp}\') WHERE CAST("{cc}" AS VARCHAR) IN (SELECT npi FROM g)').df()
            tag = "partd" if "partd" in Path(pp).name else "dmepos"
            for n in d.n:
                other_surface[n].add(tag)
        except Exception:
            continue
    yr_min, yr_max = 2013, max(int(re.search(r"(\d{4})", Path(p).name).group(1)) for p in panels)
    for r in recs:
        n = str(r.get("npi"))
        billed = years_by_npi.get(n, set())
        cov = r.get("covered_years") or []
        cov = [int(c) for c in cov if str(c).isdigit()]
        if not billed:
            oth = other_surface.get(n, set())
            if oth:
                flag("LOW", "non_partb_surface", n, r.get("case_id"),
                     f"no Part-B lines but has {sorted(oth)} surface — fine (prescriber/DME scheme)")
            else:
                cov_in_range = [c for c in cov if yr_min <= c <= yr_max]
                sev = "HIGH" if cov_in_range else "LOW"
                flag(sev, "no_medicare_surface", n, r.get("case_id"),
                     f"zero Part-B/D/DMEPOS surface {yr_min}-{yr_max}"
                     + (f" though covered_years={cov_in_range} overlap the panel window" if cov_in_range
                        else f" (covered_years={cov or '?'} predate panels — informational)"))
        elif cov:
            overlap = billed & set(cov)
            if not overlap and min(cov) <= yr_max and max(cov) >= yr_min:
                flag("MED", "covered_years_disjoint", n, r.get("case_id"),
                     f"covered_years {sorted(cov)} vs billed years {sorted(billed)} — no overlap")

    # E. outcome vocabulary
    otypes = Counter()
    for r in recs:
        o = r.get("outcome")
        otypes[(o.get("type") if isinstance(o, dict) else str(o))] += 1
    for t, cnt in otypes.items():
        if cnt <= 2:
            flag("LOW", "outcome_vocab_rare", None, None, f"outcome.type {t!r} appears only {cnt}x — verify canonical")

    # F. sources
    for r in recs:
        u = str(r.get("source_url") or "")
        if not u.startswith("http"):
            flag("MED", "source_url_missing", str(r.get("npi")), r.get("case_id"), f"source_url={u!r}")
    hosts = Counter(re.sub(r"^https?://([^/]+)/.*$", r"\1", str(r.get("source_url"))) for r in recs)

    # G. confidence
    lowconf = [r for r in recs if isinstance(r.get("npi_confidence"), (int, float)) and r["npi_confidence"] < 0.8]
    for r in sorted(lowconf, key=lambda x: x["npi_confidence"])[:40]:
        flag("MED", "low_npi_confidence", str(r.get("npi")), r.get("case_id"),
             f"npi_confidence={r['npi_confidence']} name_match={str(r.get('name_match'))[:60]}")

    # H. contamination
    bat = json.load(BATTERY.open())["subjects"]
    gset = set(npis)
    for s in bat:
        if s.get("kind") in ("negative_control", "adjudicated_true_negative") and str(s.get("npi")) in gset:
            flag("HIGH", "contamination", str(s.get("npi")), None,
                 f"battery {s['kind']} {s.get('name')!r} is ALSO in gold_set")

    # report
    OUTDIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    sev_rank = {"HIGH": 0, "MED": 1, "LOW": 2}
    findings.sort(key=lambda f: sev_rank[f["severity"]])
    with (OUTDIR / f"findings_{today}.jsonl").open("w") as f:
        for x in findings:
            f.write(json.dumps(x) + "\n")
    n_by = Counter(f["check"] for f in findings)
    s_by = Counter(f["severity"] for f in findings)
    lines = [f"# Gold-set audit — {today} (read-only; proposals, no writes)",
             "",
             f"records: {len(recs)} | unique NPIs: {len(npis)} | NPPES present: {len(present)}/{len(npis)}",
             f"Part-B panel years checked: {yr_min}-{yr_max} ({len(panels)} panels)",
             f"findings: {len(findings)}  (HIGH {s_by.get('HIGH',0)} / MED {s_by.get('MED',0)} / LOW {s_by.get('LOW',0)})",
             "", "| check | count |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in n_by.most_common()]
    lines += ["", "## outcome.type census", ""]
    lines += [f"- {t!r}: {c}" for t, c in otypes.most_common()]
    lines += ["", "## source hosts", ""]
    lines += [f"- {h}: {c}" for h, c in hosts.most_common(8)]
    lines += ["", "## top findings (HIGH first, max 50)", ""]
    for x in findings[:50]:
        lines.append(f"- **{x['severity']}** `{x['check']}` npi={x['npi']} case={x['case_id']}: {x['detail']}")
    (OUTDIR / f"audit_{today}.md").write_text("\n".join(lines))
    print("\n".join(lines[:22]))
    print(f"\nfull report -> {OUTDIR}/audit_{today}.md")
    print(f"findings    -> {OUTDIR}/findings_{today}.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
