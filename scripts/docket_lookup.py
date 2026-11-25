#!/usr/bin/env python3
"""Federal docket lookup for the fraud-investigator agents.

WHY THIS EXISTS
---------------
CourtListener (our canonical court tool) only holds a bare docket *shell* for
many cases — name, date, judge — with EMPTY nature_of_suit and NO documents,
because RECAP is crowd-sourced and most dockets were never purchased into it.
Agents kept misreading "no documents in my free tools" as "the case is SEALED /
a qui tam," which manufactured phantom first-to-file (31 USC 3730(b)(5)) risk.
The PacerMonitor *public* case-summary page is free and DOES expose the
nature-of-suit, parties, counsel, judge, and recent docket entries — enough to
classify a case (e.g. Title VII employment vs. sealed qui tam) without paying.

RULE FOR AGENTS (do not violate):
  Inaccessibility in free tools is NOT evidence of a seal. A federal qui tam
  under seal has NO public docket entries and NO public answer. If you can see a
  public ANSWER, a scheduling order, or a nature-of-suit code, the case is NOT
  sealed. Only treat a case as a possible sealed qui tam if PACER's own index
  affirmatively shows it sealed — never infer it from your own access limits.

WORKFLOW
--------
  1. Confirm existence + judge/date via CourtListener:
        python3 scripts/docket_lookup.py --cl-docket 9:99-cv-00001 --court txnd
  2. Find the PacerMonitor public URL with your WebSearch tool, e.g. search
        "PacerMonitor <case name>"  ->  https://www.pacermonitor.com/public/case/<id>/<slug>
  3. Parse the public summary (nature of suit, parties, counsel, docket entries):
        python3 scripts/docket_lookup.py --url https://www.pacermonitor.com/public/case/60189598/Dogwood_v_Total_Care_2_U_LLC

Emits structured JSON to stdout. Network only; no credentials; no paid PACER.
"""
from __future__ import annotations
import argparse, json, os, re, ssl, sys, urllib.request
from pathlib import Path

_UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")}
_CTX = ssl.create_default_context()


def _load_dotenv() -> None:
    for envp in (Path(__file__).resolve().parents[1] / "fraud-investigator-v2" / ".env",
                 Path(__file__).resolve().parents[1] / ".env"):
        if envp.exists():
            for line in envp.read_text().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def _get(url: str, timeout: int = 25, retries: int = 4) -> str:
    """GET with backoff. PacerMonitor rate-limits direct fetches (HTTP 429);
    the agent's WebFetch tool is the more reliable path for that host — see the
    module docstring. This retries transient 429/503 with exponential backoff."""
    import time
    last = None
    for attempt in range(retries):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers=_UA), timeout=timeout, context=_CTX
            ).read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt * 1.5)  # 1.5, 3, 6s
                continue
            raise
    raise last  # pragma: no cover


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def parse_pacermonitor(url: str) -> dict:
    """Fetch a PacerMonitor /public/case/ page and extract case metadata."""
    html = _get(url)
    out: dict = {"source": "pacermonitor_public", "url": url}

    # Case-info table is rendered as <td class="caseInfoTableLbl">LABEL</td><td>VALUE</td>
    pairs = re.findall(
        r'caseInfoTableLbl[^>]*>\s*([^<]+?)\s*</td>\s*<td[^>]*>(.*?)</td>',
        html, re.I | re.S)
    info = {lbl.strip().rstrip(":"): _strip(val) for lbl, val in pairs}
    out["case_info"] = info
    # Normalize the high-value fields
    out["nature_of_suit"] = info.get("Cause") or info.get("Nature of Suit") or info.get("NOS") or ""
    out["court"] = info.get("Court", "")
    out["date_filed"] = info.get("Date Filed") or info.get("Case Filed") or ""
    out["judge"] = info.get("Presiding Judge") or info.get("Judge") or ""

    m = re.search(r"<title>\s*(.*?)\s*</title>", html, re.I | re.S)
    out["caption"] = _strip(m.group(1)) if m else ""

    # Docket entries: rows in the docket table (best-effort, capped)
    entries = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        txt = _strip(row)
        if re.match(r"^\d{1,4}\s+\d{1,2}/\d{1,2}/\d{2,4}", txt) or re.search(
                r"\b(Complaint|Answer|Scheduling Order|Motion|Order|Notice)\b", txt):
            if 5 < len(txt) < 400:
                entries.append(txt)
    out["docket_entries"] = entries[:25]

    # Seal / qui-tam assessment — the whole point of this tool.
    blob = (out["nature_of_suit"] + " " + " ".join(entries)).lower()
    has_public_answer = "answer" in blob
    has_nos = bool(out["nature_of_suit"])
    is_qui_tam_nos = any(k in blob for k in ("false claims", "qui tam", "31:3729", "3730"))
    if has_nos or has_public_answer or entries:
        seal = "PUBLIC_NOT_SEALED"
        why = ("Public docket data is visible (nature-of-suit and/or docket entries"
               + (", including a public Answer" if has_public_answer else "")
               + ") — a sealed qui tam would show none of this.")
    else:
        seal = "INDETERMINATE"
        why = ("No public metadata parsed from this page. This does NOT imply a seal; "
               "confirm via PACER's index before treating as sealed.")
    out["seal_assessment"] = {
        "status": seal,
        "is_fca_qui_tam_by_nos": is_qui_tam_nos,
        "rationale": why,
        "agent_rule": "Inaccessibility in free tools is NOT evidence of a seal.",
    }
    return out


def verify_courtlistener(docket: str, court: str) -> dict:
    _load_dotenv()
    tok = os.environ.get("COURTLISTENER_TOKEN", "").strip()
    url = f"https://www.courtlistener.com/api/rest/v4/dockets/?docket_number={urllib.parse.quote(docket)}"
    if court:
        url += f"&court={court}"
    req = urllib.request.Request(url, headers={**_UA, **({"Authorization": f"Token {tok}"} if tok else {})})
    data = json.loads(urllib.request.urlopen(req, timeout=25, context=_CTX).read().decode("utf-8", "replace"))
    res = data.get("results", [])
    out = {"source": "courtlistener", "authenticated": bool(tok), "n_results": len(res)}
    if res:
        r = res[0]
        out.update({
            "case_name": r.get("case_name"), "date_filed": r.get("date_filed"),
            "nature_of_suit": r.get("nature_of_suit") or "(empty in CourtListener)",
            "cause": r.get("cause") or "(empty in CourtListener)",
            "assigned_to": r.get("assigned_to_str"),
            "note": ("CourtListener confirms existence; empty nature_of_suit/cause means the "
                     "docket shell was never enriched from PACER — use --url (PacerMonitor) for classification."),
        })
    return out


def main() -> int:
    import urllib.parse  # noqa: F401 (used in verify_courtlistener)
    ap = argparse.ArgumentParser(description="Federal docket lookup (CourtListener + PacerMonitor public).")
    ap.add_argument("--url", help="PacerMonitor /public/case/ URL to fetch + parse")
    ap.add_argument("--cl-docket", help="Docket number for CourtListener verification, e.g. 9:99-cv-00001")
    ap.add_argument("--court", default="", help="CourtListener court id, e.g. txnd")
    args = ap.parse_args()
    if not args.url and not args.cl_docket:
        ap.error("provide --url and/or --cl-docket")
    result: dict = {}
    try:
        if args.cl_docket:
            result["courtlistener"] = verify_courtlistener(args.cl_docket, args.court)
        if args.url:
            result["pacermonitor"] = parse_pacermonitor(args.url)
    except Exception as e:  # never crash the agent's turn
        result["error"] = repr(e)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
