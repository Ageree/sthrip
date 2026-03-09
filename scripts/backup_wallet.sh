#!/bin/bash
set -euo pipefail

# Wallet Backup Script
# Exports key images, copies wallet files, encrypts and uploads to S3.
#
# Required env vars:
#   RPC_USER, RPC_PASS        — wallet-rpc digest auth credentials
#   BACKUP_PASSPHRASE         — GPG symmetric encryption passphrase
#   BACKUP_BUCKET             — S3 bucket name for offsite storage
#
# Optional env vars:
#   RPC_URL                   — wallet-rpc endpoint (default: http://localhost:18082)
#   WALLET_DIR                — wallet files location (default: /data/wallets)
#   AWS_DEFAULT_REGION        — S3 region

RPC_URL="${RPC_URL:-http://localhost:18082}"
WALLET_DIR="${WALLET_DIR:-/data/wallets}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="/tmp/wallet_backup_${TIMESTAMP}"

# ── Validate required vars ──────────────────────────────────────────────────
for var in RPC_USER RPC_PASS BACKUP_PASSPHRASE BACKUP_BUCKET; do
    if [ -z "${!var:-}" ]; then
        echo "ERROR: ${var} is not set" >&2
        exit 1
    fi
done

cleanup() {
    rm -rf "${BACKUP_DIR}" "/tmp/wallet_backup_${TIMESTAMP}.tar.gz.gpg"
}
trap cleanup EXIT

mkdir -p "${BACKUP_DIR}"

# ── 1. Export key images via RPC ────────────────────────────────────────────
echo "Exporting key images..."
curl --fail --silent --digest \
    -u "${RPC_USER}:${RPC_PASS}" \
    "${RPC_URL}/json_rpc" \
    -d '{"jsonrpc":"2.0","id":"0","method":"export_key_images","params":{"all":true}}' \
    > "${BACKUP_DIR}/key_images.json"

echo "Key images exported."

# ── 2. Copy wallet files ───────────────────────────────────────────────────
if [ -d "${WALLET_DIR}" ]; then
    cp "${WALLET_DIR}"/sthrip* "${BACKUP_DIR}/" 2>/dev/null || true
    echo "Wallet files copied."
else
    echo "WARNING: ${WALLET_DIR} does not exist, skipping wallet file copy." >&2
fi

# ── 3. Encrypt with GPG ───────────────────────────────────────────────────
ENCRYPTED="/tmp/wallet_backup_${TIMESTAMP}.tar.gz.gpg"
tar czf - -C /tmp "wallet_backup_${TIMESTAMP}" \
    | gpg --batch --yes --symmetric --cipher-algo AES256 \
          --passphrase "${BACKUP_PASSPHRASE}" \
    > "${ENCRYPTED}"

echo "Backup encrypted: ${ENCRYPTED}"

# ── 4. Upload to S3 ────────────────────────────────────────────────────────
DEST="s3://${BACKUP_BUCKET}/wallet/${TIMESTAMP}.gpg"
aws s3 cp "${ENCRYPTED}" "${DEST}"

echo "Backup uploaded to ${DEST}"
echo "Backup complete: ${TIMESTAMP}"
