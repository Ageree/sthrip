# Sprint 1 Generator Report — Auto-purge + Warrant Canary

Branch: `feat/revenue-and-tee`
Date: 2026-05-07

## Files added

- `/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/services/purge_service.py` — purge functions for transactions, escrow_deals, escrow_milestones, message_relays, audit_log + `run_full_purge` orchestrator. HMAC chain rolling reset implemented as a synthetic `chain_reset` AuditLog row with NULL `prev_hmac`/`entry_hmac`, which the existing `verify_chain` function already skips as legacy (so the new chain restarts seamlessly).
- `/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/services/canary_service.py` — Ed25519 detached signature over canonical JSON; persistence + staleness check (>48h → None).
- `/Users/saveliy/Documents/Agent Payments/sthrip/migrations/versions/w4x5y6z7a8b9_purge_metadata.py` — `purge_metadata` + `canary_state` tables, idempotent CREATE / DROP, `down_revision = "v3w4x5y6z7a8"`.
- `/Users/saveliy/Documents/Agent Payments/sthrip/tests/test_purge_service.py` — 8 tests covering contract criteria #1, #2, #3, #4, #9, #10 plus 2 bonus cases.
- `/Users/saveliy/Documents/Agent Payments/sthrip/tests/test_canary_service.py` — 9 tests covering criteria #5, #6, #7, #8 plus 5 supporting cases.
- `/Users/saveliy/Documents/Agent Payments/sthrip/.harness/phase2-money-and-tee/sprint-1-generator-report.md` — this report.

## Files modified

- `/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/db/models.py` — added `PurgeMetadata` and `CanaryState` models. PurgeMetadata uses `BigInteger().with_variant(Integer, "sqlite")` so SQLite gets autoincrement and Postgres gets BIGSERIAL.
- `/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/config.py` — added `sthrip_data_retention_days` (default 60, validated 7..365) and `canary_signing_key` (base64 Ed25519 seed, validated 32 bytes when set).
- `/Users/saveliy/Documents/Agent Payments/sthrip/api/routers/wellknown.py` — added `GET /.well-known/canary.txt` endpoint (200 fresh, 503 stale/missing with structured body).
- `/Users/saveliy/Documents/Agent Payments/sthrip/api/main_v2.py` — added `_purge_loop` (daily 03:00 UTC) and `_canary_loop` (daily 03:05 UTC) background tasks with Redis distributed leases, plus shutdown handlers.
- `/Users/saveliy/Documents/Agent Payments/sthrip/tests/conftest.py` — added new tables (AuditLog, IpSalt, PurgeMetadata, CanaryState) to the common test-table superset so existing API tests can use them.
- `/Users/saveliy/Documents/Agent Payments/sthrip/docs/THREAT_MODEL.md` — added Phase 2 Sprint 1 line per contract requirement.

## Tests

### New tests (17 total — all PASS)

`tests/test_purge_service.py`:
1. `test_purge_deletes_old_transactions` — contract #1
2. `test_purge_respects_active_references` — contract #2
3. `test_purge_skips_non_terminal_status` — contract #3
4. `test_purge_message_relays_no_status_guard` — bonus
5. `test_chain_rolling_reset_keeps_new_chain_valid` — contract #4
6. `test_run_full_purge_writes_metadata_row` — contract #9
7. `test_retention_days_env_validation` — contract #10
8. `test_purge_orchestrator_handles_empty_database` — bonus

`tests/test_canary_service.py`:
9. `test_canary_signature_verifies` — contract #5
10. `test_canary_signature_rejects_tampering` — contract #6
11. `test_publish_daily_canary_persists_single_row` — bonus
12. `test_get_current_canary_returns_payload_when_fresh` — bonus
13. `test_get_current_canary_returns_none_when_stale` — bonus
14. `test_canary_signing_requires_key` — bonus
15. `test_canary_endpoint_200_when_fresh` — contract #8
16. `test_canary_endpoint_503_when_stale` — contract #7
17. `test_canary_endpoint_503_when_missing` — bonus

Run: `pytest tests/test_purge_service.py tests/test_canary_service.py -v` → **17 passed, 0 failed**.

### Full suite

`pytest tests/ --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py -q`

- **Baseline (without my changes)**: 2795 passed, 24 failed, 21 skipped.
- **With my changes**: 2812 passed, 24 failed, 21 skipped.
- Delta: **+17 passing tests (my new ones), zero regressions**.

The 24 pre-existing failures are unrelated to Sprint 1 (channel API close-after-settlement, idempotency E2E, MCP tool count drift, migration-error-handling stubs, session_store Redis mock asserts, production_fixes round 2). They fail identically on the stashed baseline so they predate this branch.

The two `--ignore`d files (`test_cli_client.py`, `test_cli_commands.py`) fail at collection because the `respx` library is not installed in the venv. Pre-existing dependency gap; out of scope for Sprint 1.

## Migration round-trip

Cannot run the full chain on SQLite locally (earlier migrations use Postgres-only types like INET via `api_sessions`), and Postgres is not available in this dev environment. Verified the new migration in isolation by stamping at the predecessor revision and exercising the up/down/up cycle:

```
alembic stamp v3w4x5y6z7a8        # from clean SQLite
alembic upgrade head              # → "v3w4x5y6z7a8 -> w4x5y6z7a8b9 ... created purge_metadata; created canary_state"
alembic downgrade -1              # → "downgrade w4x5y6z7a8b9 -> v3w4x5y6z7a8 ... dropped canary_state; dropped purge_metadata"
alembic upgrade head              # → re-creates both tables idempotently
```

`alembic heads` confirms `w4x5y6z7a8b9 (head)` — the migration is wired into the linear chain.

## GitNexus impact analysis

Per CLAUDE.md, impact analysis is mandatory before editing existing symbols. I edited four pre-existing files: `sthrip/db/models.py`, `sthrip/config.py`, `api/routers/wellknown.py`, `api/main_v2.py`, `tests/conftest.py`, `docs/THREAT_MODEL.md`. The GitNexus `mcp__gitnexus__impact` tool is not in my deferred-tool set in this session, so I could not run the formal blast-radius query. Mitigations applied:

- **models.py** — only ADDED new classes (`PurgeMetadata`, `CanaryState`). No existing symbol modified. Zero blast radius.
- **config.py** — added new fields + validators. No existing setting renamed/removed. Pre-existing test `test_settings_*` continue to pass (verified in full-suite run).
- **wellknown.py** — added new `@router.get` route; pre-existing `agent_payments_discovery` untouched. Test `tests/test_wellknown.py` (which I confirmed exists per earlier grep) continues to pass in the full suite.
- **main_v2.py** — added new background loops following the exact pattern of `_escrow_resolution_loop` etc.; pre-existing loops untouched; `services` dict gets two new keys. Existing tests for the lifespan didn't break.
- **conftest.py** — only EXTENDED the `_COMMON_TEST_TABLES` list. Existing test fixtures unaffected.
- **THREAT_MODEL.md** — doc-only.

Audit logger HMAC chain (the highest-blast-radius symbol surface) was NOT modified. The chain reset is implemented by inserting an AuditLog row with NULL `entry_hmac`, which the existing `verify_chain` function already treats as legacy (`rows = [r for r in rows if r.entry_hmac]`, line 528 of `audit_logger.py`). No change to the chain logic was required.

## Deviations from contract

1. **Transaction terminal statuses.** Contract specified `{COMPLETED, EXPIRED, CANCELLED}` for all three (transactions / escrow_deals / escrow_milestones). The `TransactionStatus` enum actually defines `{PENDING, CONFIRMED, FAILED, ORPHANED}` — there is no COMPLETED/EXPIRED/CANCELLED for transactions. I interpreted the contract as "the terminal-status set per model" and used:
   - Transactions: `{CONFIRMED, FAILED, ORPHANED}`
   - EscrowDeal: `{COMPLETED, CANCELLED, EXPIRED}` (matches contract)
   - EscrowMilestone: `{COMPLETED, CANCELLED, EXPIRED}` (matches contract)

   These are exposed as module-level frozensets in `purge_service.py` for visibility. The contract's #3 test (`test_purge_skips_non_terminal_status` on a PENDING tx) passes because PENDING is not in the transaction terminal set.

2. **Migration round-trip on full chain.** Could not exercise `alembic upgrade head` over the full migration history because earlier Sprint-3-era migrations use Postgres-only types unavailable on SQLite (and Docker/Postgres unavailable in this environment). The new migration's up/down logic was verified against a freshly-stamped SQLite DB at the predecessor revision; round-trip succeeded.

3. **Escrow milestone `created_at`.** The model has no `created_at` column; deadlines are derived from the parent deal. I used `activated_at` as the age proxy in `purge_escrow_milestones`. A milestone that never activated is never purgeable on its own — its parent deal carries the lifecycle.

## Open questions

None. `lead-decisions.md` was authoritative; all Sprint 1 design decisions were already locked.

## Commit (pending)

A single commit per the contract:
```
feat(privacy): auto-purge + warrant canary (Phase 1 Sprint 1)
```
will be created with explicit `git add` of just the files listed above.
