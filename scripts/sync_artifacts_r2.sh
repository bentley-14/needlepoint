#!/usr/bin/env bash
# Push ALL run artifacts to R2 under r2:needlepoint-data/artifacts/, neatly organized.
#
#   artifacts/calibration/CALIBRATION-<id>/   (fraud-investigator-v2/state/calibration/)
#   artifacts/cases/CASE-<id>/                (fraud-investigator-v2/state/CASE-*/  — full state incl PDFs/transcript)
#   artifacts/investigations/<Name-date>/     (investigations/  — curated dossiers incl PDFs)
#   artifacts/pipeline_runs/run_<ts>_<id>/    (state/runs/run_*/ — orchestrator logs)
#
# POLICY: run artifacts live in R2, NOT in Git. Additive `rclone copy` (never
# deletes on the remote); deliberately never touches the data prefixes
# (cache/processed/raw/derived/results/ground_truth/archive/scraped). PDFs included
# (rclone does not honor .gitignore). Runs automatically at run completion via scripts/orchestrate_pipeline.py.
#
# Universal credentials: if rclone.conf already has an 'r2' remote, it wins.
# Otherwise the script falls back to env vars R2_ACCOUNT_ID / R2_ACCESS_KEY_ID /
# R2_SECRET_ACCESS_KEY (from the environment OR the repo-root .env file — only
# those three keys are parsed; the full file is never sourced).
# If neither is available the script exits 0 (fail-open) with one notice line.
#
# Special modes:
#   --dry-run   pass-through to rclone (verbose diff, no write)
#   --check     print "configured" or "unconfigured" and exit 0 (used by tests)
#
# Honour NP_R2_DISABLE=1 → silent exit 0 (calibration / offline).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ── NP_R2_DISABLE ────────────────────────────────────────────────────────────
if [ "${NP_R2_DISABLE:-0}" = "1" ]; then
  exit 0
fi

# ── rclone binary check ───────────────────────────────────────────────────────
if ! command -v rclone >/dev/null 2>&1; then
  if [ "${1:-}" = "--check" ]; then
    echo "unconfigured"
    exit 0
  fi
  echo "R2 sync skipped: rclone not found — artifacts stay local. See .env.example, storage section."
  exit 0
fi

# ── credential resolution ─────────────────────────────────────────────────────
# Step 1: does rclone.conf already have an 'r2' remote?
_has_r2_remote() {
  rclone listremotes 2>/dev/null | grep -qx 'r2:'
}

_setup_env_remote() {
  # Try env first, then fall back to parsing .env (only the three R2_* keys).
  local acct="${R2_ACCOUNT_ID:-}"
  local key="${R2_ACCESS_KEY_ID:-}"
  local secret="${R2_SECRET_ACCESS_KEY:-}"

  if [ -z "$acct" ] || [ -z "$key" ] || [ -z "$secret" ]; then
    local dotenv="$ROOT/.env"
    if [ -f "$dotenv" ]; then
      [ -z "$acct" ]   && acct="$(grep -E '^R2_ACCOUNT_ID=' "$dotenv" | sed 's/^R2_ACCOUNT_ID=//' | tr -d '[:space:]"'"'"'' 2>/dev/null || true)"  # strips ALL quotes/whitespace — safe for hex/base64 R2 tokens (no internal spaces); a secret containing quotes would be corrupted (opus #273 finding 2)
      [ -z "$key" ]    && key="$(grep -E '^R2_ACCESS_KEY_ID=' "$dotenv" | sed 's/^R2_ACCESS_KEY_ID=//' | tr -d '[:space:]"'"'"'' 2>/dev/null || true)"
      [ -z "$secret" ] && secret="$(grep -E '^R2_SECRET_ACCESS_KEY=' "$dotenv" | sed 's/^R2_SECRET_ACCESS_KEY=//' | tr -d '[:space:]"'"'"'' 2>/dev/null || true)"
    fi
  fi

  if [ -z "$acct" ] || [ -z "$key" ] || [ -z "$secret" ]; then
    return 1
  fi

  export RCLONE_CONFIG_R2_TYPE=s3
  export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
  export RCLONE_CONFIG_R2_REGION=auto
  export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$key"
  export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$secret"
  export RCLONE_CONFIG_R2_ENDPOINT="https://${acct}.r2.cloudflarestorage.com"
  return 0
}

if _has_r2_remote; then
  : # rclone.conf remote wins — nothing to export
elif _setup_env_remote; then
  : # env-var fallback configured
else
  if [ "${1:-}" = "--check" ]; then
    echo "unconfigured"
    exit 0
  fi
  echo "R2 sync skipped: no rclone remote 'r2' and no R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY in env/.env — artifacts stay local. See .env.example, storage section."
  exit 0
fi

# ── --check mode ──────────────────────────────────────────────────────────────
if [ "${1:-}" = "--check" ]; then
  echo "configured"
  exit 0
fi

# ── rclone copy ───────────────────────────────────────────────────────────────
RC=(rclone copy --transfers 8 --contimeout 15s --timeout 120s)
[ "${1:-}" = "--dry-run" ] && RC+=(--dry-run -v)
push(){ local src="$1" sub="$2"; [ -d "$src" ] || return 0; "${RC[@]}" "$src" "r2:needlepoint-data/artifacts/$sub/$(basename "$src")"; }

for d in "$ROOT"/fraud-investigator-v2/state/calibration/CALIBRATION-*/; do push "$d" calibration; done
for d in "$ROOT"/fraud-investigator-v2/state/CASE-*/;                  do push "$d" cases; done
for d in "$ROOT"/state/runs/run_*/;                                   do push "$d" pipeline_runs; done
for d in "$ROOT"/investigations/*/; do
  [ "$(basename "$d")" = "system-design-2026-05-03" ] && continue   # design doc, not a run artifact
  push "$d" investigations
done
echo "Done. r2:needlepoint-data/artifacts/ :"
rclone lsf r2:needlepoint-data/artifacts --dirs-only -R --max-depth 2 --contimeout 10s 2>/dev/null
