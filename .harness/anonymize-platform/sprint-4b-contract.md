# Sprint 4b Contract: RemoteKeystore + gated FK drop (CODE only)

## What I will build

### Code

- **`sthrip/services/operator_keystore.py`**: replace `RemoteKeystore`'s
  `NotImplementedError` body with a real `httpx.Client` that POSTs to
  `OP_KEYSTORE_URL` (default
  `http://sthrip-op-keystore.railway.internal:8000`). Auth via
  `OP_KEYSTORE_AUTH_TOKEN` shared-secret in the `Authorization: Bearer …`
  header. 5 s timeout. Default `OP_KEYSTORE_MODE=stub` remains; only
  `OP_KEYSTORE_MODE=remote` activates the new client. `get_kek_for_envelope()`
  on the remote class still raises (the hub never sees `KEK_OP` plaintext —
  this is the whole point of pulling the keystore out of the hub process);
  it raises a clearer `RuntimeError` instead of `NotImplementedError`.

- **`sthrip/services/payment_envelope_reader.py`**: already uses `getattr`
  for FK access. Audit + tighten: when both the envelope and every FK column
  are missing return a new `ReadResult.source = "fallback_no_data"` instead
  of constructing all-`None` results that pretend to be `flag_off`/`fallback_*`.

- **`migrations/versions/v3w4x5y6z7a8_drop_legacy_payment_fks.py`**: gated
  behind `STHRIP_DROP_LEGACY_FK=true` (default false). When the flag is unset,
  `upgrade()` raises `RuntimeError` with a helpful operator-facing message
  describing the prerequisites (op-keystore deployed, backfill complete,
  envelope-read soak passed). When the flag is set, drops:
    - `transactions.from_agent_id`, `to_agent_id`, `amount`
    - `escrow_deals.buyer_id`, `seller_id`, `amount`
    - `escrow_milestones.amount`
    - `message_relays.from_agent_id`, `to_agent_id`
  All drops are idempotent (inspector checks each column before dropping).
  Downgrade re-adds nullable columns best-effort (data is unrecoverable
  without backup).

- **`railway/op-keystore-deploy/`**: new service deploy artifacts.
    - `Dockerfile` — `python:3.11-slim` + `fastapi` + `uvicorn` + `cryptography`.
    - `server.py` — minimal FastAPI service exposing `POST /wrap`,
      `POST /unwrap`, `GET /health`. AES-GCM under `KEK_OP_BASE64`. Bearer
      auth via `AUTH_TOKEN` env.
    - `entrypoint.sh` — start uvicorn.
    - `README.md` — operator runbook (env vars, deploy steps, key
      generation, ACL, cutover sequence).

### Tests

- **`tests/test_remote_keystore.py`** (httpx mocked):
    1. Construction without `OP_KEYSTORE_AUTH_TOKEN` → `RuntimeError`.
    2. `unwrap_dek` POSTs to `<URL>/unwrap` with `wrapped_b64` body.
    3. Authorization header is `Bearer <token>`.
    4. Non-200 response raises `RuntimeError` mentioning the status.
    5. Round-trip `wrap_dek` then `unwrap_dek` (mock both endpoints).
    6. `OP_KEYSTORE_MODE=remote` returns a `RemoteKeystore` instance.
    7. `OP_KEYSTORE_MODE=stub` (or unset) returns a `StubKeystore`.

- **`tests/test_fk_drop_migration.py`**:
    1. Migration aborts when `STHRIP_DROP_LEGACY_FK` is unset
       (`RuntimeError`, message mentions prereqs).
    2. Migration drops all required columns when flag is set.
    3. Migration is idempotent (rerun does not fail when columns already
       absent).
    4. The error message lists the operator prerequisites verbatim (keystore,
       backfill, soak).

- **`tests/test_reader_after_fk_drop.py`**:
    1. Reader handles a row with no FK columns at all (simulates post-drop
       schema) via `getattr` and uses envelope payload.
    2. Reader returns `fallback_no_data` when envelope is null AND FK
       columns are missing.
    3. Apply-envelope-to-row no-ops cleanly when target row lacks the legacy
       columns.

- **`tests/test_op_keystore_server.py`** (FastAPI TestClient if available;
  skip otherwise):
    1. `/wrap` then `/unwrap` round-trips a 32-byte DEK.
    2. Missing `Authorization` header → 401.
    3. Wrong bearer token → 403.
    4. `/health` returns 200 ok.

Plus updates to existing `tests/test_operator_keystore.py` to align with
the new contract: `RemoteKeystore` no longer raises `NotImplementedError`
unconditionally; the auth-token-missing branch is the new failure mode.

## Specific testable acceptance criteria

1. `RemoteKeystore.__init__` raises `RuntimeError` if `OP_KEYSTORE_AUTH_TOKEN`
   is unset — `test_remote_keystore_requires_auth_token`.
2. `RemoteKeystore.unwrap_dek` POSTs to `/unwrap` with base64 wrapped body
   and returns base64-decoded plaintext DEK — `test_remote_unwrap_calls_endpoint`.
3. Authorization header is `Bearer <OP_KEYSTORE_AUTH_TOKEN>` —
   `test_remote_auth_header_set`.
4. Non-200 response raises `RuntimeError` mentioning status code —
   `test_remote_unwrap_error_handling`.
5. `OP_KEYSTORE_MODE=stub` (default) → `StubKeystore` —
   `test_keystore_mode_stub_default`.
6. `OP_KEYSTORE_MODE=remote` → `RemoteKeystore` —
   `test_keystore_mode_remote`.
7. Migration aborts when flag unset —
   `test_fk_drop_migration_requires_flag`.
8. Migration drops all four tables' columns when flag set —
   `test_fk_drop_migration_drops_columns`.
9. Migration idempotent on rerun — `test_fk_drop_migration_idempotent`.
10. Reader copes with FK columns dropped — `test_reader_works_after_fk_drop`.
11. Reader signals `fallback_no_data` when both sources missing —
    `test_reader_no_envelope_no_fk_returns_fallback_no_data`.
12. Keystore server `/wrap`+`/unwrap` round-trips —
    `test_keystore_server_wrap_unwrap_roundtrip`.
13. Keystore server enforces bearer auth — `test_keystore_server_requires_auth`.

## How verified

```
pytest tests/test_remote_keystore.py \
       tests/test_fk_drop_migration.py \
       tests/test_reader_after_fk_drop.py \
       tests/test_op_keystore_server.py \
       tests/test_operator_keystore.py \
       tests/test_payment_envelope_reader.py -v
pytest --cov=sthrip/services/operator_keystore \
       --cov=sthrip/services/payment_envelope_reader \
       --cov-report=term --cov-fail-under=80
```

Plus full Sprint 1–7 regression suite remains green:
`pytest tests/ -x --ignore=tests/test_channels.py --ignore=tests/test_mcp_auth.py`.

GitNexus impact run beforehand for `RemoteKeystore`, `payment_envelope_reader`,
`get_keystore`.

## Out of scope

- Actually deploying `sthrip-op-keystore` Railway service (operator).
- Actually flipping `STHRIP_DROP_LEGACY_FK=true` in production (operator).
- Backfill verification cron (Sprint 4a).
- KEK rotation tooling (Sprint 7).
- Reworking `envelope_crypto` to use `wrap_dek`/`unwrap_dek` instead of
  `get_kek_for_envelope` — that is a follow-up cutover after the keystore
  service is online. For now `get_kek_for_envelope` on `RemoteKeystore`
  raises a clearer `RuntimeError` so any accidental cutover surfaces fast.
