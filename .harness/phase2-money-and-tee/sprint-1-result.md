# Sprint 1 Result

**Verdict**: PASS
**Commit verified**: a3a6e38
**Evaluated**: 2026-05-07T15:35Z (Evaluator, independent context)

## Contract criteria scoring

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| A1 | `purge_service.py` with all 6 functions | ✓ | `purge_transactions`, `purge_escrow_deals`, `purge_escrow_milestones`, `purge_message_relays`, `purge_audit_log`, `run_full_purge` all present (lines 117/170/202/239/269/342). |
| A2 | `STHRIP_DATA_RETENTION_DAYS` env validated 7..365 | ✓ | `sthrip/config.py:160` `validate_retention_days` raises ValueError outside [7,365]. |
| B  | HMAC chain rolling reset | ✓ | Synthetic `chain_reset` row inserted with `prev_hmac=NULL`, `entry_hmac=NULL`. Existing `verify_chain` filters NULL-entry rows (`audit_logger.py:528`); existing `_get_prev_hmac` returns `_GENESIS_HMAC` when last row has NULL entry_hmac (`audit_logger.py:298`). New events therefore form a clean chain from genesis. Test #4 confirms behavior end-to-end. |
| C1 | `canary_service.py` generate/sign/publish/get_current | ✓ | `generate_canary_payload`, `sign_canary`, `verify_canary`, `publish_daily_canary`, `get_current_canary` all present. Ed25519 via PyNaCl (battle-tested), canonical JSON (sorted keys, compact separators) over the payload sans `signature_b64`. |
| C2 | `CANARY_SIGNING_KEY` env validated (32-byte b64) | ✓ | `sthrip/config.py:180` validates base64 + exact 32-byte length; empty string allowed (opt-in). |
| D  | `GET /.well-known/canary.txt` 200/503 | ✓ | `api/routers/wellknown.py:287` — 200 fresh, 503 with `{"error":"canary_stale","last_signed_at":...}` when stale or missing. Read-only DB session via `get_db()` context manager. |
| E  | Scheduler integration daily 03:00 / 03:05 UTC | ✓ | `api/main_v2.py:571,607` — `_purge_loop` and `_canary_loop`, both wrapped in distributed Redis lease, scheduled via `_seconds_until_utc(3,0)` and `(3,5)`. Tasks registered in `_startup_services` (lines 738/742). Existing `_escrow_resolution_loop` etc. untouched. |
| F  | Migration `w4x5y6z7a8b9` adds `purge_metadata` + `canary_state` | ✓ | All required columns present (id BigInt/Int variant, run_at TZ-aware, 5x deleted counters, audit_chain_reset_at_id String(64) nullable). Index `ix_purge_metadata_run_at_desc` on `run_at DESC`. Idempotent CREATE/DROP via `_table_exists` guard. |

### Test acceptance criteria (10 contractual + 7 bonus)

| # | Test | Status |
|---|------|--------|
| 1 | `test_purge_deletes_old_transactions` | ✓ PASS |
| 2 | `test_purge_respects_active_references` | ✓ PASS — uses `EXISTS` subquery on EscrowDeal in non-terminal status touching either participant; semantically conservative but correct (false positives delay deletion 1 cycle). |
| 3 | `test_purge_skips_non_terminal_status` | ✓ PASS |
| 4 | `test_chain_rolling_reset_keeps_new_chain_valid` | ✓ PASS — chain re-anchors at genesis after purge; 3 new events form valid chain (`status.ok=True`, `total_checked=3`). |
| 5 | `test_canary_signature_verifies` | ✓ PASS |
| 6 | `test_canary_signature_rejects_tampering` | ✓ PASS |
| 7 | `test_canary_endpoint_503_when_stale` | ✓ PASS — body `{"error":"canary_stale"}`. |
| 8 | `test_canary_endpoint_200_when_fresh` | ✓ PASS — body has `date`, `status`, `signature_b64`. |
| 9 | `test_run_full_purge_writes_metadata_row` | ✓ PASS |
| 10 | `test_retention_days_env_validation` | ✓ PASS — 5 → ValueError, 400 → ValueError, 60 → OK. |
| bonus | `test_purge_message_relays_no_status_guard` | ✓ PASS |
| bonus | `test_purge_orchestrator_handles_empty_database` | ✓ PASS |
| bonus | `test_publish_daily_canary_persists_single_row` | ✓ PASS |
| bonus | `test_get_current_canary_returns_payload_when_fresh` | ✓ PASS |
| bonus | `test_get_current_canary_returns_none_when_stale` | ✓ PASS |
| bonus | `test_canary_signing_requires_key` | ✓ PASS |
| bonus | `test_canary_endpoint_503_when_missing` | ✓ PASS |

## Test verification

- **Focused suite** (`pytest tests/test_purge_service.py tests/test_canary_service.py`): **17 passed, 0 failed** in 0.68s.
- **Full suite** (`pytest tests/ -q --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py`): **2812 passed, 24 failed, 21 skipped** in 109.93s.
- **Delta vs Generator-claimed baseline (2795)**: +17 passing tests, **0 regressions**. Failure set matches Generator's enumeration exactly (channel close-after-settlement [`pytz` ModuleNotFoundError], idempotency E2E, MCP tool count drift, migration-error-handling stubs, session_store Redis mock asserts, production_fixes round 2). I spot-verified `test_close_channel_after_settlement_200` against the stashed baseline — fails identically with `ModuleNotFoundError: No module named 'pytz'`. Pre-existing.
- **Migration round-trip** (in isolation, fresh SQLite stamped at `v3w4x5y6z7a8`): up → down → up succeeds. Both tables present after upgrade, both gone after downgrade, recreated cleanly on re-upgrade. Idempotency guards (`_table_exists`) work as advertised. Index drop in downgrade is wrapped in try/except (Postgres needs explicit drop, SQLite drops with table — safe both ways).

## Code review findings

### CRITICAL
None.

### HIGH
None.

### MEDIUM
- **Active-reference heuristic in `purge_transactions` is over-broad.** The FK protection treats *any* active escrow deal touching either the buyer or seller as a reason to defer deletion of *any* transaction involving those agents (lines 140-150 of `purge_service.py`). An old confirmed transaction between Alice and Bob that has nothing to do with their current open escrow still survives one extra cycle. Generator documents this as deliberate ("false positives delay deletion by one cycle; false negatives would violate the privacy invariant") — that's a defensible privacy-leaning trade-off, not a bug. Worth documenting in the threat model alongside an eventual tightening (e.g., per-deal transaction FK once it exists) rather than left as a hidden behavior.

### LOW
- **Per-row `db.delete()` loops** instead of bulk `Query.delete(synchronize_session=False)`. At 60-day cadence + expected steady-state row count, performance is fine; a refactor to bulk delete would help only at very high volumes. Leaving as a future optimisation is acceptable.
- **`canary_endpoint` opens two `get_db()` sessions** (one for read, one for diagnostic on failure). Cosmetic; combining is cheap but not necessary. Function still returns deterministically.
- **Logger string format `%s` with UUID** in `purge_audit_log` is fine but `reset_id=%s` will str() the UUID lazily — call sites read fine.
- **`_canonical_json(default=str)` fallback**: the canary payload uses only ISO strings + ASCII; `default=str` is unused but harmless. Keeps signing-vs-verify paths symmetric with audit_logger's helper.
- The canary endpoint serves `application/json` while the URL ends in `.txt`. Convention here (RFC-style canary path uses `.txt`); not a concern.

## Generator deviations review

- **Deviation 1 — Transaction terminal statuses {CONFIRMED, FAILED, ORPHANED}**: ACCEPTED. The contract listed `{COMPLETED, EXPIRED, CANCELLED}` generically but the actual `TransactionStatus` enum (`sthrip/db/enums.py`) defines `{PENDING, CONFIRMED, FAILED, ORPHANED}` — there is no `COMPLETED` for transactions. Generator's interpretation ("terminal-status set per model") is the only reasonable read, and EscrowDeal/EscrowMilestone use the literal contract values. Frozen sets are exposed at module level for introspection (good). Test #3 (PENDING is non-terminal → not deleted) confirms.
- **Deviation 2 — Migration round-trip in isolation, not full chain**: ACCEPTED. The earlier Sprint-3-era `api_sessions` migration uses Postgres-only `INET`, and Postgres isn't available in this dev environment. I verified the new migration's up/down/up against a freshly-stamped SQLite DB at `v3w4x5y6z7a8` — round-trip clean, both tables created/dropped/recreated as expected. Generator's deviation is identical to mine.
- **Deviation 3 (Generator-noted) — `escrow_milestones.activated_at` instead of `created_at`**: ACCEPTED. The model has no `created_at`; `activated_at` is the right proxy and a milestone that never activated stays under its parent deal's lifecycle (which has its own purge path). Documented inline in code.

## Final verdict

**PASS — Sprint 1 satisfies contract; ready for Sprint 2.**

All 8 contract code/structure criteria met. All 10 contract test acceptance criteria pass. 17 new tests added (10 contract + 7 supporting); 0 regressions in the 2795-baseline full suite. Migration round-trip verified in isolation. HMAC chain rolling reset is implemented correctly given the existing `verify_chain` semantics (NULL entry_hmac = legacy, skipped) — the choice to use the existing affordance rather than modifying chain logic is exactly the minimum-blast-radius approach the contract risk callout demanded. Two minor deviations are environmentally forced and the third is enum-driven; all three are documented and accepted.

Recommended for Sprint 2 (commission accrual layer).
