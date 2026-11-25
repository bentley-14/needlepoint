#!/usr/bin/env bash
# Label flywheel — periodic DOJ label refresh (promoted from the 2026-07 chain).
#
#   1. top-up   : rebuild the extractor input from the raw DOJ cache (new uuids
#                 only — labelgen_extract is resumable by uuid)
#   2. extract  : Haiku reads any new releases (cap-aware, 2 workers)
#   3. escalate : Sonnet re-reads flagged rows (resumable)
#   4. ground   : deterministic NPPES/surface grounding (overlay-aware)
#   5. confirm  : chunked Opus 3-gate confirm (chunk-resumable)
#   6. land     : AUTO-LAND into entity_labels.jsonl (timestamped .bak;
#                 gold_set NEVER touched) — user-authorized 2026-07-16
#   7. annotate : window-activity audit fields (training_usable)
#
# Usage:  make label-refresh            (or bash scripts/ops/label_refresh.sh)
# Serial, tmux-recommended for the LLM stages. Each stage no-ops when current.
set -o pipefail
cd "$(dirname "$0")/../.." || exit 9
set -a; [ -f .env ] && . ./.env; set +a
export PYTHONNOUSERSITE=1
PY=".venv/bin/python3"
STAMP=$(date +%Y-%m-%d)
EX="validation/curator/doj_haiku_extractions_2026-07-14.jsonl"   # canonical rolling file
ESC="${EX%.jsonl}.escalated.jsonl"
GROUNDED="validation/curator/grounded_candidates_${STAMP}.jsonl"
CONFIRMED="validation/curator/entity_labels_CONFIRMED_${STAMP}.jsonl"
LOG="state/label_refresh_${STAMP}.log"
mkdir -p state
{
echo "=== label-refresh ${STAMP} start $(date) ==="

echo "--- 1. input top-up (raw cache -> extractor input, new uuids ride the resume) ---"
"$PY" - <<'PYEOF'
import json, re
from pathlib import Path
raw = Path("data/source/doj_api/raw_press_releases.jsonl")
out = Path("/tmp/labelgen_input_full.jsonl")
def clean(h): return re.sub(r"<[^>]+>", " ", re.sub(r"\s+", " ", h or "")).strip()
seen, n = set(), 0
with out.open("w") as f:
    for line in raw.open():
        if not line.strip():
            continue
        r = json.loads(line)
        u = r.get("uuid")
        if not u or u in seen:
            continue
        seen.add(u)
        f.write(json.dumps({"uuid": u, "title": r.get("title", "") or "",
                            "url": r.get("url", "") or "",
                            "body": clean(r.get("body", ""))[:5500]}) + "\n")
        n += 1
print(f"extractor input: {n} releases")
PYEOF

echo "--- 2. extract (Haiku, resumable; no-op when current) ---"
"$PY" scripts/labelgen_extract.py /tmp/labelgen_input_full.jsonl "$EX" claude-haiku-4-5-20251001 2

echo "--- 3. escalate (Sonnet, resumable) ---"
"$PY" scripts/labelgen_escalate.py "$EX" --out "$ESC" --workers 2

echo "--- 4. ground ---"
"$PY" scripts/labelgen_ground.py "$EX" --escalated "$ESC" --out "$GROUNDED"

echo "--- 5. confirm (Opus chunks) ---"
"$PY" scripts/labelgen_confirm.py "$GROUNDED" --out "$CONFIRMED"

echo "--- 6. land (auto-land authorized; .bak + dedupe; gold_set untouched) ---"
"$PY" scripts/labelgen_land.py "$CONFIRMED"

echo "--- 7. annotate window activity (training_usable) ---"
"$PY" scripts/ops/annotate_label_activity.py || echo "(annotation script optional — skipped)"

echo "=== label-refresh done $(date): labels=$(wc -l < data/extracted/entity_labels.jsonl) ==="
} 2>&1 | tee -a "$LOG"
