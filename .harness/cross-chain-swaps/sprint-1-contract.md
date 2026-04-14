# Sprint 1 Contract: Exchange Provider Clients

## What will be built

**New file**: `sthrip/services/exchange_providers.py`

Abstract `ExchangeProvider` protocol + two concrete implementations:
- `ChangeNowProvider` — primary, uses ChangeNOW REST API v1
- `SideShiftProvider` — fallback, uses SideShift.ai REST API

**New file**: `tests/test_exchange_providers.py`

Unit tests covering both providers and the fallback logic (mocked httpx calls).

## Specific files and changes

| File | Action |
|------|--------|
| `sthrip/services/exchange_providers.py` | CREATE — protocol + 2 providers |
| `tests/test_exchange_providers.py` | CREATE — unit tests (mocked HTTP) |

## Acceptance criteria

1. `ExchangeProvider` protocol defines `create_order(from_currency, from_amount, to_currency, to_address) -> dict` and `get_order_status(external_order_id) -> dict`
2. `ChangeNowProvider.create_order()` calls `POST https://api.changenow.io/v1/transactions/{api_key}` and returns `{external_order_id, deposit_address, expected_amount}`
3. `ChangeNowProvider.get_order_status()` calls `GET https://api.changenow.io/v1/transactions/{id}/{api_key}` and returns `{status, to_amount}` where status is one of: `waiting|confirming|exchanging|sending|finished|failed|expired`
4. `SideShiftProvider.create_order()` calls SideShift fixed-rate order endpoint and returns same shape
5. `SideShiftProvider.get_order_status()` calls SideShift order status endpoint and returns same shape
6. Both providers raise `ExchangeProviderError` (subclass of `RuntimeError`) on HTTP errors
7. `CHANGENOW_API_KEY` read from env; `SIDESHIFT_AFFILIATE_ID` optional from env
8. Tests achieve 80%+ coverage on the new module — all HTTP calls mocked with `unittest.mock.patch`

## Success verification

```bash
cd /Users/saveliy/Documents/Agent\ Payments/sthrip
python -m pytest tests/test_exchange_providers.py -v --tb=short
```

Expected: all tests pass, no network calls made.
