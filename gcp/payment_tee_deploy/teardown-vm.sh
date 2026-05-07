#!/usr/bin/env bash
#
# teardown-vm.sh — Destroy the GCP Confidential VM provisioned by setup-vm.sh.
#
# Idempotent: re-running after the VM is gone exits 0 with a no-op.
#
# Usage:
#   GCP_PROJECT=sthrip-tee \
#   GCP_ZONE=us-central1-a \
#   VM_NAME=sthrip-payment-tee \
#   bash teardown-vm.sh [--dry-run] [--keep-ip]
#
# --keep-ip preserves the static external IP so a subsequent setup-vm.sh
# brings the service back up at the same address (useful for rolling
# upgrades).
#
set -euo pipefail

GCP_PROJECT="${GCP_PROJECT:-sthrip-tee}"
GCP_REGION="${GCP_REGION:-us-central1}"
GCP_ZONE="${GCP_ZONE:-us-central1-a}"
VM_NAME="${VM_NAME:-sthrip-payment-tee}"
STATIC_IP_NAME="${STATIC_IP_NAME:-${VM_NAME}-ip}"

DRY_RUN=0
KEEP_IP=0
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY_RUN=1 ;;
        --keep-ip)    KEEP_IP=1 ;;
        --help|-h)
            grep '^#' "$0" | sed 's/^#//' | head -20
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 64
            ;;
    esac
done

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '+ '
        printf '%q ' "$@"
        printf '\n'
    else
        "$@"
    fi
}

echo "==> Tearing down ${VM_NAME} (project=${GCP_PROJECT}, zone=${GCP_ZONE})"

if [ "$DRY_RUN" -eq 0 ]; then
    if gcloud compute instances describe "${VM_NAME}" \
            --project="${GCP_PROJECT}" \
            --zone="${GCP_ZONE}" \
            --format='value(name)' >/dev/null 2>&1; then
        run gcloud compute instances delete "${VM_NAME}" \
            --project="${GCP_PROJECT}" \
            --zone="${GCP_ZONE}" \
            --quiet
    else
        echo "    (instance not present — nothing to delete)"
    fi
else
    run gcloud compute instances delete "${VM_NAME}" \
        --project="${GCP_PROJECT}" \
        --zone="${GCP_ZONE}" \
        --quiet
fi

if [ "$KEEP_IP" -eq 0 ]; then
    echo "==> Releasing static external IP ${STATIC_IP_NAME}"
    if [ "$DRY_RUN" -eq 0 ]; then
        if gcloud compute addresses describe "${STATIC_IP_NAME}" \
                --project="${GCP_PROJECT}" \
                --region="${GCP_REGION}" \
                --format='value(name)' >/dev/null 2>&1; then
            run gcloud compute addresses delete "${STATIC_IP_NAME}" \
                --project="${GCP_PROJECT}" \
                --region="${GCP_REGION}" \
                --quiet
        else
            echo "    (address not present — nothing to release)"
        fi
    else
        run gcloud compute addresses delete "${STATIC_IP_NAME}" \
            --project="${GCP_PROJECT}" \
            --region="${GCP_REGION}" \
            --quiet
    fi
else
    echo "==> --keep-ip set, leaving ${STATIC_IP_NAME} reserved."
fi

echo "==> Teardown complete."
