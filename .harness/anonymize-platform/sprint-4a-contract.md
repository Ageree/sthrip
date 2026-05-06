# Sprint 4a Contract: Dual-read + backfill (non-destructive)

## What I will build

**New service module**
- `sthrip/services/payment_envelope_reader.py` (~200 lines): a feature-flag-gated
  reader that decrypts a row's `participant_envelope` and falls back to plaintext
  FK columns if envelope is null or decrypt fails. Exports:
  - `feature_flag_enabled() -> bool` — reads `STHRIP_READ_FROM_ENVELOPE`.
  - `read_with_fallback(row, ...) -> ReadResult` — frozen dataclass with
    `from_agent_id`, `to_agent_id`, `amount`, `description`, `source` literal.
  - `read_payload_or_none(row) -> Optional[dict]` — pure decrypt, no fallback.

**Repo modifications (additive only — public method signatures unchanged)**
- `sthrip/db/transaction_repo.py`: when flag enabled, post-process
  `list_by_agent` / `get_by_hash` results so the returned ORM rows have their
  `from_agent_id`/`to_agent_id`/`amount`/`memo` swapped to envelope-decrypted
  values when envelope is present and decryptable. Idempotent and read-only.
- `sthrip/db/escrow_repo.py`: same pattern for `get_by_id` / `get_by_hash` /
  `list_by_agent` / `get_pending_expiry`.
- `sthrip/db/milestone_repo.py`: same for `get_by_escrow_and_sequence` /
  `get_by_escrow` / `get_pending_milestone_expiry`.

**Backfill script**
- `scripts/backfill_payment_envelope.py` (~200 lines): rerun-safe backfill for
  4 tables (`transactions`, `escrow_deals`, `escrow_milestones`, `message_relays`).
  Selects `WHERE participant_envelope IS NULL`, batches at 500 rows, calls
  `payment_envelope_writer.apply_envelope` per row, commits per batch.
  CLI flags: `--dry-run`, `--batch-size N`, `--table NAME` (optional filter).

**Admin redacted view**
- `api/admin_ui/views.py`: a small `_keystore_available()` helper — returns
  False when `OP_KEYSTORE_MODE=remote` (the Sprint 4b production mode that
  raises `NotImplementedError` until the keystore service is deployed) or when
  decryption raises. When unavailable, `_serialize_hub_route`,
  `_serialize_escrow`, and the milestone serializer route through a new
  `_redact_envelope_fields` helper that returns:
  - `from_agent_id` → string `"encrypted"`
  - `to_agent_id` → string `"encrypted"`
  - `amount` → `row.amount_bucket` if present else `"redacted"`
  - `description` / `memo` → `"encrypted"`

**Tests** (all new files, no touching existing tests)
- `tests/test_payment_envelope_reader.py` — unit tests for reader module
- `tests/test_backfill_envelope.py` — backfill script tests
- `tests/test_admin_redacted_view.py` — admin view conditional rendering
- `tests/test_repo_dual_read.py` — repo-level integration smoke tests

## Specific testable acceptance criteria

1. Default `STHRIP_READ_FROM_ENVELOPE` is `false`; reads return identical
   results as before. → `test_repo_dual_read.py::test_reads_unchanged_when_flag_off`
2. With flag `true` and envelope present, repo reads return values decrypted
   from envelope. → `test_repo_dual_read.py::test_reads_use_envelope_when_flag_on`
3. With flag `true` and envelope null, repo reads fall back to FK columns.
   → `test_repo_dual_read.py::test_reads_fallback_when_envelope_null`
4. With flag `true` and envelope present but decrypt fails (wrong KEK
   simulated), reads emit a warning and fall back to FK.
   → `test_repo_dual_read.py::test_reads_fallback_when_decrypt_fails`
5. Backfill running twice on the same DB produces zero changes on the second
   run. → `test_backfill_envelope.py::test_backfill_idempotent`
6. Backfill skips rows that already have a non-null envelope.
   → `test_backfill_envelope.py::test_backfill_skips_existing`
7. Backfill processes all 4 tables (transactions, escrow_deals,
   escrow_milestones, message_relays).
   → `test_backfill_envelope.py::test_backfill_covers_all_tables`
8. Admin view without operator KEK shows `"encrypted"` for participants and
   bucket label for amount.
   → `test_admin_redacted_view.py::test_admin_view_redacted_when_keystore_unavailable`
9. Admin view with operator KEK shows full participants and exact amount.
   → `test_admin_redacted_view.py::test_admin_view_full_when_keystore_available`
10. Reader module ≥80% line coverage.
    → `pytest --cov=sthrip/services/payment_envelope_reader --cov-fail-under=80`
11. Zero new regressions in existing read-path tests.
    → `pytest tests/ -x --ignore=tests/test_channels.py --ignore=tests/test_mcp_auth.py`

## How verified

```
pytest tests/test_payment_envelope_reader.py \
       tests/test_backfill_envelope.py \
       tests/test_admin_redacted_view.py \
       tests/test_repo_dual_read.py -v

pytest --cov=sthrip/services/payment_envelope_reader \
       --cov-report=term --cov-fail-under=80 \
       tests/test_payment_envelope_reader.py

python scripts/backfill_payment_envelope.py --dry-run

pytest tests/ -x --ignore=tests/test_channels.py --ignore=tests/test_mcp_auth.py
```

## GitNexus impact

| Symbol | Risk | Notes |
|---|---|---|
| `TransactionRepository.list_by_agent` | LOW | post-processing inside method; signature unchanged. |
| `EscrowRepository.get_pending_expiry` | LOW | 1 d=1 caller (`EscrowService.resolve_expired`); only mutates returned row contents when flag on. |
| `MilestoneRepository.get_by_escrow` | LOW | additive post-process. |
| `_serialize_escrow` / `_serialize_hub_route` | LOW | branch on `_keystore_available()`; default branch unchanged. |
| New module `payment_envelope_reader` | NEW | no upstream callers initially — wired in by repo edits in this sprint. |

**Mitigation:** all behaviour changes are gated behind
`STHRIP_READ_FROM_ENVELOPE=false` (default). Existing reads have IDENTICAL
output until an operator opts in. Admin redaction triggers only when
keystore is unreachable, which today is impossible (stub mode is default).

## Out of scope (Sprint 4b)

- FK column drops (`from_agent_id`, `to_agent_id`, `buyer_id`, `seller_id`,
  `amount`, `memo`, `description`)
- Real `RemoteKeystore` implementation
- `sthrip-op-keystore` Railway service deploy
- Production flag flip (`STHRIP_READ_FROM_ENVELOPE=true`)
- Mainnet backfill execution
