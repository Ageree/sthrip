# Sprint 2 Contract: Evolve SwapOrder + SwapService for Real Exchanges

## What will be built

Extend the existing `SwapOrder` model, `SwapRepository`, and `SwapService` to support the real exchange-provider flow (deposit address, external order tracking) while keeping all existing HTLC fields for backward compatibility.

## Specific files and changes

| File | Action |
|------|--------|
| `sthrip/db/models.py` | ADD 3 nullable columns to SwapOrder: `external_order_id`, `deposit_address`, `provider_name` |
| `sthrip/db/swap_repo.py` | ADD `set_external_order()` method; ADD `get_pending_external()` query method |
| `sthrip/services/swap_service.py` | Rewrite `create_swap()` to call exchange providers; add `get_pending_external_orders()` |
| `api/schemas_swap.py` | ADD `deposit_address`, `external_order_id`, `provider_name` to `SwapResponse` |
| `migrations/versions/k2l3m4n5o6p7_swap_external_order_fields.py` | CREATE idempotent Alembic migration |
| `tests/test_swap.py` | ADD tests for new create_swap behaviour with mocked providers |

## Acceptance criteria

1. `SwapOrder` has 3 new nullable columns: `external_order_id VARCHAR(128)`, `deposit_address VARCHAR(255)`, `provider_name VARCHAR(32)`
2. `SwapService.create_swap()` calls `create_order_with_fallback()` to get deposit address; stores `external_order_id`, `deposit_address`, `provider_name` on the order
3. API response for `POST /v2/swap/create` includes `deposit_address` — the address where user should send funds
4. `SwapRepository.get_pending_external()` returns all orders in CREATED state that have a non-null `external_order_id`
5. All 2221 existing tests still pass (regression check)
6. New tests cover the evolved create_swap path with mocked providers

## Success verification

```bash
cd /Users/saveliy/Documents/Agent\ Payments/sthrip
python3 -m pytest tests/test_swap.py -v --tb=short
python3 -m pytest tests/test_exchange_providers.py -v --tb=short
```
