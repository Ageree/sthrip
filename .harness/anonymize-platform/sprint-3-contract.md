# Sprint 3 Contract: Encrypted payment-graph schema + dual-write

## What I will build

**New service modules**
- `sthrip/services/envelope_crypto.py` — AES-256-GCM envelope encryption with per-row DEK
  wrapped twice (HUB_KEK + OP_KEK). Frozen `PaymentEnvelope` dataclass with stable
  `to_bytes` / `from_bytes` (msgpack, schema_version=1). `amount_to_bucket(Decimal)`
  for log-scale bucket strings (no precise amount).
- `sthrip/services/operator_keystore.py` — keystore facade with two implementations
  (`StubKeystore`, `RemoteKeystore`). Selection via `OP_KEYSTORE_MODE`
  (default `stub`). Stub uses an internal AES-GCM with a fixed 32-byte test KEK so
  wrap/unwrap round-trip preserves the DEK; `RemoteKeystore.unwrap_dek` raises
  `NotImplementedError("Sprint 4")`.

**Schema additions (dual-write only — Sprint 4 cuts over reads)**
- `transactions.participant_envelope BYTEA NULL`, `transactions.amount_bucket VARCHAR(32) NULL`
- `escrow_deals.participant_envelope BYTEA NULL`, `escrow_deals.amount_bucket VARCHAR(32) NULL`
- `escrow_milestones.participant_envelope BYTEA NULL`, `escrow_milestones.amount_bucket VARCHAR(32) NULL`
- `message_relays.participant_envelope BYTEA NULL` (no amount_bucket — no amount field)

**Migration `s0t1u2v3w4x5_payment_envelope.py`**
- Idempotent `add_column` with `inspector.has_column` guards.
- Downgrade drops the columns (only the ones added by this migration).
- No backfill of existing rows; envelope is forward-only per spec line 346.

**Dual-write call sites (repo internals only — public signatures unchanged)**
- `TransactionRepository.create` populates envelope from `(from_agent_id, to_agent_id, amount, memo)`
- `EscrowRepository.create` from `(buyer_id, seller_id, amount, description)`
- `MilestoneRepository.create_milestones` from `(parent_buyer_id, parent_seller_id, amount, description)`
  — milestones don't carry buyer/seller themselves, so the call site loads them from the
  parent escrow's plaintext FKs (still present during dual-write).
- `MessagingService.relay_message` populates envelope from `(from_uuid, to_uuid, payment_id, size_bytes)`
  for the new `MessageRelay` row (no amount, so `amount_bucket` n/a).

**Idempotency**
- `_set_envelope_if_missing(model, payload)` helper checks `model.participant_envelope is None`
  before writing. Re-insert (shouldn't happen, but defence-in-depth) does not overwrite.

**Test files**
- `tests/test_envelope_crypto_service.py` — unit tests for the crypto module
- `tests/test_operator_keystore.py` — unit tests for the keystore facade
- `tests/test_payment_envelope.py` — integration tests across all four repos

**KEK handling**
- `STHRIP_HUB_KEK` env var (32 bytes; accepts hex or base64). On missing, fail-fast at
  first envelope-write attempt with a clear `RuntimeError`. **Not** at module import —
  test fixtures need control. We expose a small helper `_load_hub_kek()` cached via
  `lru_cache` and reset in conftest fixtures.
- `OP_KEYSTORE_MODE` defaults to `stub` (Sprint 3); `remote` raises NotImplementedError.

## Specific testable acceptance criteria

1. Inserting a transaction via `TransactionRepository.create(...)` produces a row whose
   `participant_envelope` is non-null bytes ≥80 bytes.
   → `tests/test_payment_envelope.py::test_transaction_envelope_written`
2. `decrypt_envelope(env, hub_kek, op_kek)` round-trips to the original
   `{from_id, to_id, amount, description}`.
   → `tests/test_envelope_crypto_service.py::test_envelope_roundtrip`
3. Decrypt with only `hub_kek` (op_kek replaced with random) raises `InvalidTag`.
   → `tests/test_envelope_crypto_service.py::test_envelope_requires_both_keys`
4. Existing reads (`get_volume_by_agent`, `get_by_id`, `list_by_agent`) still work and
   still return correct rows because plaintext FKs are populated.
   → `tests/test_payment_envelope.py::test_reads_unchanged_in_dual_write`
5. `amount_to_bucket(Decimal('123.45'))` returns a coarse bucket label such as
   `"100-1k XMR"` and does NOT contain the substring `"123"` or `"123.45"`.
   → `tests/test_envelope_crypto_service.py::test_amount_bucket_coarsened`
6. `EscrowDeal`, `EscrowMilestone`, `MessageRelay` rows all dual-write envelopes.
   → 4 separate tests, one per model.
7. `get_keystore()` in stub mode returns a `StubKeystore` whose `wrap_dek/unwrap_dek`
   round-trips a 32-byte DEK to itself (identity-equivalent).
   → `tests/test_operator_keystore.py::test_keystore_stub_mode`
8. Migration upgrade adds the four columns; downgrade removes them. SQLite-compatible
   (the test suite's in-memory engine).
   → `tests/test_payment_envelope.py::test_migration_round_trip` (introspects columns
   before/after running upgrade/downgrade ops).
9. The existing repo-level test suite remains green (no regressions vs commit `0b03e69`).
   → `pytest tests/ -x --ignore=tests/test_channels.py --ignore=tests/test_mcp_auth.py`
10. `PaymentEnvelope.from_bytes(env.to_bytes()) == env` and `schema_version == 1` survives
    the round trip.
    → `tests/test_envelope_crypto_service.py::test_envelope_schema_version`

## How verified

```
pytest tests/test_payment_envelope.py tests/test_envelope_crypto_service.py tests/test_operator_keystore.py -v
pytest --cov=sthrip/services/envelope_crypto --cov=sthrip/services/operator_keystore --cov-fail-under=80 \
       tests/test_payment_envelope.py tests/test_envelope_crypto_service.py tests/test_operator_keystore.py
pytest tests/ -x --ignore=tests/test_channels.py --ignore=tests/test_mcp_auth.py
```

Migration round-trip is verified inside the test (`test_migration_round_trip`) by
calling the migration's `upgrade()` / `downgrade()` against the SQLAlchemy engine.

## GitNexus impact

| Symbol | Risk | Notes |
|---|---|---|
| `Transaction` | CRITICAL | 71 d=1 importers — but they import the *model*; we only add nullable columns. No method-signature changes. |
| `EscrowDeal` (SDK) | LOW | Index resolved to `sthrip/escrow.py` SDK class (different from DB model). DB-side model is co-located in `models.py` with Transaction; same caveat. |
| `EscrowMilestone` | CRITICAL | 72 d=1 importers; affects 3 execution flows (`accept_match`, `create_contract`, `create_escrow`). Only repo-internal write path changes. |
| `MessageRelay` | CRITICAL | 72 d=1 importers; affects `send_message`. Only `MessagingService.relay_message` write path changes. |

**Mitigation:** all envelope writes are confined to existing `*_repo.create*` functions
plus `MessagingService.relay_message`. Public method signatures and return types are
**unchanged**. Read paths are untouched (Sprint 4 will cut them over). The wide blast
radius reflects "everyone imports `models.py`" — adding nullable columns does not break
them.

## Out of scope

- Read-path cutover (Sprint 4)
- Real `sthrip-op-keystore` Railway service deploy (Sprint 4)
- Backfill of existing rows with envelopes (Sprint 4)
- Admin UI redaction (Sprint 4)
- KEK rotation tooling (post-Sprint 7)
- `crypto_keys` registry table — spec line 332 mentions it, but it's optional
  for dual-write (operator KEK lives in env var on the keystore service; hub KEK in
  hub env var). Deferring to Sprint 4 keeps this sprint contained.
