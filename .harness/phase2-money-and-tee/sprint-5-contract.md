# Sprint 5 Contract — GCP Project + Confidential VM Template

> Phase 3 Sprint 5 — TEE migration kickoff. Artefacts only, no actual GCP deploy.
> Pre-filled by Lead from product-spec.md (AD-6) and lead-decisions.md.

## Scope statement

Build the deployable artefacts for a payment hot-path service running on GCP Confidential VM (AMD SEV-SNP). Operator runs the actual `gcloud` deploy commands later; Sprint 5 makes that runbook trivial. **No live GCP resources are created in this sprint.**

## What Generator will build

### A. Self-contained payment service

1. **`gcp/payment-tee-deploy/payment_service.py`** — minimal FastAPI app exposing ONLY the payment hot-path endpoint:
   - `/v2/payments/hub-routing` (POST) — proxies the same logic that `api/routers/payments.py::_execute_hub_transfer` does
   - `/health` (GET) — liveness probe
   - `/attestation` (GET) — placeholder returning `{"status": "stub", "todo": "Sprint 7 wires real SEV-SNP attestation"}`
   - Imports MUST be limited to: payment-related modules (`transaction_repo.create_with_commission`, `envelope_crypto`, `payment_envelope_writer`, `balance_repo`, `agent_stats_service.record_transaction`, `fee_calculator`).
   - Imports MUST NOT include: marketplace, admin UI, escrow, MCP, SLA, reviews, channels, swaps, stablecoins, Tor sidecar.

2. **`gcp/payment-tee-deploy/import_guard.py`** — runtime check at boot: scans loaded modules and asserts no forbidden modules are imported. Logs the import graph to stderr at startup.

### B. Dockerfile

1. **`gcp/payment-tee-deploy/Dockerfile`**:
   - Base: `python:3.9-slim` (matches main project)
   - Multi-stage: builder stage installs deps; runtime stage copies only `/app` + `.venv`
   - Non-root user
   - Exposes 8080
   - CMD runs `payment_service:app` via uvicorn
   - **Reproducible**: `COPY` only required files, pin SHA tags where possible
   - Image label `org.opencontainers.image.source` pointing to repo
   - Final image size goal: < 300 MB

2. **`gcp/payment-tee-deploy/.dockerignore`** — excludes tests, docs, marketplace code, etc., to keep image lean and reduce supply-chain surface.

### C. Provisioning script

1. **`gcp/payment-tee-deploy/setup-vm.sh`** (bash):
   - Reads `GCP_PROJECT`, `GCP_REGION`, `VM_NAME` from env (with sane defaults documented)
   - Calls `gcloud compute instances create` with:
     - `--machine-type=n2d-standard-2`
     - `--confidential-compute-type=SEV_SNP`
     - `--maintenance-policy=TERMINATE`
     - `--image-family=ubuntu-2204-lts` (or COS — pick and document)
     - Reserved disk + networking flags
   - Installs Docker on first boot via startup-script
   - Pulls and runs `sthrip-payment-tee:latest` image
   - Creates static external IP (named) for stable mTLS endpoint
   - Idempotent: re-running with same VM_NAME prints "already exists" rather than failing
   - **DRY-RUN flag**: `setup-vm.sh --dry-run` prints commands without executing — used by tests

2. **`gcp/payment-tee-deploy/teardown-vm.sh`** — counterpart for cleanup.

### D. mTLS certificate handling

1. **`gcp/payment-tee-deploy/mtls/generate-certs.sh`** — creates a CA, server cert (for VM), client cert (for Railway). Uses `openssl`. Stores under `gcp/payment-tee-deploy/mtls/certs/` (.gitignored).

2. **`gcp/payment-tee-deploy/mtls/README.md`** — instructs operator to generate certs in a secure environment, rotate annually, and load `client.crt` / `client.key` into Railway secrets.

### E. Operator runbook

1. **`gcp/payment-tee-deploy/README.md`** — top-level guide:
   - Prerequisites (gcloud SDK, project + billing enabled, IAM)
   - Step-by-step from `gcloud auth login` to running `setup-vm.sh`
   - How to view logs (`gcloud compute ssh`)
   - Cost expectation table ($50-70/mo)
   - Sprint 5 boundary: Sprint 6 wires Railway → GCP proxy; Sprint 7 wires attestation

### F. Tests

Tests in `tests/test_payment_service_self_contained.py`:

1. **`test_payment_service_imports_only_payment_deps`** — boot the service via `python -c "import gcp.payment_tee_deploy.payment_service"`; capture loaded modules; assert NO forbidden modules (`marketplace`, `mcp`, `escrow`, `sla`, `reviews`, `channels`, `swaps`, etc.). Use `sys.modules` snapshot diff.

2. **`test_payment_service_health_endpoint`** — TestClient hit `/health`, assert 200 + `{"status": "ok"}`.

3. **`test_payment_service_attestation_stub`** — TestClient hit `/attestation`, assert 200 + body has `status: stub` (Sprint 7 will replace).

4. **`test_payment_service_hub_routing_endpoint_exists`** — TestClient hit POST `/v2/payments/hub-routing` with valid envelope, assert response shape matches Railway endpoint (or returns 401/422 for invalid auth — proves endpoint registered).

5. **`test_dockerfile_lints`** — basic checks on Dockerfile: uses non-root user, doesn't run `apt-get install` without `--no-install-recommends`, has `EXPOSE 8080`. Use simple grep, no Docker daemon required.

6. **`test_setup_vm_dry_run`** — call `bash setup-vm.sh --dry-run` with mocked env vars, assert it prints `gcloud compute instances create` with `--confidential-compute-type=SEV_SNP`. No gcloud call actually made.

7. **`test_setup_vm_idempotent`** — mock `gcloud compute instances describe` returning success, run `setup-vm.sh`, assert it short-circuits with "already exists" message.

8. **`test_mtls_cert_script_generates_three_certs`** — run `generate-certs.sh` in tmpdir, assert `ca.crt`, `server.crt`, `client.crt` exist with non-empty content. (Use `--dry-run` if openssl-less env, but openssl should be available on dev macs.)

## How success is verified

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip" && source .venv/bin/activate
pytest tests/test_payment_service_self_contained.py -v --tb=short 2>&1 | tail -30
timeout 600 pytest tests/ -q --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py 2>&1 | tail -30
```

Plus manual verification:
- `ls gcp/payment-tee-deploy/` — all expected files present
- `head -50 gcp/payment-tee-deploy/Dockerfile` — readable, sane
- `bash gcp/payment-tee-deploy/setup-vm.sh --dry-run` — prints expected gcloud commands

## Risk callouts

- **Self-contained boundary**: payment_service.py imports `transaction_repo` which imports balance_repo, models, etc. Need careful pruning — possibly extract a `payment_core` submodule in `sthrip/` that contains ONLY payment dependencies. **Generator: prefer using `import_guard.py` to enforce boundaries at boot rather than restructuring `sthrip/` layout (low blast radius).**
- **Don't break existing tests**: full suite (2871 baseline) MUST stay green. The new payment_service is isolated, but the import_guard MUST NOT poison normal Railway boot.
- **No actual GCP calls**: tests must mock or skip if gcloud SDK absent. CI must pass without GCP credentials.
- **Don't commit secrets**: `.gitignore` mtls/certs/ directory.

## Out of scope

- Running real Confidential VM (Sprint 6 + operator action item)
- Attestation logic (Sprint 7)
- Railway proxy code (Sprint 6)
- Cost tracking dashboard (out of all sprints)

## Branch and commit

- Single commit: `feat(tee): GCP Confidential VM payment-service deploy artifacts (Phase 3 Sprint 5)`
- No push.

## Lead clarifications (preempt Generator questions)

- **Project name on GCP**: `sthrip-tee` per lead-decisions.md. Default in setup-vm.sh.
- **Region**: default to `us-central1` (mature GCP region for Confidential Compute). Document override.
- **Image name**: `sthrip-payment-tee:latest`. CI/registry decision deferred.
- **Domain**: Sprint 6 creates DNS / Railway gRPC client config; Sprint 5 just emits the static external IP for the operator.
- **Backup / DR**: out of scope for Sprint 5. Document in CUTOVER.md (Sprint 7).
