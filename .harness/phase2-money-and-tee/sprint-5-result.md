# Sprint 5 Evaluation — GCP Confidential VM Payment-Service Artefacts

**Verdict: PASS**
**Commit:** `ed3821c`
**Branch:** `feat/revenue-and-tee`
**Evaluator context:** independent (no Generator history).

---

## Contract scoring

| Contract item | Status | Notes |
|---|---|---|
| A.1 `payment_service.py` — `/health`, `/attestation`, `/v2/payments/hub-routing` | PASS | All three routes registered; verified via TestClient. |
| A.2 `import_guard.py` — runtime forbidden-module check | PASS | Project-scoped scan + `ALLOWED_OVERRIDES`; opt-in via `TEE_ENFORCE_BOUNDARY=1` (Dockerfile sets it). |
| B.1 Dockerfile — multi-stage, non-root, EXPOSE 8080 | PASS | 2 `FROM` stages, `useradd --uid 10001 sthrip`, `USER sthrip:sthrip`, `EXPOSE 8080`, `--no-install-recommends` discipline, OCI labels, sets `TEE_ENFORCE_BOUNDARY=1`. |
| B.2 `.dockerignore` — excludes marketplace/escrow/tests/etc. | PASS | Explicit per-file exclusions for every forbidden router and service plus tests/docs/.git. |
| C.1 `setup-vm.sh` — env-driven, idempotent, `--dry-run`, SEV-SNP flags | PASS | Defaults from lead-decisions.md, dry-run prints all gcloud commands incl. `--confidential-compute-type=SEV_SNP`, `n2d-standard-2`, shielded-VM hardening, named static IP, `--maintenance-policy=TERMINATE`, `ubuntu-2204-lts`. |
| C.2 `teardown-vm.sh` | PASS | Idempotent, supports `--keep-ip`, `--dry-run`. |
| D.1 `mtls/generate-certs.sh` — CA + server + client | PASS | Produces 6 files (3 .crt 644, 3 .key 600), proper SAN/EKU extensions, transient `.csr`/`.ext`/`.srl` cleaned. |
| D.2 `mtls/README.md` | PASS | Documents secure-host requirement, distribution table, annual rotation. |
| E.1 Operator `README.md` | PASS | Prerequisites, quick-start, live-deploy, cost table ($58–70/mo), trust-boundary rationale, sprint roadmap. |
| F. Tests — 8 contract tests | PASS | All 8 green in 3.04s. |

---

## Test verification

### Focused suite

```
pytest tests/test_payment_service_self_contained.py -v --tb=short
========================== 8 passed in 3.04s ==========================
```

All 8 tests pass:
1. `test_payment_service_imports_only_payment_deps` — POSITIVE (clean subprocess) + NEGATIVE (pre-load `api.routers.escrow`, expect guard to trip) — both subpaths verified.
2. `test_payment_service_health_endpoint`
3. `test_payment_service_attestation_stub`
4. `test_payment_service_hub_routing_endpoint_exists`
5. `test_dockerfile_lints` — multi-stage, non-root USER, EXPOSE 8080, `--no-install-recommends`, `payment_service:app` CMD.
6. `test_setup_vm_dry_run` — gcloud create + SEV_SNP + n2d-standard-2 + describe probe all present.
7. `test_setup_vm_idempotent` — fake `gcloud` on PATH; create call returns exit 99 if invoked; script short-circuits with "already exists".
8. `test_mtls_cert_script_generates_three_certs` — ca/server/client all PEM, non-empty.

Spot-checked test bodies #1 and #6: assertions match their names; subprocess isolation in #1 is the right approach (avoids cross-test sys.modules pollution from prior suites).

### Full suite

```
pytest tests/ -q --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py
24 failed, 2879 passed, 21 skipped, 3015 warnings in 112.79s
```

Sprint 4 baseline: 2871 passed, 24 failed, 21 skipped.
Sprint 5: **+8 passed (2879), 24 failed unchanged, 21 skipped unchanged** — zero regressions.

Failure set is byte-identical to baseline (mcp_tools/19-tools registration, e2e idempotency, migration_error_handling, production_fixes UUID, readiness_nonblocking, session_store Redis).

### Manual scripts

- `bash setup-vm.sh --dry-run` — emits the full gcloud plan with all required flags.
- `bash teardown-vm.sh --dry-run` — emits delete + address-release plan.
- `CERT_OUT_DIR=$tmp bash mtls/generate-certs.sh` — produces 3 cert pairs with 600/644 perms.

---

## Code review

### CRITICAL — none

### HIGH — none

### MEDIUM

**M-1. TEE `hub_routing` skips `HubRoute` row creation that Railway does today.**

Railway's `_execute_hub_transfer` calls `collector.create_hub_route(...)` BEFORE the commission write. This:
* Inserts a `HubRoute` row that the admin dashboard and `GET /payments/{id}` lookup depend on.
* Provides idempotency via the `hub_routes` table's unique key on `idempotency_key`.
* Returns `{"duplicate": True}` on replay so the caller can short-circuit gracefully.

The TEE re-implementation skips this entirely and relies on `tx_hash = f"hub:{idempotency_key}"` colliding in the `transactions` table to enforce idempotency. Behaviourally similar but NOT identical. Sprint 6 MUST either (a) wire the Railway proxy to create the `HubRoute` row before forwarding to TEE, or (b) add the call back inside the TEE service. The Sprint 6 contract should pin this explicitly.

This is acceptable for Sprint 5 (contract scope is the artefact, not behavioural parity), but it raises the integration risk for Sprint 6. **Flag for Sprint 6 lead-decisions.**

**M-2. `fee_info` legacy fields not surfaced.**

Railway's handler mutates `fee_info["fee_amount"]`, `fee_info["fee_percent"]`, `fee_info["total_deduction"]` so older clients still see commission rates. The TEE returns its own `HubPaymentResponse` shape (`payment_id`, `status`, `amount`, `fee`, `fee_percent` strings). Sprint 6 proxy will either translate or pass through — needs explicit wiring.

### LOW

**L-1. Idempotency probe in `setup-vm.sh` dry-run is purely cosmetic.**

In dry-run, the script ALWAYS sets `INSTANCE_EXISTS=0` (line 108) and prints the create command. This is fine for "show me the plan" but means dry-run can't preview the idempotent short-circuit. Test #7 covers that path with a real fake gcloud, so test coverage is intact. Documented inline.

**L-2. Base image is tag-pinned, not SHA-pinned.**

Contract said "pin SHA tags where possible" — `python:3.9-slim` is tag-only. Acceptable for Sprint 5 (the main project's Dockerfile uses the same tag); Sprint 7 / supply-chain hardening can SHA-pin during the production cutover.

**L-3. `cryptography==43.0.1` and `psycopg2-binary==2.9.9` in `requirements.txt` differ from the main project's lockfile.**

Generator notes "intentional divergence to keep the TEE image small". Reasonable, but Sprint 7 should add a CI lockfile diff check (already noted by Generator as out-of-scope).

**L-4. `PROXY_AUTH_TOKEN` defaults to "warn and accept" if unset.**

`payment_service.py:140-143` logs a warning and continues when `PROXY_AUTH_TOKEN` is unset. The Dockerfile does NOT set this env var (it expects Secret Manager injection on the VM). Risk: a misconfigured deploy that fails to inject the secret will silently serve unauthenticated traffic. Sprint 7 production cutover should harden to fail-closed when running with `TEE_ENFORCE_BOUNDARY=1`.

---

## Generator deviations review

### 1. Directory uses underscore (`gcp/payment_tee_deploy/`) not hyphen — **ACCEPT**

**Reason:** Python's import system rejects hyphens in module names, and the contract tests expect `gcp.payment_tee_deploy.payment_service` as an importable path. Maintaining a parallel hyphen-form directory or a `sys.path` shim would be unjustified complexity for a single-character cosmetic preference. All shell scripts, Dockerfile paths, and operator README references work correctly with the underscore form. The README documents the choice. Functionally and operationally identical to the contract's intent.

### 2. Guard is opt-in via `TEE_ENFORCE_BOUNDARY=1` — **ACCEPT**

**Reason:** Verified the Dockerfile sets `TEE_ENFORCE_BOUNDARY=1` at line 52 (runtime stage `ENV` block). In production this means the guard IS mandatory inside the VM container. The opt-in is required because pytest collection legitimately loads marketplace/escrow code from prior tests into the same Python process — making the guard always-on would produce false positives during unit testing. Test #1 forces `TEE_ENFORCE_BOUNDARY=1` in a fresh subprocess, exercising the production code path. Both directions tested (positive: clean import allowed; negative: pre-loaded escrow router trips guard). The guard is NOT dead in production — only suppressed in CI/test contexts where it cannot be true.

### 3. `_execute_hub_transfer` re-implemented (not imported) — **ACCEPT WITH SPRINT 6 CARRY-OVER**

**Reason:** Importing the Railway handler would drag in `BackgroundTasks`, spending policy, audit logger, webhook queue, and Prometheus metrics — exactly the modules the import_guard is designed to keep out. The TEE service calls the same atomic primitive (`TransactionRepository.create_with_commission`) directly, which is the one true write path.

**Drift caveat (M-1 above):** the TEE implementation skips `collector.create_hub_route` (HubRoute row + cross-table idempotency) and `HubRoute` status flip. Sprint 6 MUST wire the Railway proxy to either create the HubRoute row upstream (before forwarding) OR pass that responsibility through to the TEE explicitly. **Document in Sprint 6 contract** so the proxy isn't built assuming the TEE writes the HubRoute row.

Sprint 6's whole purpose is to wire Railway → TEE, so the duplication will reconcile naturally: production traffic only ever hits ONE copy of the logic (the TEE one) once Sprint 7 cuts over.

### 4. `ALLOWED_OVERRIDES` carve-out for `sthrip.swaps.*` and `sthrip.db.*_repo` — **ACCEPT**

**Verified empirically:**
* `sthrip/__init__.py` lines 9-15 explicitly imports `BitcoinHTLC`, `BitcoinRPCClient`, `BitcoinWatcher`, `MoneroMultisig`, `MoneroWallet` from `.swaps`. Loading the `sthrip` package therefore loads all `sthrip.swaps.btc.*` and `sthrip.swaps.xmr.*` submodules. **The carve-out is environmentally forced.**
* `sthrip/db/__init__.py` lines 6-25 imports `EscrowDeal` model and `EscrowRepository` from `.repository`, which in turn loads every repo file. Loading any payment-related repo therefore implicitly loads `sthrip.db.escrow_repo` etc. **Also environmentally forced.**

These carve-outs do NOT hide a real boundary leak — they correctly distinguish between *modules eagerly loaded by the package init* (CRUD code, atomic-swap helpers — no business logic) and *services/routers that must be excluded* (escrow_service, marketplace router, etc.). The carve-out is documented at the call site and in the operator README. **Closing it is Sprint 7+ work** (extract a `sthrip/payment_core/` submodule that doesn't trigger the package init's eager imports). **Flag for Sprint 7.**

---

## Final verdict

**PASS.** All 8 contract tests green; full suite 2879 passed with zero regressions and the byte-identical Sprint 4 failure set; all named artefacts present and functional; manual dry-runs and cert generation work end-to-end; all 4 Generator deviations have sound rationale and pass independent verification.

### Carry-overs flagged for downstream sprints

* **Sprint 6** (proxy wiring) — explicitly decide where `HubRoute` row creation lives (proxy or TEE). Update `fee_info` translation if any legacy clients are still supported. Add `PROXY_AUTH_TOKEN` to Railway secrets and verify TEE rejects requests without it.
* **Sprint 6** (proxy auth) — harden the `PROXY_AUTH_TOKEN` "warn and accept if unset" path to fail-closed when `TEE_ENFORCE_BOUNDARY=1`.
* **Sprint 7** (production hardening) — extract `sthrip/payment_core/` so the import_guard can drop the `ALLOWED_OVERRIDES` carve-out. SHA-pin the base image. Add CI lockfile diff check on `requirements.txt`.

Sprint 5 is correct, complete, and ready to advance.
