# Sprint 6 — Generator report

## Final commit

`6fee072` — `feat(tee): payment dispatch proxy w/ feature flag + fall-back (Phase 3 Sprint 6)`

Branch: `feat/revenue-and-tee`. Six files changed, 1289 / 1 insert / delete.

## gitnexus_impact on `_execute_hub_transfer`

```
target  : api/routers/payments.py:_execute_hub_transfer
risk    : LOW
direct  : 1 caller  →  send_hub_routed_payment (same file, depth 1)
processes: 1 affected (the FastAPI hub-routing handler)
```

Only one direct call site. The router's local handler still calls
`_execute_hub_transfer` *through* the new dispatcher. Flag-off path is
behaviourally unchanged (verified by Sprint 2 commission tests + the new
`test_dispatch_atomic_on_local_path`).

## Files added / modified

- `sthrip/services/payment_tee_client.py` (new) — httpx + mTLS client.
  Defines `TEEUnreachableError`, `TEEServerError`, `dispatch_hub_routing`,
  `health_check`. Cert paths via env; tests mock the HTTP layer.
- `sthrip/services/payment_dispatch.py` (new) — orchestrator. Reads
  `STHRIP_PAYMENT_VIA_TEE`, falls back on network/5xx, raises
  `HTTPException` on TEE 4xx (no fall-back), writes the HubRoute admin
  row Railway-side after TEE success (M-1 fix), exposes
  `health_check_loop` background coroutine.
- `sthrip/services/metrics.py` — adds `tee_unreachable_total{reason}` and
  `tee_dispatch_total{outcome}` Prometheus counters (with no-op fallback).
- `api/routers/payments.py` — replaces `_execute_hub_transfer(...)` call
  with `payment_dispatch.dispatch_hub_routing(...)`.
- `api/main_v2.py` — schedules `_tee_health_loop()` background task; tears
  down on shutdown.
- `tests/test_payment_dispatch.py` (new) — 12 contract tests.

## 12 test results — all PASS

| # | Test | Result |
|---|------|--------|
| 1 | `test_flag_off_uses_local_handler` | PASS |
| 2 | `test_flag_on_proxies_to_tee_client_mock` | PASS |
| 3 | `test_tee_unreachable_falls_back_to_local` | PASS |
| 4 | `test_tee_server_error_falls_back` | PASS |
| 5 | `test_tee_4xx_response_NOT_fallback` | PASS |
| 6 | `test_idempotency_key_propagates` | PASS |
| 7 | `test_response_shape_identical_local_vs_proxy` | PASS |
| 8 | `test_hub_route_row_written_after_tee_success` (M-1) | PASS |
| 9 | `test_health_check_loop_calls_endpoint` | PASS |
| 10 | `test_health_check_3_failures_increments_alert_metric` | PASS |
| 11 | `test_dispatch_atomic_on_local_path` | PASS |
| 12 | `test_payment_via_tee_env_default_is_false` | PASS |

`pytest tests/test_payment_dispatch.py -v` → **12 passed in 0.26s**.

## M-1 fix verification

Sprint 5 carry-over closed: `_write_hub_route_after_tee` is invoked
inside `_tee_dispatch` after a 2xx response from the TEE. The TEE keeps
its narrow scope (balance + transaction + fee_collection rows only); the
HubRoute admin / dashboard row is written by Railway from the dispatcher
exactly the way the local `_execute_hub_transfer` writes it today (status
CONFIRMED, `fee_collected=true`, `confirmed_at` set).

`test_hub_route_row_written_after_tee_success` proves this: starts with
0 HubRoute rows, runs the dispatcher with TEE mocked + flag on, asserts
**exactly one** row exists with status `CONFIRMED`, the right
`payment_id`, `fee_amount`, and `from_agent_id` / `to_agent_id`.

The function is also idempotent: if a HubRoute row already exists for
the `payment_id` (Railway crashed mid-write after the TEE returned), it
flips `PENDING → CONFIRMED` in place instead of duplicating.

## Suite delta

| Metric | Sprint 5 baseline | Sprint 6 |
|--------|-------------------|----------|
| Passed | 2879 | **2891** (+12) |
| Failed | 24 (pre-existing) | 24 (same set) |
| Skipped | 21 | 21 |

```
24 failed, 2891 passed, 21 skipped, 3015 warnings in 114.71s
```

The pre-existing 24 failures are the same set Sprint 5's evaluator
documented (idempotency-keys table not created in some integration test
fixtures, mcp_tools auth tests, migration error-handling tests, etc.). I
diff'd the failure list against a `git stash`-baseline and the
`test_webhook_tor_routing.py` regressions I initially introduced (event
loop pollution from `asyncio.run` in tests #9 / #10) were **fixed** by
swapping to a private-loop helper `_drive_loop` that restores the
thread's default loop pointer.

## Sprint 2 regression check

`pytest tests/test_commission_on_transfer.py` → **14 passed**. Local
hub-routing path is byte-for-byte the same as Sprint 5 — the dispatcher
flag-off branch is a single-line shim that delegates to
`_execute_hub_transfer`.

## Risk callouts addressed

- **Hot path edits** — gitnexus_impact returned LOW (1 caller). Sprint 2
  tests confirm the flag-off path stays green.
- **Fall-back transactionality** — fall-back only fires on network /
  5xx, both of which mean the TEE never persisted partial state (TEE
  service wraps everything in a single `with get_db()` block — verified
  in `gcp/payment_tee_deploy/payment_service.py`). 4xx (legitimate user
  error) does NOT fall back, preventing double-charge.
- **Idempotency** — same key flows through router → dispatcher → client
  → TEE. The TEE's `tx_hash = f"hub:{idempotency_key}"` deduplicates
  server-side; Railway's idempotency store deduplicates client-side. Test
  #6 verifies the key reaches the TEE body unchanged.
- **Flag-off mode** — single-line dispatch shim. Test #1 + Sprint 2
  regression suite + `test_dispatch_atomic_on_local_path` confirm it.
- **mTLS in tests** — never exercised; tests patch
  `payment_tee_client.dispatch_hub_routing` and `.health_check`
  directly.
- **Health-check NEVER auto-flips the flag** — implementation only logs
  + increments `tee_unreachable_total{reason="health"}`. Test #10 proves
  the metric is incremented; flag manipulation never happens in
  `health_check_loop`.

## Deviations / escalations

None. The contract simplifications (HTTP+mTLS over gRPC; HubRoute write
on Railway side) are all honoured. No real GCP IPs are contacted in
tests.

GitNexus index is stale (last indexed `49c66bb`); a re-analyze is
expected as part of the post-commit hook flow. No graph queries needed
during evaluator review beyond the upstream impact already gathered.
