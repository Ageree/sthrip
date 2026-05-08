# Sprint 1 Evaluation Result (iter 2)

## Verdict: PASS

All three HIGH findings from iteration 1 have been fixed in code and verified by
new regression tests. Targeted suite: 49 passed, 2 skipped (PG-only). Coverage
on changed modules: 88.10%. Broad-suite sanity: same 24 pre-existing unrelated
failures as iter 1, no new regressions on Sprint-1 surface. Independent code
inspection confirms the propagation, hard-abort, and multi-row-test fixes all
land correctly. User-criteria AC #1 is satisfied. Ship-it.

## Iteration 1 → 2 fixes verification

- [x] **HIGH-1 propagation fix verified.** `migrations/versions/q8r9s0t1u2v3_audit_ip_hmac.py:238-342` extracts a `_backfill_ip_hmac_and_rechain` helper that:
  - Selects rows in `(created_at ASC, id ASC)` order (line 280)
  - Threads a `running_prev = _GENESIS_HMAC` counter through the loop (line 284)
  - For each row: feeds `running_prev` into `prev_hmac=` slot of `_hash_chain_link` (line 307), records `new_prev_hmac = running_prev` for the UPDATE (line 314), then advances `running_prev = new_entry_hmac` (line 315)
  - UPDATE statement (lines 322-335) writes BOTH `prev_hmac` and `entry_hmac` (plus `ip_hmac`, `ip_salt_id`) atomically
  - Genesis anchor: `_GENESIS_HMAC = sha256(b"genesis").hexdigest()` (line 84) — matches the F-11 runtime convention in `sthrip/services/audit_logger.py:66` and `migrations/versions/o6p7q8r9s0t1_audit_hmac_chain.py:55`. The user prompt's `b"\x00"*32` was incorrect; consistency with the runtime `_GENESIS_HMAC` is what matters and is what the migration uses.
  - Test verifying this: `test_chain_remains_valid_after_multi_row_backfill` (3 seeded rows) + `test_chain_remains_valid_after_hmac_rewrite` (4 log_event rows).

- [x] **HIGH-2 hard abort verified.** `migrations/versions/q8r9s0t1u2v3_audit_ip_hmac.py:227` calls `_assert_chain_linked(conn)` at the end of `upgrade()` (inside the try/finally so it runs before the advisory unlock; advisory lock release is in finally so it always releases even on RuntimeError). The helper at lines 345-371 iterates rows in `(created_at, id)` order with an `expected_prev = _GENESIS_HMAC` anchor and `raise RuntimeError("audit_log chain integrity broken at row id=...")` on any mismatch. Test verifying this: `test_assert_chain_linked_raises_on_broken_chain` (line 671) — seeds 2 rows where row 2's prev_hmac is `"deadbeef" * 8` instead of row 1's entry_hmac and asserts `pytest.raises(RuntimeError, match="chain integrity broken")`.

- [x] **HIGH-3 multi-row test verified.** `tests/test_audit_log_ip_hmac.py:531` `test_chain_remains_valid_after_multi_row_backfill` seeds **3 rows** (`seeded_rows = [...]` at lines 583-587) BEFORE running the migration helper. Pen-test passes:
  - Seed is constructed AS A VALID F-11 CHAIN OVER RAW IPs (lines 589-611 walk `prev_hmac` → `entry_hmac` forward, anchored at `_MIG_GENESIS`).
  - Sanity assertion at lines 614-624 verifies the seed chain is well-formed BEFORE running backfill (catches a constructively-broken false positive).
  - Post-backfill assertions (lines 654-661) walk `expected_prev` forward and assert `r[3] == expected_prev` row-by-row — this is the actual chain check, not just an "ip_hmac not null" check. Lines 646-652 separately assert ip_hmac matches the deterministic HMAC of each seeded IP.
  - Migration's own `_assert_chain_linked` is invoked at line 666 as a final cross-check.
  - Iter-1 implementation (single-row UPDATE without prev_hmac propagation) WOULD FAIL this test on row[1] or row[2].

## Tests run (with results)

```bash
PYTHONPATH=. ENVIRONMENT=dev ADMIN_API_KEY=<64hex> AUDIT_HMAC_KEY=<64hex> MONERO_RPC_PASS=x \
  pytest tests/test_audit_log_ip_hmac.py -v
```
Result: **20 passed, 2 skipped** (PG-only round-trip + PG-only single-row backfill check are both skipped — both are gated on `TEST_DATABASE_URL` pointing at Postgres; the new SQLite-driven multi-row regression test covers the same propagation surface independently).

```bash
pytest --cov=sthrip.services.audit_logger \
       --cov=sthrip.services.ip_salt_service \
       --cov-report=term-missing \
       tests/test_audit_log_ip_hmac.py tests/test_audit_logger.py tests/services/test_audit_chain.py
```
Result: **49 passed, 2 skipped**. Coverage:
- `sthrip/services/audit_logger.py`: **88%** (163 stmts, 19 missed)
- `sthrip/services/ip_salt_service.py`: **88%** (89 stmts, 11 missed)
- TOTAL: **88.10%** (≥80% gate satisfied; same as iter 1, no regression).

```bash
pytest tests/ --ignore=tests/test_channels.py --ignore=tests/test_mcp_auth.py \
              --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py --tb=line -q
```
Result: **24 failed, 2569 passed, 21 skipped** in 107s. The 24 failures are the
identical pre-existing set from iter 1 (channel API regex, idempotency E2E
needing real Redis, MCP auth fixtures, alembic migration-error-handling unit
tests, session-store SETEX call signature drift, e2e production readiness).
**None touch `audit_logger`, `ip_salt_service`, or
`q8r9s0t1u2v3_audit_ip_hmac.py`.** No new regressions introduced by iter 2.

## User criteria AC#1 check

| User criterion | Met? | Evidence |
|---|---|---|
| audit_log не хранит сырые PII | YES | `audit_log.ip_address` Column dropped in models + migration drop_column step 6; raw IP never persisted by `log_event` (hashes via `compute_ip_hmac` before write at `audit_logger.py:447`); no `details=...ip` writes anywhere in `sthrip/`/`api/` audit-log call-sites. |
| ip_address удалён или хеширован keyed-HMAC с rotating salt | YES | Both — column dropped from schema; service writes `ip_hmac` (`hmac.new(salt, ip, sha256).digest()` with salt as KEY at `ip_salt_service.py:105`) under a rotating salt referenced by `ip_salt_id` FK. |
| salt ротируется не реже раза в неделю и старый удаляется | YES | `IP_SALT_ROTATION_DAYS` env var (default 7), `rotate_ip_salt` retires current and zeroes salts beyond 2× window (`test_rotate_destroys_salts_beyond_double_window` passes). |
| request_body пишется через allowlist полей | YES | `_AUDIT_REQUEST_BODY_ALLOWLIST` is default-deny (`audit_logger.py:178-183`); unknown action → `request_body=None`; tests `test_allowlist_filters_disallowed_keys`, `test_allowlist_unknown_action_defaults_to_none`, `test_allowlist_empty_details_is_safe` all pass. |
| HMAC-цепочка остаётся валидной после изменений | YES | (1) Runtime `_hash_chain_link` consumes `hex(ip_hmac)` in the canonical IP slot — `test_hash_chain_link_consumes_hex_hmac` passes; (2) migration's backfill rewrites `entry_hmac` AND `prev_hmac` row-by-row using a running counter, end-to-end chain verified by `test_chain_remains_valid_after_multi_row_backfill` (3-row regression) and `_assert_chain_linked` smoke check at end of `upgrade()`. |

## Code review findings (any new since iter 1)

- **CRITICAL: none**
- **HIGH: none** — all three iter-1 HIGH findings closed.
- **MEDIUM-1 (carried over from iter 1, not a Sprint 1 blocker):** `datetime.utcnow()` at `q8r9s0t1u2v3_audit_ip_hmac.py:165, 219` is deprecated in Python 3.12+; produces tz-naive output. Same drift exists across the codebase. Defer to a separate cleanup sprint.
- **MEDIUM-2 (carried over from iter 1):** `bootstrap_secret = os.urandom(32)` at line 150 is generated unconditionally before the existence check. On a re-run of an already-applied migration the freshly-generated bytes are discarded (the code reads back the persisted secret from the DB at lines 170-174). Wasted entropy, no security impact. Cosmetic.
- **LOW-1 (new this iter):** `_backfill_ip_hmac_and_rechain` skips legacy rows where `entry_hmac IS NULL` and does NOT advance `running_prev`. This is correct behaviour (legacy rows are outside the F-11 chain by design — same as `o6p7q8r9s0t1_audit_hmac_chain.py`), but it relies on legacy rows preceding non-legacy rows in `(created_at, id)` ordering — a true assumption in production where F-11 rolled out at a fixed point in time. Not a defect; worth a one-line comment but not a Sprint 1 blocker. Already partially documented at lines 254-258 of the migration.
- **LOW-2:** Genesis-anchor convention is `sha256(b"genesis").hexdigest()`, not `b"\x00"*32` as the user prompt suggested. Migration matches the existing F-11 runtime convention in `sthrip/services/audit_logger.py:66`, which is the right call (consistency over the prompt). Verified at the iter-2 line 84 of the migration.

## Coverage

```
Name                                 Stmts   Miss  Cover   Missing
------------------------------------------------------------------
sthrip/services/audit_logger.py        163     19    88%   158, 183, 202, 249-250, 253-254, 266-267, 272-273, 301-306, 386-388, 449-454, 516, 532
sthrip/services/ip_salt_service.py      89     11    88%   78, 85-90, 104, 113-114, 116-117, 126-127
------------------------------------------------------------------
TOTAL                                  252     30    88%
Required test coverage of 80.0% reached. Total coverage: 88.10%
```

Uncovered lines are defensive fallbacks (mock-friendly except clauses, dialect
detection fallback, env-parse error path). 88% > 80% gate.

## Recommendation

**ship-it** — green-light Lead to commit Sprint 1 to `feat/anonymity-hardening`.

Iter 2 closed all three iter-1 HIGH findings:
1. Migration backfill propagates the chain via `running_prev` + dual-column UPDATE.
2. `_assert_chain_linked` is called at end of `upgrade()` and raises RuntimeError on any mismatch — deploys hard-abort on chain corruption.
3. New `test_chain_remains_valid_after_multi_row_backfill` is a true 3-row regression test (seeds a valid pre-migration F-11 chain, runs the actual backfill helper, walks the post-state forward) — will fail on the iter-1 single-row-UPDATE bug.

Targeted suite is fully green at 88% coverage. Broad suite has zero new
regressions vs iter 1 (same 24 pre-existing unrelated failures). User-criteria
AC #1 (audit_log no raw PII, salt rotation, allowlist, chain validity) is met
end-to-end.
