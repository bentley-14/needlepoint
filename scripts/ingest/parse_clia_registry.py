"""Parse the CMS CLIA Annual Laboratory Registry PDFs into a queryable parquet.

Source: https://www.cms.gov/medicare/quality/clinical-laboratory-improvement-amendments/laboratory-registry
Annual PDFs 2013-2024 listing CLIA-certified laboratories that have been
sanctioned (suspended, limited, revoked, cancelled) or convicted of
fraud/abuse/false-billing/kickbacks per 42 CFR 493.1850 (Section 353(n) of
the Public Health Service Act).

Each PDF entry has:
  - Director name + credentials
  - Lab name
  - Street address (sometimes with suite)
  - City, State Zip
  - CLIA ID# (10-character alphanumeric, e.g., 01D0303841)
  - SANCTION (type — may be multi-line for compound sanctions)
  - EFFECTIVE DATE (may be multi-date for compound sanctions)
  - REASON (free text, multi-line)
  - STATUS (free text, multi-line, sometimes absent)

We anchor on `CLIA ID#` (every entry has exactly one) and reconstruct the
fields by scanning before/after. Output: parquet with one row per entry.

Usage:
    python scripts/ingest/parse_clia_registry.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pdfplumber

REPO_ROOT = Path(__file__).resolve().parents[2]
PDF_DIR = REPO_ROOT / "data" / "raw" / "clia_registry"
OUT_PATH = REPO_ROOT / "data" / "processed" / "clia_disciplinary.parquet"

CLIA_ID_RE = re.compile(r"CLIA ID#\s*([0-9A-Z]+)", re.IGNORECASE)
SECTION_RE = re.compile(
    r"^\s*(\d+)\.\s+(LABORATORIES|REINSTATED|OTHER)",
    re.IGNORECASE | re.MULTILINE,
)
ZIP_RE = re.compile(r",\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$")


def extract_all_text(pdf_path: Path) -> str:
    """Concatenate text from every page, preserving order and a page-break marker."""
    chunks: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            chunks.append(text)
    return "\n".join(chunks)


def parse_section_header(line: str) -> str | None:
    """Detect a section-header line: '1. LABORATORIES ... CONVICTED ...'.

    Section headers ALL contain the word "LABORATORIES" and have a number
    prefix in most years (2013 sometimes omits the number). We require
    "LABORATORIES" to be present to avoid false matches on STATUS text
    that mentions "REINSTATED" / "OTHER".

    Returns a normalized section label or None.
    """
    line = line.strip()
    rest = line.upper()
    if "LABORATORIES" not in rest:
        return None
    # Classify by trailing description
    if "CONVICTED" in rest or "FRAUD" in rest or "KICKBACK" in rest or "FALSE BILLING" in rest:
        return "convicted_fraud_abuse"
    if "SUSPENDED" in rest or "LIMITED" in rest or "REVOKED" in rest or "ADVERSE ACTION" in rest:
        return "certificate_action"
    if "REINSTATED" in rest:
        return "reinstated"
    if "OWNERS" in rest or "DIRECTORS" in rest or "OTHER" in rest:
        return "other"
    return None


def parse_one_pdf(pdf_path: Path, year: int) -> list[dict]:
    """Parse a single PDF into a list of entry dicts.

    Strategy: anchor on each `CLIA ID#` occurrence, then walk backwards for
    director/lab/address and forwards for SANCTION/EFFECTIVE DATE/REASON/STATUS.
    """
    full = extract_all_text(pdf_path)
    lines = [ln.rstrip() for ln in full.splitlines()]

    # First, find the section transitions by scanning forward through lines
    section_for_line: list[str] = []
    current_section: str = "unknown"
    for ln in lines:
        s = parse_section_header(ln)
        if s is not None:
            current_section = s
        section_for_line.append(current_section)

    # Find every CLIA ID# anchor (one per entry)
    entries: list[dict] = []
    for i, ln in enumerate(lines):
        m = CLIA_ID_RE.search(ln)
        if not m:
            continue
        clia_id = m.group(1).strip()

        # ---- walk backwards for address / lab / director ----
        # Director line contains ", DIRECTOR" (with optional credentials before;
        # in older PDFs the lab name's first word may bleed onto this line, e.g.
        # "ZONG LANG TSAI MD, DIRECTOR DEL" where DEL is the start of the lab
        # name. We strip everything after DIRECTOR and recover the bleed-over.
        director = None
        director_bleed: str | None = None
        lab_name = None
        address_lines: list[str] = []
        director_line_idx = None
        for j in range(i - 1, max(0, i - 12), -1):
            ln_j = lines[j].strip()
            if not ln_j:
                continue
            m_dir = re.search(r"^(.*?),\s*DIRECTOR\b(.*)$", ln_j, re.IGNORECASE)
            if m_dir:
                director = m_dir.group(1).strip() + ", DIRECTOR"
                trailing = m_dir.group(2).strip()
                if trailing:
                    director_bleed = trailing
                director_line_idx = j
                break

        # Lab name is the line(s) between director and address
        # Address ends with "City, ST ZIP"
        if director_line_idx is not None:
            # Find the city/state/zip line first (somewhere between director and CLIA ID#)
            for j in range(director_line_idx + 1, i):
                ln_j = lines[j].strip()
                if ZIP_RE.search(ln_j):
                    # ln_j is the city/state/zip line
                    # Lines between director_line_idx+1 and j-1 are lab_name + street
                    block = [lines[k].strip() for k in range(director_line_idx + 1, j) if lines[k].strip()]
                    # Heuristic: lab name is typically the line BEFORE the street.
                    # Street usually has a number + has DIRECTION / ST / RD / AVE / SUITE / PO BOX etc.
                    street_indices = []
                    for ki, ll in enumerate(block):
                        if re.search(r"\b\d+\b", ll) and re.search(
                            r"\b(ST|STREET|AVE|AVENUE|BLVD|BOULEVARD|RD|ROAD|DR|DRIVE|LN|LANE|WAY|CIRCLE|CIR|PLACE|PL|CT|COURT|HIGHWAY|HWY|PARKWAY|PKWY|TER|TERRACE|TRAIL|TRL|TPKE|PO BOX|SUITE|STE|FLOOR|FL|UNIT|BLDG|BUILDING|#)\b",
                            ll, re.IGNORECASE,
                        ):
                            street_indices.append(ki)
                    if street_indices:
                        first_street = street_indices[0]
                        lab_name = " ".join(block[:first_street]).strip()
                        address_lines = block[first_street:] + [ln_j]
                    else:
                        # Fallback: assume last block line before zip is street
                        if len(block) >= 2:
                            lab_name = " ".join(block[:-1]).strip()
                            address_lines = [block[-1], ln_j]
                        else:
                            lab_name = block[0] if block else None
                            address_lines = [ln_j]
                    # If director-line had bleed-over text (e.g., "DEL" from
                    # a wrapped lab name), prepend it to lab_name. Also if
                    # the lab name has a stray trailing number that looks
                    # like a street prefix (e.g., "AMO MEDICAL LAB 21350"),
                    # split it off and prepend to the street.
                    if director_bleed and lab_name:
                        lab_name = f"{director_bleed} {lab_name}".strip()
                    if lab_name:
                        m_trailnum = re.match(r"^(.*\S)\s+(\d{2,6})\s*$", lab_name)
                        if m_trailnum and address_lines:
                            lab_name = m_trailnum.group(1).strip()
                            address_lines[0] = f"{m_trailnum.group(2)} {address_lines[0]}"
                    break

        # ---- walk forwards for SANCTION / EFFECTIVE DATE / REASON / STATUS ----
        sanction = None
        effective_date = None
        reason = None
        status = None
        current_field: str | None = None
        field_buffers: dict[str, list[str]] = {
            "SANCTION": [],
            "EFFECTIVE DATE": [],
            "REASON": [],
            "STATUS": [],
        }
        next_entry_at = i + 12  # cap how far forward we look
        for j in range(i + 1, min(len(lines), next_entry_at)):
            ln_j = lines[j].strip()
            if not ln_j:
                continue
            # Stop at next entry's CLIA ID
            if CLIA_ID_RE.search(ln_j):
                break
            # Stop at a line ending in DIRECTOR (next entry)
            if re.search(r",\s*DIRECTOR\b", ln_j, re.IGNORECASE):
                break
            # Detect a field header
            m_field = re.match(r"^(SANCTION|EFFECTIVE DATE|REASON|STATUS):\s*(.*)$", ln_j, re.IGNORECASE)
            if m_field:
                current_field = m_field.group(1).upper()
                rest = m_field.group(2).strip()
                if rest:
                    field_buffers[current_field].append(rest)
                continue
            # Continuation of current field
            if current_field:
                field_buffers[current_field].append(ln_j)

        if field_buffers["SANCTION"]:
            sanction = " | ".join(field_buffers["SANCTION"])
        if field_buffers["EFFECTIVE DATE"]:
            effective_date = " | ".join(field_buffers["EFFECTIVE DATE"])
        if field_buffers["REASON"]:
            reason = " ".join(field_buffers["REASON"])
        if field_buffers["STATUS"]:
            status = " ".join(field_buffers["STATUS"])

        # Parse address
        street = " ".join(address_lines[:-1]) if len(address_lines) > 1 else None
        city = state = zipc = None
        if address_lines:
            last_addr = address_lines[-1]
            m_zip = ZIP_RE.search(last_addr)
            if m_zip:
                state = m_zip.group(1)
                zipc = m_zip.group(2)
                # city is what's before the ", ST ZIP" portion
                city = last_addr[: m_zip.start()].rstrip(", ").strip()

        section = section_for_line[i] if i < len(section_for_line) else "unknown"

        entries.append({
            "year": year,
            "section": section,
            "clia_id": clia_id,
            "director": director,
            "lab_name": lab_name,
            "street": street,
            "city": city,
            "state": state,
            "zip": zipc,
            "sanction": sanction,
            "effective_date": effective_date,
            "reason": reason,
            "status": status,
            "source_pdf": pdf_path.name,
        })

    return entries


def main() -> int:
    print(f"=== Parsing CLIA disciplinary registry PDFs from {PDF_DIR} ===")
    all_entries: list[dict] = []
    for pdf_path in sorted(PDF_DIR.glob("*.pdf")):
        year_match = re.match(r"(\d{4})", pdf_path.stem)
        if not year_match:
            print(f"  skipping {pdf_path.name} (no year in name)")
            continue
        year = int(year_match.group(1))
        try:
            entries = parse_one_pdf(pdf_path, year)
            print(f"  {pdf_path.name}: parsed {len(entries)} entries")
            all_entries.extend(entries)
        except Exception as e:
            print(f"  {pdf_path.name}: ERROR {e}")

    if not all_entries:
        print("ERROR: no entries parsed")
        return 1

    # Write parquet
    try:
        import pandas as pd
    except ImportError:
        print("pandas not available — falling back to JSONL")
        import json
        out = OUT_PATH.with_suffix(".jsonl")
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w") as f:
            for e in all_entries:
                f.write(json.dumps(e) + "\n")
        print(f"Wrote {out} ({len(all_entries)} entries)")
        return 0

    df = pd.DataFrame(all_entries)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT_PATH, index=False)
    print(f"\n=== Wrote {OUT_PATH}: {len(df):,} entries across {df['year'].nunique()} years ===")

    # Print quick stats
    print("\n--- Per-year entry counts ---")
    print(df.groupby("year").size())
    print("\n--- Sections ---")
    print(df.groupby("section").size())
    print("\n--- Top sanctions ---")
    print(df["sanction"].value_counts().head(8))
    print("\n--- Top reasons (truncated) ---")
    print(df["reason"].str[:80].value_counts().head(8))

    # Sanity-check a known entry
    print("\n--- Sanity check: first parsed entry ---")
    print(df.iloc[0].to_dict())

    return 0


if __name__ == "__main__":
    sys.exit(main())
