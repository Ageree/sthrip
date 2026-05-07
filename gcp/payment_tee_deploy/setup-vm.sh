#!/usr/bin/env bash
#
# setup-vm.sh — Provision the GCP Confidential VM that runs sthrip-payment-tee.
#
# Phase 3 Sprint 5 (kickoff). This script is idempotent: re-running it with
# the same VM_NAME prints "already exists" and exits 0 instead of failing.
#
# Usage:
#   GCP_PROJECT=sthrip-tee \
#   GCP_REGION=us-central1 \
#   GCP_ZONE=us-central1-a \
#   VM_NAME=sthrip-payment-tee \
#   bash setup-vm.sh [--dry-run]
#
# --dry-run prints every gcloud command without executing it (used by tests
# and for operator review before the live run).
#
# Environment defaults (override via env vars):
#   GCP_PROJECT  : sthrip-tee          (lead-decisions.md)
#   GCP_REGION   : us-central1         (mature region for Confidential Compute)
#   GCP_ZONE     : us-central1-a
#   VM_NAME      : sthrip-payment-tee
#   MACHINE_TYPE : n2d-standard-2      (AMD SEV-SNP capable, 2 vCPU/8 GiB)
#   IMAGE_FAMILY : ubuntu-2204-lts     (newer kernel, SEV-SNP guest tooling)
#   IMAGE_PROJECT: ubuntu-os-cloud
#   STATIC_IP    : sthrip-payment-tee-ip   (named regional address)
#   IMAGE_NAME   : sthrip-payment-tee:latest
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults.
# ---------------------------------------------------------------------------
GCP_PROJECT="${GCP_PROJECT:-sthrip-tee}"
GCP_REGION="${GCP_REGION:-us-central1}"
GCP_ZONE="${GCP_ZONE:-us-central1-a}"
VM_NAME="${VM_NAME:-sthrip-payment-tee}"
MACHINE_TYPE="${MACHINE_TYPE:-n2d-standard-2}"
IMAGE_FAMILY="${IMAGE_FAMILY:-ubuntu-2204-lts}"
IMAGE_PROJECT="${IMAGE_PROJECT:-ubuntu-os-cloud}"
STATIC_IP_NAME="${STATIC_IP_NAME:-${VM_NAME}-ip}"
IMAGE_NAME="${IMAGE_NAME:-sthrip-payment-tee:latest}"
NETWORK_TAG="${NETWORK_TAG:-sthrip-payment-tee}"

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY_RUN=1 ;;
        --help|-h)
            grep '^#' "$0" | sed 's/^#//' | head -40
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 64
            ;;
    esac
done

# ---------------------------------------------------------------------------
# run :: print-or-exec wrapper.
# ---------------------------------------------------------------------------
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '+ '
        printf '%q ' "$@"
        printf '\n'
    else
        "$@"
    fi
}

# Capture-output variant; in dry-run prints the command and returns success
# (the caller treats success as "instance exists / does not exist" depending
# on the call site, but in dry-run we just want the command echoed).
run_capture() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '+ '
        printf '%q ' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

echo "==> sthrip-payment-tee VM provisioning"
echo "    project : ${GCP_PROJECT}"
echo "    region  : ${GCP_REGION}"
echo "    zone    : ${GCP_ZONE}"
echo "    vm      : ${VM_NAME}"
echo "    machine : ${MACHINE_TYPE}"
echo "    image   : ${IMAGE_FAMILY} (${IMAGE_PROJECT})"
echo "    dry-run : ${DRY_RUN}"
echo

# ---------------------------------------------------------------------------
# Idempotency probe — short-circuit if the instance already exists.
# ---------------------------------------------------------------------------
echo "==> Checking whether ${VM_NAME} already exists..."

if [ "$DRY_RUN" -eq 1 ]; then
    # In dry-run we ECHO the describe call but still proceed to print the
    # rest of the script so the operator sees the full plan.
    printf '+ '
    printf '%q ' gcloud compute instances describe "${VM_NAME}" \
        --project="${GCP_PROJECT}" --zone="${GCP_ZONE}" --format=value\(name\)
    printf '\n'
    INSTANCE_EXISTS=0
else
    if gcloud compute instances describe "${VM_NAME}" \
            --project="${GCP_PROJECT}" \
            --zone="${GCP_ZONE}" \
            --format='value(name)' >/dev/null 2>&1; then
        INSTANCE_EXISTS=1
    else
        INSTANCE_EXISTS=0
    fi
fi

if [ "$INSTANCE_EXISTS" -eq 1 ]; then
    echo "==> ${VM_NAME} already exists in zone ${GCP_ZONE} — nothing to do."
    echo "    To rebuild from scratch, run teardown-vm.sh first."
    exit 0
fi

# ---------------------------------------------------------------------------
# Reserve a static external IP (regional, idempotent).
# ---------------------------------------------------------------------------
echo "==> Reserving static external IP ${STATIC_IP_NAME}..."
if [ "$DRY_RUN" -eq 0 ]; then
    if ! gcloud compute addresses describe "${STATIC_IP_NAME}" \
            --project="${GCP_PROJECT}" \
            --region="${GCP_REGION}" \
            --format='value(name)' >/dev/null 2>&1; then
        run gcloud compute addresses create "${STATIC_IP_NAME}" \
            --project="${GCP_PROJECT}" \
            --region="${GCP_REGION}"
    else
        echo "    (already reserved)"
    fi
else
    run gcloud compute addresses create "${STATIC_IP_NAME}" \
        --project="${GCP_PROJECT}" \
        --region="${GCP_REGION}"
fi

# ---------------------------------------------------------------------------
# Render the startup script (Docker install + image pull + run on port 8080).
# ---------------------------------------------------------------------------
STARTUP_SCRIPT_PATH="${TMPDIR:-/tmp}/sthrip-payment-tee-startup.sh"
cat >"${STARTUP_SCRIPT_PATH}" <<'STARTUP_EOF'
#!/usr/bin/env bash
set -euo pipefail

# Install Docker.
if ! command -v docker >/dev/null 2>&1; then
    apt-get update
    apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu jammy stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io
    systemctl enable --now docker
fi

# Pull the payment service image and run it on 8080. Image tag is provided
# via instance metadata so operator can roll forward without re-running this
# whole script.
IMAGE="$(curl -fsSL -H 'Metadata-Flavor: Google' \
    http://metadata.google.internal/computeMetadata/v1/instance/attributes/image-name \
    || echo sthrip-payment-tee:latest)"

docker pull "$IMAGE" || true

# Stop any running container before relaunching.
docker rm -f sthrip-payment-tee 2>/dev/null || true

docker run -d --restart=always \
    --name=sthrip-payment-tee \
    -p 8080:8080 \
    --read-only \
    --tmpfs /tmp \
    -e LOG_LEVEL=INFO \
    "$IMAGE"
STARTUP_EOF

# ---------------------------------------------------------------------------
# Create the Confidential VM.
# ---------------------------------------------------------------------------
echo "==> Creating Confidential VM ${VM_NAME}..."

run gcloud compute instances create "${VM_NAME}" \
    --project="${GCP_PROJECT}" \
    --zone="${GCP_ZONE}" \
    --machine-type="${MACHINE_TYPE}" \
    --confidential-compute-type=SEV_SNP \
    --maintenance-policy=TERMINATE \
    --image-family="${IMAGE_FAMILY}" \
    --image-project="${IMAGE_PROJECT}" \
    --boot-disk-size=20GB \
    --boot-disk-type=pd-ssd \
    --shielded-secure-boot \
    --shielded-vtpm \
    --shielded-integrity-monitoring \
    --address="${STATIC_IP_NAME}" \
    --tags="${NETWORK_TAG}" \
    --metadata-from-file="startup-script=${STARTUP_SCRIPT_PATH}" \
    --metadata="image-name=${IMAGE_NAME}"

echo
echo "==> Done. The VM is provisioning. View boot logs with:"
echo "    gcloud compute ssh ${VM_NAME} --project=${GCP_PROJECT} --zone=${GCP_ZONE} \\"
echo "        --command='sudo journalctl -u google-startup-scripts.service -f'"
echo
echo "Sprint 6 will wire the Railway proxy to the static IP at ${STATIC_IP_NAME}."
