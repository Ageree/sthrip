# sthrip-payment-tee — GCP Confidential VM payment-service deploy

> **Sprint 5 boundary**: this directory ships the artefacts. **No live GCP
> resources are created in Sprint 5.** Sprint 6 wires the Railway proxy.
> Sprint 7 wires real AMD SEV-SNP attestation and the production cutover.

The TEE payment service is a minimal FastAPI app that runs the payment hot
path inside a GCP Confidential VM (AMD SEV-SNP). Everything outside the
payment hot path stays on Railway.

## Why a TEE

Sthrip is custodial: the hub holds plaintext payment metadata in RAM during
routing. The Phase 3 plan moves only the payment routing logic into a
hardware-isolated TEE so a compromised host cannot read participant
identifiers, amounts, or memos. See `.harness/phase2-money-and-tee/lead-decisions.md`
for the architectural decision record.

## What's in this directory

```
payment_service.py      FastAPI app: /health, /attestation, /v2/payments/hub-routing
import_guard.py         Boot-time check that no forbidden module is loaded
Dockerfile              Multi-stage, non-root, exposes 8080
.dockerignore           Excludes marketplace, escrow, MCP, admin UI, tests, etc.
requirements.txt        Pinned runtime deps (fastapi, sqlalchemy, cryptography, ...)
setup-vm.sh             Idempotent provisioning script (supports --dry-run)
teardown-vm.sh          Counterpart cleanup script
mtls/generate-certs.sh  Mints CA, server cert, client cert via openssl
mtls/README.md          Cert distribution + rotation runbook
README.md               This file
```

The Python package is `gcp.payment_tee_deploy` (underscore form) to avoid
Python's hyphen-in-import limitation. The deploy artefacts (Dockerfile,
shell scripts, certs) live in the same directory.

## Prerequisites

* `gcloud` SDK installed and authenticated (`gcloud auth login`).
* GCP project with billing enabled. Default name: `sthrip-tee` (override
  via `GCP_PROJECT`).
* Confidential Compute API enabled:
  ```bash
  gcloud services enable compute.googleapis.com confidentialcomputing.googleapis.com \
      --project=sthrip-tee
  ```
* IAM role `Compute Admin` on the project for the operator account.
* Container registry chosen and authenticated (Artifact Registry recommended).

## Quick start (dry-run only — Sprint 5 stops here)

```bash
cd gcp/payment_tee_deploy

# 1. Inspect the planned gcloud commands without executing.
GCP_PROJECT=sthrip-tee \
GCP_ZONE=us-central1-a \
VM_NAME=sthrip-payment-tee \
bash setup-vm.sh --dry-run

# 2. Generate mTLS certs (run on a SECURE host).
cd mtls
bash generate-certs.sh
ls certs/   # ca.crt + server.{crt,key} + client.{crt,key}
```

## Live deploy (operator action item — Sprint 6+)

```bash
# 1. Build and push the image.
docker build \
    -t us-central1-docker.pkg.dev/sthrip-tee/payment-tee/sthrip-payment-tee:latest \
    -f gcp/payment_tee_deploy/Dockerfile \
    .
docker push us-central1-docker.pkg.dev/sthrip-tee/payment-tee/sthrip-payment-tee:latest

# 2. Provision the VM (creates static IP + Confidential VM + startup-script).
GCP_PROJECT=sthrip-tee \
GCP_ZONE=us-central1-a \
VM_NAME=sthrip-payment-tee \
IMAGE_NAME=us-central1-docker.pkg.dev/sthrip-tee/payment-tee/sthrip-payment-tee:latest \
bash setup-vm.sh

# 3. Verify the VM is up and serving.
gcloud compute ssh sthrip-payment-tee \
    --project=sthrip-tee --zone=us-central1-a \
    --command='curl -sf http://localhost:8080/health'
```

## Viewing logs

```bash
# Startup-script logs (Docker install, image pull, container launch).
gcloud compute ssh sthrip-payment-tee \
    --project=sthrip-tee --zone=us-central1-a \
    --command='sudo journalctl -u google-startup-scripts.service -f'

# Application logs.
gcloud compute ssh sthrip-payment-tee \
    --project=sthrip-tee --zone=us-central1-a \
    --command='docker logs -f sthrip-payment-tee'
```

## Cost expectation

| Resource              | Type                | Approx monthly USD |
|-----------------------|---------------------|--------------------|
| Confidential VM       | n2d-standard-2      | $50–60             |
| Persistent SSD (20 GB)| pd-ssd              | $4                 |
| Static external IP    | Regional            | $3                 |
| Egress to Railway     | ~10 GB/mo           | $1                 |
| **Total**             |                     | **$58–70/mo**      |

Confidential VMs add ~10% on top of standard n2d pricing. See
`lead-decisions.md` for the cost analysis behind the GCP-over-AWS-Nitro choice.

## Idempotency

`setup-vm.sh` is safe to re-run. It probes `gcloud compute instances describe`
first and short-circuits with "already exists" if the VM is present. To force
a rebuild, run `teardown-vm.sh` (preserves the static IP via `--keep-ip`)
then `setup-vm.sh` again.

## Trust boundary

The payment service refuses to boot if any non-payment module is loaded into
the Python interpreter. The check lives in
[`import_guard.py`](./import_guard.py) and runs at module import time.

The forbidden list mirrors the test-suite assertion in
`tests/test_payment_service_self_contained.py` — keep them in sync. Forbidden
substrings include `marketplace`, `admin_ui`, `subscription_billing_service`,
`escrow_service`, every non-payment router, and the MCP integration.

A handful of legacy modules (e.g. the swap atomic-swap helpers) ARE allowed
even though their names brush forbidden tokens — they get loaded eagerly by
`sthrip/__init__.py` and contain no business logic. Those carve-outs are
listed in `import_guard.ALLOWED_OVERRIDES`.

## Sprint roadmap

| Sprint | Scope                                           | Status        |
|--------|-------------------------------------------------|---------------|
| 5      | Deploy artefacts, runbook, mTLS scripts          | this sprint   |
| 6      | Railway proxy, gRPC client, DNS, secret loading | next          |
| 7      | Real SEV-SNP attestation `/attestation`, cutover| pending       |

After Sprint 7, the `/attestation` endpoint returns a real AMD SEV-SNP
report including the image digest. The SDK will verify against the
`sthrip_sdk.attestation_anchors` known-good hash list.

## Troubleshooting

### `setup-vm.sh` says "already exists" but I want a fresh VM
Run `bash teardown-vm.sh` first, then re-run `setup-vm.sh`.

### `docker pull` fails on first boot
The startup script uses metadata-server credentials. Ensure the VM service
account has the `roles/artifactregistry.reader` role on the registry
project.

### `/health` returns 502 from Railway proxy
Sprint 6 introduces the Railway proxy. Until then, `/health` is reachable
only from inside the VPC. SSH into the VM to test locally.

### Forbidden module crash at boot
Inspect the error: it lists the offending modules. Either:
  - Add the module to `ALLOWED_OVERRIDES` (only if it's a benign repo
    loaded by `sthrip/__init__.py`),
  - Or remove the import from the payment hot path.

Never relax the `FORBIDDEN_SUBSTRINGS` list to silence the guard — the
guard is the last line of defence on the trust boundary.
