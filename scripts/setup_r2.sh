#!/usr/bin/env bash
# One-command R2 setup for collaborators.
#
# Prompts for R2 read-only credentials, writes rclone config, runs a
# first data sync. Idempotent — safe to re-run.
#
# Usage: bash scripts/setup_r2.sh

set -euo pipefail

R2_ENDPOINT="https://<ACCOUNT_ID>.r2.cloudflarestorage.com"
R2_REMOTE_NAME="r2"
CONFIG_DIR="${HOME}/.config/rclone"
CONFIG_FILE="${CONFIG_DIR}/rclone.conf"

# ---- 1. rclone --------------------------------------------------------------
if ! command -v rclone >/dev/null 2>&1; then
  echo "rclone not found — installing..."
  if command -v brew >/dev/null 2>&1; then
    brew install rclone
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y rclone
  else
    echo "Install rclone manually: https://rclone.org/install/" >&2
    exit 1
  fi
fi
echo "rclone: $(rclone version | head -1)"

# ---- 2. config --------------------------------------------------------------
if rclone listremotes 2>/dev/null | grep -q "^${R2_REMOTE_NAME}:$"; then
  echo "rclone remote '${R2_REMOTE_NAME}:' already configured — skipping."
else
  echo ""
  echo "Paste the R2 credentials the maintainer sent you."
  echo "(Secret key input is hidden — paste and press Enter.)"
  echo ""
  read -r -p "Access Key ID: " ACCESS_KEY_ID
  read -r -s -p "Secret Access Key: " SECRET_ACCESS_KEY
  echo ""

  mkdir -p "${CONFIG_DIR}"
  chmod 700 "${CONFIG_DIR}"
  touch "${CONFIG_FILE}"
  chmod 600 "${CONFIG_FILE}"

  cat >> "${CONFIG_FILE}" <<EOF

[${R2_REMOTE_NAME}]
type = s3
provider = Cloudflare
access_key_id = ${ACCESS_KEY_ID}
secret_access_key = ${SECRET_ACCESS_KEY}
region = auto
endpoint = ${R2_ENDPOINT}
EOF

  echo "Wrote rclone config to ${CONFIG_FILE}"
fi

# ---- 3. smoke test ----------------------------------------------------------
echo ""
echo "Smoke-testing bucket access..."
if ! rclone lsd "${R2_REMOTE_NAME}:" >/dev/null 2>&1; then
  echo "Failed to list buckets. Check your keys with:" >&2
  echo "  rclone lsd ${R2_REMOTE_NAME}:" >&2
  echo "If that errors with SignatureDoesNotMatch, re-run:" >&2
  echo "  rclone config   # then 'e' to edit '${R2_REMOTE_NAME}'" >&2
  exit 1
fi
echo "OK — bucket is reachable."

# ---- 4. pull ----------------------------------------------------------------
echo ""
echo "Starting data pull (~7 GB, ~20 min on a good connection)..."
echo "You can Ctrl-C and resume later with 'make pull-data' — it's incremental."
echo ""
make pull-data

echo ""
echo "Pulling run artifacts (calibration runs, case states, dossiers, pipeline runs)..."
echo "These live only in R2 (not Git); rclone copy is additive."
make pull-artifacts

echo ""
echo "Pulling ground-truth reference (HCPCS/CPT code descriptors — canonical lookup data)..."
make pull-ground-truth

echo ""
echo "Done. Verify with 'make verify-data' if you want to be sure."
