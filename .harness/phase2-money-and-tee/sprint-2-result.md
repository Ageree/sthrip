# Sprint 2 Result — Commission on Transfers (final, after iter 2)

**Verdict**: PASS
**Commit verified**: `b1d05a3` (iter 2; iter 1 was `768d0ea`)
**Evaluated**: 2026-05-07T (UTC)
**Iterations**: 2
**Evaluator**: Independent Code Reviewer (no Generator history)

## Iter 2 changes verified

| Change | Status | Notes |
|--------|--------|-------|
| Hub-routing wiring (`_execute_hub_transfer` → `create_with_commission`) | PASS | Confirmed in `api/routers/payments.py:155`; the legacy `tx_repo.create` call is fully removed from this path. |
| Removal of `balance_repo.deduct(...)` from hub path | PASS | grep on `_execute_hub_transfer` returns no `balance_repo.deduct` matches. Deduction now atomic inside `create_with_commission`. |
| Removal of `balance_repo.credit(_UUID(recipient.id), amount)` | PASS | Receiver credit also moved into `create_with_commission`. |
| Removal of `collector.confirm_hub_route(...)` | PASS | Replaced by inline HubRoute status flip with explicit comment naming `confirm_hub_route` as the legacy 1% FeeCollection insert source. |
| HubRoute PENDING → CONFIRMED preserved | PASS | Inline at `payments.py:189-194`; sets `status`, `confirmed_at`, `fee_amount`, `fee_collected`, `fee_collected_at`. Admin/audit invariant kept. |
| 3 new integration tests in `TestHubRoutingWiring` | PASS | All 3 pass in 0.36s. Each asserts exactly ONE FeeCollection row, correct rate_applied_bps (30 / 10), correct sender balance, no legacy `source_type=hub_routing & payer_agent_id IS NULL` row. |
| `create_with_commission` XMR-Decimal conversion correct | PASS | Uses `PICO = Decimal(10) ** 12` (Monero atomic unit is 10⁻¹², not 10⁻⁹). Public API still takes/returns piconero ints. |
| Suite delta: 2839 → 2842 (+3 wiring tests) | PASS | Verified 2842 passed locally. |
| Regressions vs Sprint-1 baseline | NONE | Iter-2 24-failure set was diffed name-by-name against the iter-1 baseline list — identical (channel pytz, e2e idempotency mocks, MCP 19-tool count, migration_error_handling, production_fixes, readiness, session_store Redis). |

## Original contract criteria (re-verified)

| #  | Criterion                                              | Status | Notes |
|----|--------------------------------------------------------|--------|-------|
| A1 | `fee_calculator.py` with `compute_fee` + tier consts   | PASS   | Decimal+ROUND_HALF_UP, integer return, no float. |
| A2 | `AgentTier` enum aligned                                | PASS   | Existing FREE/VERIFIED/PREMIUM/ENTERPRISE; non-FREE → 10 bps (deviation #2 from iter-1, accepted). |
| B1 | Commission deduction at write time, fee row, full amount to receiver, **wired to hot path** | **PASS** (was PARTIAL in iter 1) | Hub-routing path now calls `create_with_commission`. Live traffic on `/v2/payments/hub-routing` charges the new commission rate. |
| B2 | `SELECT FOR UPDATE` per sender                          | PASS   | `BalanceRepository._get_for_update` on Postgres; SQLite fallback for tests. |
| C  | Migration `x5y6z7a8b9c0` with all columns + indexes + idempotency | PASS | Round-trip clean in iter 1, no migration changes in iter 2. |
| D  | `tier_cache.py` per-request memoization                 | PASS   | contextvars-scoped, fallback to direct DB read; middleware wiring is Sprint-3 work. |
| E  | Existing fee_collector tests still green                | PASS   | 25 legacy tests pass; iter-2 changes did not modify `fee_collector.py`. |
| F  | THREAT_MODEL.md update                                  | PASS   | Done in iter 1. |
| 1-12 | Acceptance tests 1–12                                | PASS   | All 27 iter-1 unit/integration tests pass. |
| **NEW** | Hot-path wiring: 3 `TestHubRoutingWiring` integration tests | PASS | Each verifies count=1 FeeCollection, exact balance match (free=997_000_000_000, pro=999_000_000_000), Transaction+HubRoute creation. |

## Test execution evidence

```
$ pytest tests/test_commission_on_transfer.py::TestHubRoutingWiring -v
tests/test_commission_on_transfer.py::TestHubRoutingWiring::test_hub_transfer_charges_commission_only_free_tier PASSED [ 33%]
tests/test_commission_on_transfer.py::TestHubRoutingWiring::test_hub_transfer_charges_commission_only_pro_tier PASSED [ 66%]
tests/test_commission_on_transfer.py::TestHubRoutingWiring::test_hub_transfer_creates_transaction_and_hubroute_rows PASSED [100%]
3 passed in 0.36s

$ pytest tests/test_commission_on_transfer.py tests/test_fee_calculator.py tests/test_fee_collector.py
55 passed in 0.42s

$ pytest tests/ -q --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py
24 failed, 2842 passed, 21 skipped in 110.36s
```

## Code review findings (iter 2)

### CRITICAL
None.

### HIGH
None. **The iter-1 H1 (hub-routing not wired) is CLOSED.**

### MEDIUM

**M1. `fee_info` mutation pattern is unusual.** After `create_with_commission` returns, `_execute_hub_transfer` mutates the caller-supplied `fee_info` dict (`fee_amount`, `fee_percent`, `total_deduction`) so the response surface reflects the real commission, not the legacy 1%. This works but is a side-effect on a parameter the caller still holds. Consider returning a new dict and having `send_hub_routed_payment` use it for response shaping. Not a blocker — caller does not reuse the old values.

**M2. Inline HubRoute status flip duplicates locking semantics already in `confirm_hub_route`.** The function repeats `with_for_update()` + status flip inline. Extracting `mark_hub_route_confirmed(db, payment_id, fee_amount)` would centralise the side-effect set. Sprint-3 cleanup, not a blocker.

**M3. `confirm_hub_route` is still defined and reachable from other call sites.** Generator's table covered the 8 other `create()` callers, but `confirm_hub_route` itself was not audited end-to-end. A separate audit before Sprint-3 cleanup is prudent.

### LOW

**L1. `fee_info["fee_percent"]` overwrite is silent.** Documenting in the docstring would help future readers who might log `fee_info` pre/post invocation.

**L2. Wiring tests create `HubRoute` table inline via `HubRoute.__table__.create(..., checkfirst=True)`.** Works but couples test setup to SQLAlchemy table API. Sprint-3 fixture refactor candidate.

### Other 8 callers of legacy `create()` — verified unchanged

Spot-checked `sthrip/services/deposit_monitor.py:296`, `sthrip/services/recurring_service.py:94`, `api/routers/balance.py:259`. All still call `tx_repo.create(...)`. Deliberate design: system credits, recurring cron, and dev top-ups should not pay commission. Sprint 3 may extend coverage to split-payment / multi-party legs (product call from Lead).

## Honest open issues (carried from Generator)

- GitNexus index stale; rerun `npx gitnexus analyze` before Sprint 3.
- `tier_cache` middleware not wired into FastAPI yet — fallback works (direct DB read in `get_tier`) but adds a per-transfer DB hit. Sprint 3 work.
- Migration full-chain replay blocked by Sprint-3-era `api_sessions.ip_address INET` Postgres-only column on SQLite — verified UP/DOWN/UP in isolation per Sprint 1 precedent.
- `confirm_hub_route` still reachable from non-cutover paths; audit before Sprint 3 cleanup.

## Final verdict

**PASS — ready for Sprint 3.**

Iter-1 H1 (hub-routing path not wired, Sprint 2 dead code in production) is fully closed. The hot path now charges the new commission rate (0.3% Free / 0.1% Pro+) with exactly one FeeCollection row per transfer, no double-charge, balance assertions verified end-to-end. Suite delta is +3 (2839 → 2842) with zero regressions against the Sprint-1 baseline. The 3 MEDIUM findings are cleanup items; none block Sprint 3.
