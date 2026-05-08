# Sprint 5 Generator Report — GCP Confidential VM Payment-Service Artefacts

> Phase 3 Sprint 5 (kickoff). Artefacts only — no live GCP resources created.

## Summary

All 8 named tests green. Suite delta: 2871 → 2879 passed (+8). Same 24
pre-existing failures as Sprint 4 baseline; zero regressions.

## Files added

```
gcp/__init__.py
gcp/payment_tee_deploy/__init__.py
gcp/payment_tee_deploy/payment_service.py        FastAPI app: /health, /attestation, /v2/payments/hub-routing
gcp/payment_tee_deploy/import_guard.py           Boot-time forbidden-module scanner
gcp/payment_tee_deploy/Dockerfile                Multi-stage, non-root, EXPOSE 8080
gcp/payment_tee_deploy/.dockerignore             Excludes marketplace/escrow/admin/MCP/tests
gcp/payment_tee_deploy/.gitignore                Blocks mtls/certs/, *.key, *.crt
gcp/payment_tee_deploy/requirements.txt          Pinned runtime deps (8 packages)
gcp/payment_tee_deploy/setup-vm.sh               Idempotent provisioning script
gcp/payment_tee_deploy/teardown-vm.sh            Counterpart cleanup script
gcp/payment_tee_deploy/mtls/generate-certs.sh    openssl-based CA + server + client cert mint
gcp/payment_tee_deploy/mtls/README.md            Cert distribution + rotation runbook
gcp/payment_tee_deploy/README.md                 Top-level operator runbook
tests/test_payment_service_self_contained.py    8 contract tests
```

## Boundary approach

Chose **Option A — runtime import_guard**, per contract guidance.

The Sthrip package's `sthrip/__init__.py` eagerly imports several sibling
subpackages (`sthrip.swaps`, `sthrip.swaps.btc`, `sthrip.swaps.xmr`, etc.)
and the loaded `sthrip.db.repository` facade fans out into every repo file.
Restructuring to extract a `sthrip/payment_core/` would have required
touching ~20 modules and re-routing every existing import in production
code. **That is Sprint 7+ work**, not Sprint 5.

The runtime guard:

* Scans `sys.modules` after FastAPI app construction.
* Restricts the scan to **project modules only** (`sthrip.*`, `api.*`,
  `cli.*`, `integrations.*`, etc.) — third-party names like
  `sqlalchemy.dialects.postgresql.operators` (which contains the substring
  `tor`) are correctly ignored. The first-pass code lacked this restriction
  and produced ~30 false-positive hits on stdlib/third-party module names.
* Uses an `ALLOWED_OVERRIDES` carve-out list for the handful of legacy
  modules (`sthrip.db.escrow_repo`, `sthrip.swaps.btc.htlc`, etc.) that the
  package init unavoidably loads. These are pure CRUD or atomic-swap
  helpers; loading them does NOT expand the trust boundary because the
  payment service does not invoke them.
* Is **opt-in via `TEE_ENFORCE_BOUNDARY=1`** so it doesn't trigger false
  positives during pytest collection (where earlier suites legitimately
  load marketplace/escrow code into the same Python process). The
  Dockerfile sets the env var to `1`; tests verify the guard works
  correctly via a fresh subprocess that has `TEE_ENFORCE_BOUNDARY=1` set.
* The contract test verifies BOTH directions: payment_service imports
  cleanly with no forbidden modules (positive), AND the guard trips when
  `api.routers.escrow` is pre-loaded (negative).

The forbidden list targets HIGH-RISK modules:

```
marketplace, admin_ui, integrations.sthrip_mcp, tor_sidecar,
subscription_billing_service, matchmaking_service, messaging_service,
review_service, sla_service, channel_service, swap_service, stablecoin,
escrow_service, api.routers.{escrow,marketplace,reviews,sla,channels,
swaps,stablecoin}, api.admin_ui.
```

These are the routers and services that, if loaded into the TEE, would
expand the attack surface meaningfully. Repos (CRUD) and atomic-swap
helpers are LOW-RISK and exempted via `ALLOWED_OVERRIDES`.

## Endpoint design — DUPLICATION over IMPORT

The Railway router `api.routers.payments.send_hub_routed_payment` is
tightly coupled to FastAPI `Request`, `BackgroundTasks`, the spending
policy service, the audit logger, the webhook queue, and Prometheus
metrics. Importing it would pull every one of those into the TEE. Instead,
`payment_service.py::hub_routing` re-implements a minimal core that calls
the same atomic primitives directly:

* `TransactionRepository.create_with_commission` — atomic balance + fee
  + envelope write (the one true payment write path).
* `Agent` lookup by id (recipient validation already happened in the
  Railway proxy).

Spending-policy / webhooks / audit happen **upstream** in the Railway
proxy (Sprint 6). The TEE service trusts that anything reaching its
endpoint via mTLS already passed those checks.

## 8 test results

| # | Test                                                          | Status |
|---|---------------------------------------------------------------|--------|
| 1 | `test_payment_service_imports_only_payment_deps`              | PASS   |
| 2 | `test_payment_service_health_endpoint`                        | PASS   |
| 3 | `test_payment_service_attestation_stub`                       | PASS   |
| 4 | `test_payment_service_hub_routing_endpoint_exists`            | PASS   |
| 5 | `test_dockerfile_lints`                                       | PASS   |
| 6 | `test_setup_vm_dry_run`                                       | PASS   |
| 7 | `test_setup_vm_idempotent`                                    | PASS   |
| 8 | `test_mtls_cert_script_generates_three_certs`                 | PASS   |

```
============================== 8 passed in 5.19s ===============================
```

Test 1 also verifies the negative case (guard trips when `api.routers.escrow`
is pre-loaded) via a second sub-probe.

## Suite delta

| Run        | Passed | Failed | Skipped |
|------------|--------|--------|---------|
| Baseline   | 2871   | 24     | 21      |
| Sprint 5   | 2879   | 24     | 21      |

`+8` tests added, `0` regressions. Failure set is byte-identical to the
Sprint 4 baseline (mcp_tools auth, e2e_production_readiness idempotency,
migration_error_handling, production_fixes UUID, readiness_nonblocking,
session_store Redis — all pre-existing).

## Manual verification

### `bash gcp/payment_tee_deploy/setup-vm.sh --dry-run`

```
==> sthrip-payment-tee VM provisioning
    project : sthrip-tee
    region  : us-central1
    zone    : us-central1-a
    vm      : sthrip-payment-tee
    machine : n2d-standard-2
    image   : ubuntu-2204-lts (ubuntu-os-cloud)
    dry-run : 1

==> Checking whether sthrip-payment-tee already exists...
+ gcloud compute instances describe sthrip-payment-tee --project=sthrip-tee --zone=us-central1-a --format=value\(name\)
==> Reserving static external IP sthrip-payment-tee-ip...
+ gcloud compute addresses create sthrip-payment-tee-ip --project=sthrip-tee --region=us-central1
==> Creating Confidential VM sthrip-payment-tee...
+ gcloud compute instances create sthrip-payment-tee --project=sthrip-tee --zone=us-central1-a --machine-type=n2d-standard-2 --confidential-compute-type=SEV_SNP --maintenance-policy=TERMINATE --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud --boot-disk-size=20GB --boot-disk-type=pd-ssd --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring --address=sthrip-payment-tee-ip --tags=sthrip-payment-tee --metadata-from-file=startup-script=... --metadata=image-name=sthrip-payment-tee:latest

==> Done. The VM is provisioning. View boot logs with:
    gcloud compute ssh sthrip-payment-tee --project=sthrip-tee --zone=us-central1-a \
        --command='sudo journalctl -u google-startup-scripts.service -f'
```

All flags from the contract are present: `n2d-standard-2`,
`--confidential-compute-type=SEV_SNP`, `--maintenance-policy=TERMINATE`,
`--image-family=ubuntu-2204-lts`, `--image-project=ubuntu-os-cloud`,
shielded-VM hardening flags, named static external IP.

### `CERT_OUT_DIR=/tmp/sthrip-mtls-test bash gcp/payment_tee_deploy/mtls/generate-certs.sh`

```
==> Generating 4096-bit RSA root CA (3650 days)...
==> Generating server cert for sthrip-payment-tee...
==> Generating client cert for sthrip-railway-proxy...
==> Done. Files in /tmp/sthrip-mtls-test:
total 48
-rw-r--r--  1 saveliy  wheel  1716 May  7 ca.crt
-rw-------  1 saveliy  wheel  3243 May  7 ca.key
-rw-r--r--  1 saveliy  wheel  1870 May  7 client.crt
-rw-------  1 saveliy  wheel  3243 May  7 client.key
-rw-r--r--  1 saveliy  wheel  1935 May  7 server.crt
-rw-------  1 saveliy  wheel  3243 May  7 server.key
```

3 cert files + 3 key files; permissions correctly 600 on keys, 644 on certs.

## Deviations from contract

1. **Directory uses underscore (`gcp/payment_tee_deploy/`) not hyphen
   (`gcp/payment-tee-deploy/`).** Python's import system rejects hyphens
   in module names, and the test imports the package as
   `gcp.payment_tee_deploy.payment_service`. Maintaining two parallel
   directories or aliasing was unjustified complexity for a single-letter
   stylistic difference. The README documents the choice. All shell
   scripts and Dockerfile work correctly with the underscore form.

2. **Guard is opt-in (`TEE_ENFORCE_BOUNDARY=1`) rather than always-on.**
   First-pass made the guard always-on, which produced false positives in
   pytest because earlier test files legitimately load
   marketplace/escrow code into the same Python process. The
   Dockerfile sets `TEE_ENFORCE_BOUNDARY=1`, so production behaviour is
   unchanged. Test #1 forces `TEE_ENFORCE_BOUNDARY=1` in a fresh
   subprocess to verify the guard works.

3. **`ALLOWED_OVERRIDES` carve-out for repos and swap helpers.**
   `sthrip/__init__.py` and `sthrip/db/__init__.py` eagerly import every
   sibling repo + the entire `sthrip.swaps` subpackage. These are pure
   CRUD or atomic-swap helpers with no business logic; their loading
   does not expand the trust boundary. The carve-out is documented at
   the call site and in the operator README. Closing this carve-out is
   a Sprint 7+ task (extract `sthrip/payment_core/` submodule).

4. **`/v2/payments/hub-routing` is a re-implementation, not a delegating
   wrapper.** The Railway handler is too coupled to spending-policy /
   audit / webhook side-effects to import without dragging those into
   the TEE. The TEE handler calls the same atomic primitives
   (`TransactionRepository.create_with_commission`) directly. Sprint 6
   wires the Railway proxy that runs the upstream policy/audit checks
   before forwarding to the TEE.

## Commit

Single commit on `feat/revenue-and-tee`:

```
feat(tee): GCP Confidential VM payment-service deploy artifacts (Phase 3 Sprint 5)
```

(SHA captured below in the final response.)

No push.

## Out of scope (per contract)

- Live Confidential VM provisioning (operator action item, Sprint 6+).
- Real SEV-SNP attestation report fetching (Sprint 7).
- Railway proxy / gRPC client / DNS wiring (Sprint 6).
- CI lockfile diff check on `requirements.txt` (Sprint 7).
