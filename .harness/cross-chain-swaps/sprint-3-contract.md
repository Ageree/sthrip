# Sprint 3 Contract: Background Poller + Auto-Credit + Cleanup

## What will be built

1. **Background poller** in `SwapService`: `poll_external_orders()` — checks exchange status for all pending orders every 60s, credits XMR balance on completion
2. **Background task loop** wired into `api/main_v2.py`
3. **SwapRepository**: add `complete_from_external()` — transition CREATED → COMPLETED directly (exchange-completed path, no HTLC lock required)
4. **Stale order cleanup**: `expire_stale()` already exists; ensure it runs in the loop
5. **New migration**: no new columns needed; this sprint is logic-only
6. **Tests**: `tests/test_swap_poller.py` covering the poller logic

## Specific files and changes

| File | Action |
|------|--------|
| `sthrip/db/swap_repo.py` | ADD `complete_from_external()` — CREATED → COMPLETED directly |
| `sthrip/services/swap_service.py` | ADD `poll_external_orders()` |
| `api/main_v2.py` | ADD `_swap_poll_loop()` async task; start it in `_startup_services()` |
| `tests/test_swap_poller.py` | CREATE — unit tests for poller |

## Acceptance criteria

1. `SwapRepository.complete_from_external(swap_id, to_amount, xmr_tx_hash)` transitions CREATED → COMPLETED (no LOCKED step needed for exchange path)
2. `SwapService.poll_external_orders(db)` — for each CREATED order with `external_order_id`: calls `provider.get_order_status()`; if FINISHED, calls `complete_from_external()` and credits XMR balance; if FAILED/EXPIRED, calls `expire()` 
3. Background loop `_swap_poll_loop()` runs every 60 seconds in lifespan
4. Stale orders (past lock_expiry) are expired by the loop as well
5. Tests achieve 80%+ coverage on new poller code — all provider calls mocked

## Success verification

```bash
cd /Users/saveliy/Documents/Agent\ Payments/sthrip
python3 -m pytest tests/test_swap_poller.py tests/test_swap.py tests/test_exchange_providers.py -v --tb=short
```
