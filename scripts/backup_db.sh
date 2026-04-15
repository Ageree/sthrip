#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Sthrip Database Backup Script
#
# Creates compressed PostgreSQL backups with rotation.
# Supports local storage and optional upload to S3-compatible storage.
#
# Usage:
#   ./scripts/backup_db.sh                    # one-shot backup
#   ./scripts/backup_db.sh --cron             # install as daily cron job
#   ./scripts/backup_db.sh --restore <file>   # restore from backup
#
# Required env vars:
#   DATABASE_URL   — PostgreSQL connection string
#
# Optional env vars:
#   BACKUP_DIR            — local backup directory (default: /tmp/sthrip-backups)
#   BACKUP_RETENTION_DAYS — keep backups for N days (default: 7)
#   BACKUP_S3_BUCKET      — S3 bucket for remote backups (optional)
#   BACKUP_S3_ENDPOINT    — S3-compatible endpoint URL (optional, for Minio/R2)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/tmp/sthrip-backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/sthrip_backup_${TIMESTAMP}.sql.gz"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
err() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2; }

# ── Validate ──────────────────────────────────────────────────────────────────

if [ -z "${DATABASE_URL:-}" ]; then
    err "DATABASE_URL is not set"
    exit 1
fi

if ! command -v pg_dump &>/dev/null; then
    err "pg_dump not found — install postgresql-client"
    exit 1
fi

# ── Cron install mode ─────────────────────────────────────────────────────────

if [ "${1:-}" = "--cron" ]; then
    SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    CRON_LINE="0 3 * * * DATABASE_URL='${DATABASE_URL}' BACKUP_DIR='${BACKUP_DIR}' ${SCRIPT_PATH} >> /var/log/sthrip-backup.log 2>&1"

    if crontab -l 2>/dev/null | grep -qF "backup_db.sh"; then
        log "Cron job already exists — skipping"
    else
        (crontab -l 2>/dev/null; echo "${CRON_LINE}") | crontab -
        log "Cron job installed: daily at 03:00 UTC"
    fi
    exit 0
fi

# ── Restore mode ──────────────────────────────────────────────────────────────

if [ "${1:-}" = "--restore" ]; then
    RESTORE_FILE="${2:-}"
    if [ -z "${RESTORE_FILE}" ] || [ ! -f "${RESTORE_FILE}" ]; then
        err "Usage: $0 --restore <backup_file.sql.gz>"
        exit 1
    fi
    log "Restoring from ${RESTORE_FILE}..."
    gunzip -c "${RESTORE_FILE}" | psql "${DATABASE_URL}" --single-transaction
    log "Restore completed"
    exit 0
fi

# ── Backup ────────────────────────────────────────────────────────────────────

mkdir -p "${BACKUP_DIR}"

log "Starting backup to ${BACKUP_FILE}..."

pg_dump "${DATABASE_URL}" \
    --no-owner \
    --no-privileges \
    --format=plain \
    --verbose \
    2>/dev/null \
    | gzip > "${BACKUP_FILE}"

FILESIZE=$(du -h "${BACKUP_FILE}" | cut -f1)
log "Backup completed: ${BACKUP_FILE} (${FILESIZE})"

# ── Verify backup is not empty ────────────────────────────────────────────────

MIN_SIZE=1024  # 1KB minimum
ACTUAL_SIZE=$(stat -c%s "${BACKUP_FILE}" 2>/dev/null || stat -f%z "${BACKUP_FILE}" 2>/dev/null || echo 0)
if [ "${ACTUAL_SIZE}" -lt "${MIN_SIZE}" ]; then
    err "Backup file is suspiciously small (${ACTUAL_SIZE} bytes) — possible failure"
    rm -f "${BACKUP_FILE}"
    exit 1
fi

# ── Rotate old backups ────────────────────────────────────────────────────────

DELETED=$(find "${BACKUP_DIR}" -name "sthrip_backup_*.sql.gz" -mtime +"${BACKUP_RETENTION_DAYS}" -delete -print | wc -l)
if [ "${DELETED}" -gt 0 ]; then
    log "Rotated ${DELETED} old backup(s) (retention: ${BACKUP_RETENTION_DAYS} days)"
fi

# ── Optional S3 upload ────────────────────────────────────────────────────────

if [ -n "${BACKUP_S3_BUCKET:-}" ] && command -v aws &>/dev/null; then
    S3_ARGS=""
    if [ -n "${BACKUP_S3_ENDPOINT:-}" ]; then
        S3_ARGS="--endpoint-url ${BACKUP_S3_ENDPOINT}"
    fi
    log "Uploading to s3://${BACKUP_S3_BUCKET}/..."
    aws s3 cp ${S3_ARGS} "${BACKUP_FILE}" "s3://${BACKUP_S3_BUCKET}/sthrip_backup_${TIMESTAMP}.sql.gz"
    log "S3 upload completed"
fi

log "Backup pipeline finished successfully"
