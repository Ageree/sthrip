# Sprint 6 Contract — Payment Proxy from Railway to GCP TEE

> Phase 3 Sprint 6. HIGH risk — payment hot path. Pre-filled by Lead from product-spec.md (AD-7) and lead-decisions.md.

## Critical context — Sprint 5 carry-over M-1

Sprint 5 evaluator found: TEE `payment_service.hub_routing` (in `gcp/payment_tee_deploy/payment_service.py`) skips HubRoute row creation + status flip that Railway's `_execute_hub_transfer` does. The two are NOT behaviorally identical.

**Lead decision (do not re-litigate)**: keep TEE service minimal. Sprint 6 fixes drift by writing HubRoute row from the **Railway proxy layer AFTER TEE returns success**. TEE stays focused on the payment-correctness primitives (envelope decrypt, balance moves, fee row, transaction row, stats counter). HubRoute is admin/dashboard metadata — owned by Railway.

## What Generator will build

### A. TEE client

1. **`sthrip/services/payment_tee_client.py`** — HTTP client (mTLS) to the GCP TEE service:
   - `dispatch_hub_routing(envelope: bytes, idempotency_key: str, timeout_s: float = 30.0) -> dict` — POSTs to TEE `/v2/payments/hub-routing`, returns parsed response.
   - mTLS: loads client cert + key + CA from env paths (`STHRIP_TEE_CLIENT_CERT_PATH`, `_KEY_PATH`, `_CA_PATH`).
   - Honors `STHRIP_TEE_ENDPOINT` env (e.g., `https://1.2.3.4:8080` — IP from Sprint 5 setup-vm.sh).
   - Network errors → raise `TEEUnreachableError` (clear distinction from TEE returning a 4xx/5xx).
   - 5xx from TEE → raise `TEEServerError` (also fall-back trigger).
   - 4xx from TEE (e.g., insufficient balance) → return the response body unchanged (don't fall back; payment legitimately rejected).
   - Idempotency-key passthrough: include in request body or header per TEE service spec.

### B. Dispatch layer

1. **`sthrip/services/payment_dispatch.py`** — orchestrator that decides local vs TEE:
   - `dispatch_hub_routing(envelope, idempotency_key, db, ...) -> dict`:
     - Reads env `STHRIP_PAYMENT_VIA_TEE` (default `false`)
     - If `false` → call existing local handler (`_execute_hub_transfer` logic)
     - If `true`:
       - Call `payment_tee_client.dispatch_hub_routing(...)`
       - If `TEEUnreachableError` or `TEEServerError` → log + alert + fall back to local handler (rollback safety)
       - If success → write HubRoute row locally (the M-1 fix), update status, return TEE response
   - All paths must return identical response shape.

### C. Wire dispatch into Railway

1. **Modify `api/routers/payments.py`** — replace direct call to `_execute_hub_transfer` with `payment_dispatch.dispatch_hub_routing(...)`.
   - Run `gitnexus_impact` on `_execute_hub_transfer` first.
   - The signature should remain identical for the dispatcher (same params).
   - All other 8 callers of `tx_repo.create()` are unaffected (they don't touch this path).

### D. Health check

1. **Background loop in `api/main_v2.py`** (or scheduler):
   - Every 60s, when `STHRIP_PAYMENT_VIA_TEE=true`, calls `GET <TEE_ENDPOINT>/health`.
   - On 3 consecutive failures → emit alert (log warning, increment `tee_unreachable_total` metric).
   - Does NOT auto-flip the flag — operator decides; fallback already in dispatcher.
   - Skip loop entirely if flag is `false`.

### E. Idempotency-key passthrough

1. The Railway HTTP request body has `idempotency_key`. Pass it through to TEE. TEE's existing logic should respect it (already wired in Sprint 5's `_execute_hub_transfer` duplicate). Verify this is end-to-end consistent.

### F. Response shape parity

1. Local mode and TEE-proxy mode MUST return byte-identical response bodies (modulo non-deterministic timestamps). Add a contract test that runs the same request through both modes (TEE mocked) and asserts shape equality.

## Specific testable acceptance criteria

Tests in `tests/test_payment_dispatch.py`:

1. **`test_flag_off_uses_local_handler`** — `STHRIP_PAYMENT_VIA_TEE` unset, dispatch invokes local handler, no TEE client call.

2. **`test_flag_on_proxies_to_tee_client_mock`** — flag set, mock TEE client, verify TEE client called with the right envelope+idempotency_key, response returned.

3. **`test_tee_unreachable_falls_back_to_local`** — flag on, TEE client raises TEEUnreachableError, dispatch falls back to local; assert local executed; assert alert metric incremented.

4. **`test_tee_server_error_falls_back`** — flag on, TEE client raises TEEServerError, same fall-back as #3.

5. **`test_tee_4xx_response_NOT_fallback`** — flag on, TEE returns 422 (legitimate rejection like insufficient balance). Dispatch returns the 4xx response, does NOT fall back to local. Reason: legitimate user-error, no need to retry on Railway.

6. **`test_idempotency_key_propagates`** — flag on, TEE client mock asserts the idempotency_key from the request is passed to the TEE call.

7. **`test_response_shape_identical_local_vs_proxy`** — run same input through both modes (mock TEE returning a structurally identical response), assert response shapes match (json keys + types).

8. **`test_hub_route_row_written_after_tee_success`** (Sprint 5 M-1 fix) — flag on, TEE returns success, assert a HubRoute row was inserted in Railway's DB with correct status (CONFIRMED).

9. **`test_health_check_loop_calls_endpoint`** — start the background loop with mock client, advance clock, verify GET /health was called.

10. **`test_health_check_3_failures_increments_alert_metric`** — mock 3 consecutive failures, verify `tee_unreachable_total` Prometheus counter incremented to 3.

11. **`test_dispatch_atomic_on_local_path`** — flag off, simulate failure mid-local-handler, verify rollback (no partial state).

12. **`test_payment_via_tee_env_default_is_false`** — without setting env, `STHRIP_PAYMENT_VIA_TEE` reads as false → local mode.

## How success is verified

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip" && source .venv/bin/activate
pytest tests/test_payment_dispatch.py -v --tb=short 2>&1 | tail -50
pytest tests/test_commission_on_transfer.py -v --tb=short 2>&1 | tail -20  # regression on Sprint 2
timeout 600 pytest tests/ -q --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py 2>&1 | tail -30
```

No migration in Sprint 6.

## Risk callouts (Generator MUST address)

- **HIGH risk: payment hot path edits.** Run `gitnexus_impact({target: "_execute_hub_transfer", direction: "upstream"})` first. Evaluator will verify both flag-on and flag-off paths produce identical outcomes.
- **Fall-back must be transactional** — if TEE call fails AFTER any persistent side-effect on TEE side (envelope written? balance deducted?), the local handler doing the same work twice would double-charge. Solution: the TEE service only persists changes on full success; partial failures roll back inside the TEE. Verify this assumption by reading Sprint 5's payment_service.py and confirming the TEE wraps everything in a DB transaction.
- **Idempotency-key**: same idempotency_key must yield same response from local AND TEE modes. If TEE handles a request and Railway falls back to local for a retry, local must NOT charge again — the idempotency-key check protects this.
- **Don't break flag-off mode**: 99% of traffic until operator flips the flag. Local handler MUST stay green.
- **mTLS cert paths**: tests should NOT require real certs. Mock the HTTP layer at the `httpx`/`requests` level.

## Out of scope

- Attestation verification (Sprint 7)
- Live GCP deploy (operator action)
- gRPC migration (using HTTP+mTLS for Sprint 6 — gRPC was an option; Lead simplifies to HTTP).
- Multi-region failover (Sprint 7+)

## Branch and commit

- Single commit: `feat(tee): payment dispatch proxy w/ feature flag + fall-back (Phase 3 Sprint 6)`
- No push.
