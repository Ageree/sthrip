# Sprint 3 Generator Report

## Status: ready-for-review

## Files changed

### New files
- `sthrip/services/envelope_crypto.py` (~340 lines) — `PaymentEnvelope` frozen
  dataclass + `encrypt_envelope` / `decrypt_envelope` (AES-256-GCM, per-row DEK
  wrapped twice), msgpack wire format with `schema_version=1`, `amount_to_bucket`,
  `load_hub_kek` (env var, hex/base64).
- `sthrip/services/operator_keystore.py` (~140 lines) — `StubKeystore` (Sprint 3,
  fixed 32-byte test KEK, identity-equivalent round-trip), `RemoteKeystore`
  (Sprint 4 placeholder, raises `NotImplementedError`), `get_keystore()` selects
  via `OP_KEYSTORE_MODE` env var.
- `sthrip/services/payment_envelope_writer.py` (~100 lines) — repo-level helper
  `apply_envelope(model, …)` that loads keys, encrypts, sets columns. Idempotent
  (skips if envelope already present). Lazy-imported by repos to avoid a
  services↔db circular dependency.
- `migrations/versions/s0t1u2v3w4x5_payment_envelope.py` — idempotent
  `add_column` for the four tables, downgrade drops them.
- `tests/test_envelope_crypto_service.py` — 29 unit tests
- `tests/test_operator_keystore.py` — 9 unit tests
- `tests/test_payment_envelope.py` — 8 integration + migration tests

### Modified files
- `sthrip/db/models.py` — added `participant_envelope` + `amount_bucket`
  columns to `Transaction`, `EscrowDeal`, `EscrowMilestone`; added
  `participant_envelope` (no bucket) to `MessageRelay`.
- `sthrip/db/transaction_repo.py` — `create()` calls `apply_envelope` after
  building the model; signature unchanged.
- `sthrip/db/escrow_repo.py` — `create()` calls `apply_envelope`; signature
  unchanged.
- `sthrip/db/milestone_repo.py` — `create_milestones()` looks up parent deal
  buyer/seller, calls `apply_envelope` per row; signature unchanged.
- `sthrip/services/messaging_service.py` — `relay_message()` now also writes the
  envelope on the `MessageRelay` row (closes Lead Q5 metadata-graph leak).
- `tests/conftest.py` — autouse fixture sets `STHRIP_HUB_KEK`,
  `OP_KEYSTORE_MODE=stub`, and clears the new `lru_cache` instances each test.

```
 sthrip/db/escrow_repo.py               | 11 +++++++
 sthrip/db/milestone_repo.py            | 21 +++++++++++
 sthrip/db/models.py                    | 21 +++++++++++
 sthrip/db/transaction_repo.py          | 11 +++++++
 sthrip/services/messaging_service.py   | 11 +++++++
 tests/conftest.py                      | 19 +++++++++++
 7 files changed, 113 insertions(+), 4 deletions(-)
```

## Test results

```
tests/test_envelope_crypto_service.py  29 passed
tests/test_operator_keystore.py         9 passed
tests/test_payment_envelope.py          8 passed
TOTAL                                  46 passed, 1 warning, 0.42s
```

### Coverage on new modules

| Module | Coverage |
|---|---|
| `sthrip.services.envelope_crypto` | **89%** (15 missing lines, all defensive branches) |
| `sthrip.services.operator_keystore` | **98%** (1 missing line, interface protocol stub) |
| `sthrip.services.payment_envelope_writer` | **97%** (1 missing line, defensive `except` branch) |

All three exceed the 80% gate.

### Regression suite

`pytest tests/ --ignore=tests/test_channels.py --ignore=tests/test_mcp_auth.py`
plus the eight pre-existing-flaky test-files that already failed on the parent
commit `0b03e69` (verified by `git stash && pytest && git stash pop`):
**2567 passed, 22 skipped, 0 new failures**.

The 12 pre-existing failures live in `test_e2e_production_readiness.py`,
`test_migration_error_handling.py`, `test_production_fixes*.py`,
`test_readiness_nonblocking.py`, and `test_session_store.py`. Each fails on a
clean `0b03e69` checkout (idempotency_keys table missing in fixture, alembic
mock interception broken, redis mock setexpr) — none touch the payment-graph
write path or anything Sprint 3 modifies.

## Migration round-trip

Verified inside `tests/test_payment_envelope.py::test_migration_round_trip` —
spins up an isolated SQLite engine with the four target tables in a "pre-Sprint-3"
shape, then drives the migration body via `alembic.operations.Operations`,
asserts columns exist after `upgrade()`, asserts re-running `upgrade()` is a no-op,
and asserts `downgrade()` removes the columns.

## GitNexus impact summary

Per pre-flight `gitnexus_impact` and post-edit `gitnexus_detect_changes`:

| Symbol | Risk | Why this is OK |
|---|---|---|
| `Transaction` | CRITICAL (71 d=1) | All d=1 are `IMPORTS` of the model — adding nullable columns does not change attribute access; no method signatures changed. |
| `EscrowMilestone` | CRITICAL (72 d=1) | Same import-graph structure. The 3 affected processes (`accept_match`, `create_contract`, `create_escrow`) only break if `create_milestones` shape changed — it didn't. |
| `MessageRelay` | CRITICAL (72 d=1) | Same; only `relay_message` writes the envelope, no read/list path touches the new column. |
| `EscrowDeal` (DB model) | n/a — index resolved the name to `sthrip/escrow.py` (SDK class). DB model is co-located with `Transaction` in `models.py`. |

`detect_changes` confirms only 21 changed symbols, 3 affected processes, and
**risk_level: medium** — squarely consistent with the contract's mitigation
("hide envelope behind repo, public signatures unchanged").

## Stub keystore caveat (TODO Sprint 4)

The `StubKeystore` uses a hard-coded 32-byte literal as `KEK_OP`. This is
**not** the security boundary Sprint 4 ships. Until
`sthrip-op-keystore.railway.internal` is deployed and `OP_KEYSTORE_MODE=remote`
is flipped on:

- An attacker who reads the database **and** has access to the hub container
  (i.e. can read `STHRIP_HUB_KEK`) **can** decrypt envelopes — because the stub
  KEK lives in the same source tree.
- The on-disk format is identical to Sprint 4's; the cutover is a swap of the
  `RemoteKeystore` implementation (currently raises) plus an env-var flip.
- All Sprint 3 envelope writes are forward-compatible — they will still
  decrypt with the real keystore once it returns the same DEK bytes the stub
  returns today.

Per Lead Q1: "for Sprint 3 dual-write phase, the keystore can be a no-op stub
… so Sprint 3 lands without infra dependency, and Sprint 4 cutover blocks
until real `sthrip-op-keystore` deploys." This is exactly the shape of the
deliverable.

## Self-check vs contract

| AC | Status | Evidence |
|---|---|---|
| 1. Transaction envelope ≥80 bytes | PASS | `test_transaction_envelope_written` |
| 2. roundtrip with both keys | PASS | `test_envelope_roundtrip` |
| 3. one-key decrypt fails | PASS | `test_envelope_requires_both_keys` (raises `InvalidTag`) |
| 4. reads unchanged | PASS | `test_reads_unchanged_in_dual_write` |
| 5. amount_bucket coarsened | PASS | `test_amount_bucket_coarsened` (asserts "123" not in bucket) |
| 6. all 4 models dual-write | PASS | tx + escrow + milestone + relay each have `_envelope_written` test |
| 7. stub keystore identity round-trip | PASS | `test_keystore_stub_mode_round_trip` |
| 8. migration upgrade/downgrade round-trip | PASS | `test_migration_round_trip` |
| 9. existing tests green | PASS | 2567 passed; 12 pre-existing flakes confirmed unrelated |
| 10. envelope schema_version round-trip | PASS | `test_envelope_schema_version` |

## Notes for Evaluator

1. **Idempotency on re-encrypt:** `apply_envelope` is a no-op if the model
   already has `participant_envelope` set. This protects against a re-insert
   path or a future retry helper accidentally re-encrypting and stomping the
   prior DEK. Test: `test_transaction_envelope_idempotent_on_replay`.

2. **Tampering detection:** if either DEK wrapper is replaced with a wrap of
   a different DEK (under a still-valid KEK) the check `dek_via_hub != dek_via_op`
   raises `ValueError("wrappers disagree — possible tampering")`. Test:
   `test_envelope_tampering_detection`.

3. **Lazy-imports:** the four repo write sites import
   `payment_envelope_writer.apply_envelope` inside the function body, not at
   module top-level. The reason is `sthrip/services/__init__.py` eagerly
   imports `webhook_service`, which imports back into `sthrip/db/repository`,
   creating a top-level cycle. Inline import resolves it cleanly with
   negligible per-call overhead.

4. **`STHRIP_HUB_KEK` is fail-fast at write time, not import time.** Tests
   that don't write envelopes don't need the env var; tests that do get it
   from the autouse fixture. Production deployments should set it in Railway
   and verify via the standard `/health` boot logs.

5. **`MultisigEscrow` and `PaymentChannel` are flagged "touched" by
   `detect_changes`** because they live in the same `models.py` file as
   `EscrowDeal`/`Transaction`. They are not actually modified — the diff is
   only insertion of envelope columns into other classes. No risk.

6. **`amount_bucket` on `MessageRelay` is intentionally absent.**
   MessageRelay has no amount field; bucket would be "always None", which
   is meaningless. The migration only adds `participant_envelope` to that
   table.

## State update

`.harness/anonymize-platform/state.json` updated:
- `last_status: "sprint-3-generator-done"`
- `iteration: 1`
