# Sprint 4a Evaluation Result

## Verdict: PASS

Non-destructive read cutover landed cleanly. Feature flag default-off invariant holds; with `STHRIP_READ_FROM_ENVELOPE` unset or false, repo read paths are observably identical to Sprint 3 (verified via stash-and-rerun on existing read suites). No new migrations, no FK-column drops, no destructive operations. Backfill is rerun-safe and dry-run is read-only. Admin redacted view is wired but inert in stub mode (by contract — Sprint 4b activates it).

## Tests run

```
# 1. New Sprint 4a suites, flag unset
pytest tests/test_payment_envelope_reader.py tests/test_repo_dual_read.py \
       tests/test_backfill_envelope.py tests/test_admin_redacted_view.py
→ 49 passed (22 + 9 + 7 + 11)

# 2. New Sprint 4a suites, flag on
STHRIP_READ_FROM_ENVELOPE=true pytest tests/test_payment_envelope_reader.py \
                                      tests/test_repo_dual_read.py
→ 31 passed

# 3. Existing read paths, flag unset (regression baseline)
pytest tests/test_database.py tests/test_concurrent_payments.py \
       tests/test_payment_envelope.py tests/test_escrow.py \
       tests/test_milestone_escrow.py
→ 78 passed, 3 skipped

# 4. Coverage of reader module
coverage run --include="*payment_envelope_reader*" -m pytest \
    tests/test_payment_envelope_reader.py
→ 86% (96 stmts, 13 missed) — above 80% bar

# 5. Backfill dry-run smoke
DATABASE_URL=sqlite:////tmp/sthrip_eval_4a.db \
    python scripts/backfill_payment_envelope.py --dry-run
→ all 4 tables processed=0 skipped=0; no writes verified by sqlite mtime

# 6. Full regression (flag unset, excluding known broken suites)
pytest tests/ --ignore=test_channels.py --ignore=test_mcp_auth.py \
              --ignore=<4 sprint-4a files> --ignore=test_cli_*
→ 2616 passed, 22 skipped, 17 failed

# 7. Stash 4a, rerun the 17 failing tests on Sprint 3 baseline
git stash; pytest <17 failing tests>
→ Same 17 failures, identical signatures. ZERO new regressions from Sprint 4a.
```

## Acceptance criteria check

| # | Criterion | Status | Evidence |
|---|---|--------|---|
| 1 | Reads unchanged when flag off | PASS | `test_*_reads_unchanged_when_flag_off` (3 tests) + 78 existing read tests still green with flag unset |
| 2 | Reads use envelope when flag on | PASS | `test_*_reads_use_envelope_when_flag_on` (3 tests, all repos) |
| 3 | Fallback when envelope null | PASS | `test_transaction_reads_fallback_when_envelope_null`, escrow variant |
| 4 | Fallback when decrypt fails | PASS | `test_transaction_reads_fallback_when_decrypt_fails`, `test_read_with_fallback_wrong_key_falls_back` |
| 5 | Backfill rerun-safe | PASS | `test_backfill_idempotent` |
| 6 | Backfill skips existing | PASS | `test_backfill_skips_existing` |
| 7 | Backfill 4 tables | PASS | `test_backfill_covers_all_tables`; smoke run also exercised all 4 specs |
| 8 | Admin redacted when no KEK | PASS | `test_admin_view_redacted_when_keystore_unavailable`, `test_admin_view_auto_probes_keystore` |
| 9 | Admin full when KEK present | PASS | `test_admin_view_full_when_keystore_available`, `test_admin_view_auto_probes_stub_full` |
| 10 | ≥80% reader coverage | PASS | 86% measured (claim of 91% in generator report not reproducible with my tooling but bar still met) |
| 11 | Zero new regressions | PASS | 17 pre-existing failures; same set on Sprint 3 baseline |

## Feature flag default-off invariant verified

Critical for "non-destructive": with `STHRIP_READ_FROM_ENVELOPE` unset or `=false`,
`payment_envelope_reader.read_with_fallback` short-circuits at line 201 (`if not feature_flag_enabled(): return _row_fallback(...)`) BEFORE any envelope/keystore access. `apply_envelope_to_row` then sees `result.source != "envelope"` and returns immediately without mutating the ORM row. Confirmed empirically:

- All Sprint 3 read tests pass with `STHRIP_READ_FROM_ENVELOPE` unset (78 / 78).
- All Sprint 3 read tests pass with `STHRIP_READ_FROM_ENVELOPE=false` explicit (69 / 69 in spot-check).
- Stash-pop comparison: same 17 failures with and without 4a, ruling out any silent behavioural delta.

There is a tiny per-row overhead from invoking `_row_fallback` (attribute reads + ReadResult construction) even when flag off; not a correctness issue but flagged below as LOW.

## Backfill rerun-safety verified

- SQL filter `WHERE participant_envelope IS NULL` enforces idempotency at the source — verified at `scripts/backfill_payment_envelope.py:210`.
- `_backfill_row` re-checks `getattr(row, "participant_envelope", None) is not None` defensively (line 150).
- Per-batch commit (line 235), batch ordering by `id`, terminate on `len(rows) < batch_size`.
- Dry-run never commits and uses an early `break` after first batch to avoid the infinite-loop case (lines 230-233).
- `--table` flag works (filter at line 262).
- Milestones resolve buyer/seller from the parent `EscrowDeal` via `_resolve_milestone_parents`, one query per batch.
- Smoke run confirmed: all 4 tables iterate cleanly on an empty schema.

## Admin redaction works

- `_keystore_available()` probes the keystore and only returns False when the probe raises (Sprint 4b RemoteKeystore mode). Stub mode → returns True → full data shown. Matches the contract's "today is impossible" expectation.
- `_serialize_escrow(deal, redacted=True)` swaps `buyer_id`/`seller_id` to `"encrypted"`, `description` to `"encrypted"`, `amount` to `amount_bucket` or `"redacted"` fallback. Tests cover all three branches.
- Auto-probe path (default `redacted=None`) consults `_keystore_available()` — exactly the documented behaviour.

## NO destructive changes verified

- `git diff HEAD --stat -- migrations/` → empty. **No new migration files; no edits to existing ones.**
- The only `drop_column` calls in the tree are in the **downgrade** path of the pre-existing Sprint 3 migration (`s0t1u2v3w4x5_payment_envelope.py`) and the initial schema migration. Neither is touched.
- `git diff HEAD -- sthrip/db/` shows ONLY post-process additions: every modified read method preserves its signature and returns the same ORM row type as before. No FK columns dropped from the ORM models.
- Sprint 3's dual-write code (`payment_envelope_writer.apply_envelope`) is unmodified — the writer continues to populate both legacy FKs AND `participant_envelope` for new rows.

## Coverage

- `sthrip/services/payment_envelope_reader.py` = 86% (96 stmts, 13 missed). Missed lines are mostly `_coerce_uuid` exception branches and the `memo`/`description` else-fallthrough in `apply_envelope_to_row` — all defensive paths, all tested at the integration level.

## Code review findings

### CRITICAL
None.

### HIGH
None.

### MEDIUM

**M1. Backfill infinite loop on persistent skips.** `scripts/backfill_payment_envelope.py:207-238`. If a non-dry-run batch fetches `batch_size` rows and ALL of them get skipped (e.g. `_backfill_row` returns False for every milestone with a missing parent escrow), the next iteration re-fetches the SAME rows because `participant_envelope IS NULL` is unchanged. The current termination guard `if len(rows) < batch_size: break` won't fire. Realistic only for orphaned milestones, but worth a defensive `if processed_this_batch == 0: break` or `OFFSET`-based pagination. Not a Sprint 4a blocker because dry-run is the supported path; rule of thumb: ops should run dry-run first.

**M2. Admin redacted-view is currently inert.** `_keystore_available()` only returns False in remote keystore mode (Sprint 4b). In stub mode (Sprint 3/4a default) the helper always returns True, so the redaction branch is exercised only by tests, not by real admin requests today. Contract acknowledges this (line 137: "today is impossible"). Acceptable per the split decision — wiring is in place; activation is a Sprint 4b config flip. Flagging because **AC #2 from `user-criteria.md` ("ADMIN_API_KEY alone должен НЕ давать чтения этого графа без второго фактора") is NOT satisfied yet** — only the mechanism is. This is faithfully documented in the lead-decisions-sprint4.md split.

### LOW

**L1. Per-row overhead even when flag off.** `apply_envelope_to_row` always builds a `ReadResult` via `_row_fallback` (4 `getattr` calls + Decimal coerce + dataclass construction) before checking `result.source != "envelope"`. Cheap but unnecessary. Could fast-path: `if not feature_flag_enabled(): return None`. Trivial perf nit; not behavioural.

**L2. Re-import inside hot read paths.** Each call to `EscrowRepository.list_by_agent` etc. re-imports `payment_envelope_reader` (`from sthrip.services.payment_envelope_reader import apply_envelope_to_row`) inside the method body. CPython caches sys.modules so this is essentially a dict lookup, but module-level import would be cleaner and removes a potential circular-import temptation later.

**L3. `_row_fallback` description fallthrough is single-source.** Lines 173-177 do `if hasattr(row, "description"): use it; else use memo`. If a future model exposes BOTH columns (unlikely), only `description` would surface. Not a current bug; ORM rows have exactly one of the two.

**L4. Coverage claim discrepancy.** Generator report claims 91%; measured locally is 86%. Both above the 80% bar. Difference is environment-dependent (PyO3/cryptography reload bug locally caused me to use `coverage run --include` workaround). Not material.

## Recommendation

**ship-it**

All acceptance criteria from `sprint-4a-contract.md` met. Non-destructive invariant holds end-to-end. Zero new regressions confirmed via stash-pop comparison. The MEDIUM findings are documentation-grade (M1: defensive guard on a path that requires orphaned milestones; M2: explicit out-of-scope per the split decision). LOW findings are perf/style nits.

Sprint 4b should pick up the orphan-skip guard (M1) before the prod backfill cron, and ensure RemoteKeystore deploy is followed by `OP_KEYSTORE_MODE=remote` + `STHRIP_READ_FROM_ENVELOPE=true` flip in that order; otherwise AC #2 stays open as documented.
