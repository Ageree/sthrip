# Sprint 4b Generator Report

## What was built

### Code changes

**`sthrip/services/operator_keystore.py`** — replaced the
`NotImplementedError`-raising `RemoteKeystore` with a real httpx-based
client:

- Constructor reads `OP_KEYSTORE_URL` (default
  `http://sthrip-op-keystore.railway.internal:8000`) and
  `OP_KEYSTORE_AUTH_TOKEN` (required, raises `RuntimeError` if missing).
- `wrap_dek(dek)` and `unwrap_dek(wrapped)` POST base64-encoded bodies to
  `/wrap` / `/unwrap` with `Authorization: Bearer <token>` header.
- Non-200 responses, connection failures, malformed payloads, and
  wrong-length DEKs all raise `RuntimeError` with operator-friendly
  messages. Body slices are truncated to 200 chars to avoid leaking key
  material in logs.
- `get_kek_for_envelope()` raises a clear `RuntimeError` (not
  `NotImplementedError`) explaining the design invariant: the hub never
  sees `KEK_OP` plaintext in remote mode. Callers must migrate to
  `wrap_dek`/`unwrap_dek` before flipping `OP_KEYSTORE_MODE=remote`.

`StubKeystore` and the `get_keystore()` factory + `OP_KEYSTORE_MODE`
selection are unchanged. Default remains `stub`.

**`sthrip/services/payment_envelope_reader.py`** — added a new
`fallback_no_data` `ReadSource` value and a `_is_empty_fallback()` helper.
When the row has no envelope AND no plaintext FK columns (the post-Sprint
4b schema) the reader returns `ReadResult(source="fallback_no_data")`
instead of pretending the all-`None` result is a successful flag-off
read. Existing fallback paths (`flag_off`, `fallback_envelope_null`,
`fallback_decrypt_error`) still return populated FK values when the
columns exist.

**`migrations/versions/v3w4x5y6z7a8_drop_legacy_payment_fks.py`** — new
gated migration. Behind `STHRIP_DROP_LEGACY_FK=true` (default false).
When the flag is unset, `upgrade()` raises `RuntimeError` listing the
operator prerequisites (op-keystore deployed, backfill complete,
envelope-read soak passed). When set, the migration:

| Table | Columns dropped |
| --- | --- |
| `transactions` | `from_agent_id`, `to_agent_id`, `amount` |
| `escrow_deals` | `buyer_id`, `seller_id`, `amount` |
| `escrow_milestones` | `amount` |
| `message_relays` | `from_agent_id`, `to_agent_id` |

Each drop is wrapped in `op.batch_alter_table` (SQLite-safe) and
inspector-checked first (idempotent). Downgrade re-adds columns nullable
best-effort; data is unrecoverable without backup.

**`railway/op-keystore-deploy/`** — new directory with deploy artifacts
mirroring the `tor-sidecar-deploy/` pattern:

- `Dockerfile` — `python:3.11-slim` + FastAPI + uvicorn + cryptography +
  pydantic. Runs as unprivileged `keystore` user.
- `server.py` — minimal FastAPI service: `POST /wrap`, `POST /unwrap`,
  `GET /health`. AES-GCM under `KEK_OP_BASE64`. Bearer auth via
  `AUTH_TOKEN`. Constant-time token compare. Body bytes never logged.
- `entrypoint.sh` — uvicorn launcher (executable, single-worker).
- `README.md` — operator runbook covering KEK + token generation, ACL,
  cutover sequence (deploy → backfill → flag flip → migration), key
  rotation, blast-radius analysis, and a local docker smoke test.

### Test changes

| File | Tests | Notes |
| --- | --- | --- |
| `tests/test_remote_keystore.py` (new) | 16 | httpx mocked; covers auth, URL default, wrap/unwrap, error paths, mode selection |
| `tests/test_fk_drop_migration.py` (new) | 11 | Loaded via importlib; gates, drops, idempotency, downgrade, falsy-flag matrix |
| `tests/test_reader_after_fk_drop.py` (new) | 7 | Simulates post-cutover schema with `SimpleNamespace` rows |
| `tests/test_op_keystore_server.py` (new) | 8 | FastAPI `TestClient`; round-trip, auth, malformed inputs, tampered ciphertext |
| `tests/test_operator_keystore.py` (modified) | 10 | Replaced `NotImplementedError` assertions with new `RuntimeError` paths |

## Test status

```
pytest tests/test_remote_keystore.py tests/test_fk_drop_migration.py \
       tests/test_reader_after_fk_drop.py tests/test_op_keystore_server.py \
       tests/test_operator_keystore.py tests/test_payment_envelope_reader.py
→ 74 passed (16 + 11 + 7 + 8 + 10 + 22)

Coverage on changed modules:
  sthrip/services/operator_keystore.py            91 stmts  98%
  sthrip/services/payment_envelope_reader.py     107 stmts  90%
  TOTAL                                           93%
```

Full repo regression (excluding pre-existing skip lists):

```
pytest tests/ --ignore=tests/test_channels.py --ignore=tests/test_mcp_auth.py \
              --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py
→ 2785 passed, 24 failed, 21 skipped
```

The 24 failures were verified against the pre-Sprint-4b commit
(`e6ef31b`) by stashing the changes — they exist on baseline
unchanged. Zero new regressions introduced by Sprint 4b.

The four new envelope test files plus `test_envelope_crypto_service.py`
+ `test_payment_envelope.py` + `test_backfill_envelope.py` collectively
run as 118 passed, confirming the envelope subsystem is internally
consistent end-to-end.

## GitNexus impact

`mcp__gitnexus__impact({target: "RemoteKeystore", direction: "upstream", repo: "sthrip"})`
flagged CRITICAL risk via 4 direct importers. Inspection confirmed all
calls go through `wrap_dek`, `unwrap_dek`, or `get_kek_for_envelope`
— the same signatures preserved by this change. The only behavioural
delta is `RemoteKeystore` no longer raising `NotImplementedError`
unconditionally; it raises `RuntimeError` when `get_kek_for_envelope`
is invoked in remote mode (caught by existing decrypt-failure paths in
`read_payload_or_none`). No d=1 caller required modification.

`mcp__gitnexus__impact({target: "read_with_fallback", direction: "upstream"})`
showed HIGH risk via 8 repo helpers. The new `fallback_no_data` source
is purely additive — existing branches still fire under the same
conditions. Existing repo callers ignore the `source` field except
where they explicitly check it.

## Files changed / added

| Path | Status |
| --- | --- |
| `sthrip/services/operator_keystore.py` | modified |
| `sthrip/services/payment_envelope_reader.py` | modified |
| `tests/test_operator_keystore.py` | modified |
| `migrations/versions/v3w4x5y6z7a8_drop_legacy_payment_fks.py` | new |
| `railway/op-keystore-deploy/Dockerfile` | new |
| `railway/op-keystore-deploy/server.py` | new |
| `railway/op-keystore-deploy/entrypoint.sh` | new |
| `railway/op-keystore-deploy/README.md` | new |
| `tests/test_remote_keystore.py` | new |
| `tests/test_fk_drop_migration.py` | new |
| `tests/test_reader_after_fk_drop.py` | new |
| `tests/test_op_keystore_server.py` | new |
| `.harness/anonymize-platform/sprint-4b-contract.md` | new |
| `.harness/anonymize-platform/sprint-4b-generator-report.md` | new |
| `.harness/anonymize-platform/state.json` | will be modified |

No commits made (per harness policy).

## Operator runbook (post-merge activation)

CODE is shipped DISABLED. To activate Sprint 4b in production:

1. **Deploy `sthrip-op-keystore`** Railway service from
   `railway/op-keystore-deploy/`. Set `KEK_OP_BASE64` and `AUTH_TOKEN`
   secrets. Verify `/health` from inside the project network.
2. **Set `OP_KEYSTORE_AUTH_TOKEN`** on the API service to the same value
   as `AUTH_TOKEN` on the keystore service. Leave
   `OP_KEYSTORE_MODE=stub` for now.
3. **(Optional) Flip to remote mode** — set `OP_KEYSTORE_MODE=remote`.
   Note that current writer/reader code uses `get_kek_for_envelope`
   which raises in remote mode; this path needs the follow-up cutover
   reworking the envelope helpers to use `wrap_dek`/`unwrap_dek`
   end-to-end. Until that ships, leave mode at `stub`.
4. **Run backfill** until `scripts/backfill_payment_envelope.py` reports
   zero NULL `participant_envelope` rows in production.
5. **Set `STHRIP_READ_FROM_ENVELOPE=true`**, soak 24 h, watch for
   `fallback_decrypt_error` in audit logs.
6. **Set `STHRIP_DROP_LEGACY_FK=true`**, run `alembic upgrade head`.
   Plaintext payment-graph FKs are now gone. Reader continues to
   serve via the envelope. Point of no return — backups required for
   recovery.

## Out of scope (deferred)

- Reworking `envelope_crypto.encrypt_envelope/decrypt_envelope` to call
  `wrap_dek`/`unwrap_dek` instead of taking `op_kek` directly. Required
  before `OP_KEYSTORE_MODE=remote` is usable in production. Tracked as
  follow-up.
- Sprint 7 KEK rotation script (rewrap every row's `dek_wrap_op`).
- Actually deploying the keystore service or running the migration —
  operator responsibility.
