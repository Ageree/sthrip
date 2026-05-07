# Sprint 2 Generator Report — Commission on Transfers

**Branch:** `feat/revenue-and-tee`
**Commit SHA:** `768d0ea`
**Baseline:** Sprint 1 head `a3a6e38`

## Files added

- `/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/services/fee_calculator.py` (84 LoC) — `compute_fee(amount: int, tier) -> int`, integer/Decimal piconero math, ROUND_HALF_UP, floor=1; tier→bps mapping (`FREE=30 bps`, all paid tiers=10 bps).
- `/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/services/tier_cache.py` (60 LoC) — `contextvars`-scoped per-request `Agent.tier` memoization with safe fallback to direct DB read; `begin_request` / `end_request` / `get_tier` / `clear_for_agent`.
- `/Users/saveliy/Documents/Agent Payments/sthrip/migrations/versions/x5y6z7a8b9c0_fee_collections.py` — extends `fee_collections` with 5 new columns (`payer_agent_id`, `amount_piconero`, `rate_applied_bps`, `transaction_ref`, `collected_at`) + 2 new indexes; idempotent `IF NOT EXISTS` guards; SQLite batch-alter on downgrade for FK column drop.
- `/Users/saveliy/Documents/Agent Payments/sthrip/tests/test_fee_calculator.py` — 16 unit tests (contract criteria 1-6 + edge-case coverage).
- `/Users/saveliy/Documents/Agent Payments/sthrip/tests/test_commission_on_transfer.py` — 11 integration tests (contract criteria 7-12).

## Files modified

- `/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/db/models.py` — added 5 nullable columns to `FeeCollection` + 2 indexes; legacy columns untouched.
- `/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/db/transaction_repo.py` — new `InsufficientBalanceError(ValueError)` + new method `TransactionRepository.create_with_commission(...)`. Existing `create()` left untouched (zero-blast-radius approach).
- `/Users/saveliy/Documents/Agent Payments/sthrip/docs/THREAT_MODEL.md` — added Phase 2 Sprint 2 revenue/commission paragraph per contract.

## gitnexus_impact result

`gitnexus_impact({target: "create_transaction", direction: "upstream", repo: "sthrip"})` — symbol not present (the repo's actual write method is `TransactionRepository.create`, not a free function). Ran `gitnexus_impact({target: "TransactionRepository.create"})` — also not indexed under that name. Top callers found via grep:

| Caller | File | Risk |
|---|---|---|
| `_execute_hub_transfer` | `api/routers/payments.py:138` | HIGH — payment hot path |
| `BalanceRepository.deposit` callers | `api/routers/balance.py:259, 393` | MEDIUM |
| `tx_repo.create` | `sthrip/services/{multi_party_service,conditional_payment_service,split_payment_service,deposit_monitor,recurring_service}.py` | MEDIUM |

**Resolution:** rather than mutate the existing `create()` signature (which would force breaking changes across all 8 callers), I introduced an additive method `create_with_commission(...)`. Existing callers are 100% unchanged; the hub-routing path can adopt the new method incrementally in Sprint 3 (when tier enforcement middleware lands and atomic-deduction is moved out of `_execute_hub_transfer`). **Blast radius of this commit: ZERO direct callers broken.**

## Test counts

- `test_fee_calculator.py`: **16 new tests, all passing**
- `test_commission_on_transfer.py`: **11 new tests, all passing**
- `test_fee_collector.py` (legacy, unmodified): **25 tests, all still passing** (criterion 12)

**Suite delta:** 2812 → **2839 passed** (+27 new tests). 24 pre-existing failures unchanged (channel close-after-settlement `pytz` ModuleNotFoundError, idempotency E2E mocks, MCP tool count drift, migration-error-handling stubs, session_store Redis mocks, production_fixes round 2). **Zero regressions.**

```
2839 passed, 24 failed, 21 skipped, 387 warnings in 110.47s
```

## Migration round-trip result

In isolation against fresh SQLite stamped at `w4x5y6z7a8b9` (Sprint 1 head):

```
=== UP ===   x5y6z7a8b9c0 applied (5 cols + 2 indexes added)
=== DOWN === Sprint-2 cols + indexes dropped via batch_alter (SQLite FK-safe)
=== UP AGAIN === re-applied cleanly via add_column path
```

**Status: clean round-trip.** Same constraint as Sprint 1 — full chain runs into the Sprint-3-era `api_sessions.ip_address INET` Postgres-only column on SQLite; verified migration in isolation per contract instructions.

## FeeCollection schema delta

Existing schema (pre-Sprint 2): `id, source_type, source_id, amount, token, usd_value_at_collection, status, collection_tx_hash, withdrawn_at, created_at`.

Sprint 2 added 5 new nullable columns:

| Column | Type | Purpose |
|---|---|---|
| `payer_agent_id` | UUID FK→agents.id | Per-agent revenue rollup |
| `amount_piconero` | BigInt | Integer fee amount (avoids float drift) |
| `rate_applied_bps` | Int | 30 (Free) or 10 (Pro+) — audit of applied rate |
| `transaction_ref` | String(255) | Transaction tx_hash linkage |
| `collected_at` | TIMESTAMPTZ | Aggregation column for MTD queries |

Two new indexes: `ix_fee_collections_payer_collected` and `ix_fee_collections_collected_at`.

**Compatibility:** all new columns are NULL-able. Legacy `fee_collector.py` writes (1% flat hub-routing fee) continue to work unchanged — they leave the new columns NULL. Verified by `test_legacy_fee_collection_columns_still_writable`.

## Deviations from contract (with justification)

1. **Contract names `compute_fee(amount, agent_tier)` with `AgentTier.PRO`. The codebase's existing enum has `FREE / VERIFIED / PREMIUM / ENTERPRISE` (no `PRO`).** Renaming the enum globally would touch every persisted row and ~20 callers — out of scope and risky. **Resolution:** all non-FREE tiers map to the Pro rate (10 bps); the contract's intent ("Pro+ pays 0.1%") is preserved exactly. `FREE_TIER_RATE_BPS = 30` and `PRO_TIER_RATE_BPS = 10` constants ship as named per contract. Tests use `VERIFIED`, `PREMIUM`, `ENTERPRISE` and all return 10 bps.

2. **Contract says modify `transaction_repo.create_transaction(...)`. The actual function is `TransactionRepository.create(...)` and has 8 callers across services and routers.** Modifying its signature in-place would require updating all callers in this commit, breaking d=1 dependents that are not on the commission path (e.g. `deposit_monitor.create()` for incoming deposits — should NOT pay commission). **Resolution:** added `TransactionRepository.create_with_commission(...)` as a sibling method. Existing `create()` stays untouched. Sprint 3 (tier enforcement) will migrate the hub-routing path in `_execute_hub_transfer` to call the new method.

3. **Concurrency test: SQLite-in-memory cannot truly run two threads against one connection.** Per-request connections in SQLAlchemy with StaticPool serialize automatically; two-thread testing produces brittle races on test infra. **Resolution:** the concurrency test runs two sequential transfers and asserts the no-double-charge invariant (each transfer produces exactly one fee row, balances accurate after both). On Postgres production, `BalanceRepository._get_for_update` applies `SELECT ... FOR UPDATE` which provides true serialization — the same invariant the test verifies. Documented in test docstring. Bonus test `test_concurrent_transfers_one_rejected_no_double_fee` covers the case where second transfer is rejected on insufficient balance.

4. **Migration round-trip in isolation, not full chain.** Same as Sprint 1 — Sprint-3-era `api_sessions.ip_address INET` is Postgres-only and breaks SQLite chain replay. Full-chain verification will run in CI once Postgres is available.

## Anti-fantasy verification

- `compute_fee` uses Decimal + ROUND_HALF_UP, returns `int`. **No float anywhere.** Test `test_no_float_arithmetic` asserts.
- Sender balance check is INSIDE the same DB session (rolled back together on raise). Test `test_insufficient_balance_for_fee_blocks_transfer` verifies no partial mutation.
- Idempotency: replay returns cached Transaction without inserting a 2nd FeeCollection row. Test `test_idempotency_replay_returns_cached_no_re_deduct` verifies.
- All 27 new tests run in 0.44s. Full suite verified at 2839 passed.

## Next-sprint hand-off notes (iteration 1 — superseded)

- ~~The hub-routing handler in `api/routers/payments.py::_execute_hub_transfer` still uses the legacy `BalanceRepository.deduct` path.~~ **Cut over in iteration 2 — see below.**
- `tier_cache.begin_request` / `end_request` are not yet wired into the FastAPI middleware — fallback (direct DB read) keeps correctness. Wire them in Sprint 3 when middleware lands.
- The legacy 1% hub fee in `fee_collector.py` and the new 0.3%/0.1% commission both write to `fee_collections`; admin revenue queries in Sprint 4 should filter by `rate_applied_bps IS NOT NULL` for new commission revenue.

---

## Iteration 2 — hub-routing cutover (commit `b1d05a3`)

**Trigger:** Independent Evaluator HIGH H1 — production hub-routing path was still calling legacy `tx_repo.create()` via `_execute_hub_transfer`, charging the legacy 1% fee through `fee_collector.confirm_hub_route`. Commission was unreachable from real traffic; Sprint 2 value undelivered.

### What changed

- **`api/routers/payments.py::_execute_hub_transfer` cut over** to call `TransactionRepository.create_with_commission(...)` for the balance + fee work. Legacy calls removed:
  - `balance_repo.deduct(agent.id, total_deduction)` — gone (now done atomically by repo).
  - `balance_repo.credit(_UUID(recipient.id), amount)` — gone (now done by repo).
  - `collector.confirm_hub_route(route["payment_id"], db=db)` — **gone** (this was inserting the legacy 1% FeeCollection row that would have stacked on top of the new commission).
  - `tx_repo.create(...)` — gone (commission method now creates Transaction).
- **HubRoute row preserved** for admin dashboard / `/payments/{id}` lookup / review service. Status flipped to `CONFIRMED` inline (without the legacy `confirm_hub_route` side effect of inserting a FeeCollection).
- **`fee_info` mutated to surface the real commission fee** on the response so clients see the new rate (0.3%/0.1%), not the misleading legacy 1%.
- **`create_with_commission` switched to XMR-Decimal arithmetic** internally (matches existing `BalanceRepository` ledger schema). Piconero still the public API; conversion happens at the boundary.
- **Test seed-helpers `_make_agent` / `_balance_for`** now convert piconero ↔ XMR-Decimal at the boundary so Sprint-2 unit tests' piconero assertions still hold.
- **`tests/test_cycle5_fixes.py::test_execute_hub_transfer_checks_duplicate_before_balance`** updated — it was asserting the legacy `balance_repo.deduct` token; now asserts the same invariant against the new `create_with_commission` token. Behavior guarded; only the implementation token changed.

### gitnexus_impact on `_execute_hub_transfer`

```
risk: LOW
direct callers (d=1): 1 — send_hub_routed_payment (api/routers/payments.py)
processes affected: 1 (send_hub_routed_payment), 5 hits, earliest_broken_step=1
modules affected: 1 (Routers)
```

`send_hub_routed_payment` is unchanged at the call site (still passes `(db, agent, recipient, amount, fee_info, req, idempotency_key, fee_collector)`). Internal contract preserved; no caller updates needed.

(Note: GitNexus index is stale post-commit per the hook reminder; the impact run above was performed before the commit landed and reflects the pre-commit graph.)

### Tests added (iteration 2)

3 new tests in `tests/test_commission_on_transfer.py::TestHubRoutingWiring`:

1. **`test_hub_transfer_charges_commission_only_free_tier`** — calls `_execute_hub_transfer` end-to-end with FREE sender, asserts:
   * exactly ONE FeeCollection row (no legacy 1% double-charge)
   * `rate_applied_bps == 30`
   * `amount_piconero == 3 * 10**9` (0.3% of 1 XMR)
   * sender balance after = `997_000_000_000` piconero (= 2 XMR − 1 XMR − 0.003 XMR). If legacy 1% had also fired, balance would be `990_000_000_000`.
   * NO row with `source_type == "hub_routing" AND payer_agent_id IS NULL` (would be the legacy `confirm_hub_route` insert).
2. **`test_hub_transfer_charges_commission_only_pro_tier`** — same end-to-end test for VERIFIED tier; asserts `rate_applied_bps == 10` and balance = `999_000_000_000` piconero (0.1% rate).
3. **`test_hub_transfer_creates_transaction_and_hubroute_rows`** — verifies Transaction row (history surface) AND HubRoute row (admin dashboard surface) both exist post-cutover, with `HubRoute.status == CONFIRMED`.

### Fee-stack analysis (the actual bug)

**Pre-iteration-2 stacking confirmed real:** in iteration 1 the production path would have charged:

* `balance_repo.deduct(amount + 1% legacy_fee)` — at line 129 of `_execute_hub_transfer`
* `confirm_hub_route` — inserts FeeCollection row at 1%, marks `fee_collected=True`

If `create_with_commission` had been wired in addition, the **same Transaction would have been double-charged**: 1% + 0.3% (Free) = **1.3%** or 1% + 0.1% (Pro+) = **1.1%**. Iteration 1 saved the user from this only by accident — the new code wasn't wired into the hot path.

**Iteration-2 fix:** removed both `balance_repo.deduct` and `confirm_hub_route` from the hub-routing flow. Users now pay exactly the commission rate (0.3% Free / 0.1% Pro+) and only one FeeCollection row is inserted per transfer. Verified by the three new wiring tests' assertions of `count(FeeCollection) == 1` and balance-equality checks against the no-stacking expected value.

### Other 8 callers verified to stay on legacy `create()` (no commission)

Verified via grep — these paths are unchanged and intentionally do NOT pay commission:

| File | Caller | Why no commission |
|---|---|---|
| `sthrip/services/deposit_monitor.py:296` | incoming on-chain deposit | system credit, not user-to-user |
| `sthrip/services/recurring_service.py:94` | recurring schedule cron | platform-internal accounting |
| `sthrip/services/multi_party_service.py:157` | multi-party split | each leg is its own transfer; commission not yet supported (Sprint 3+) |
| `sthrip/services/conditional_payment_service.py:131` | conditional payout | future-Sprint feature; legacy fee structure |
| `sthrip/services/split_payment_service.py:99` | split payment | same as multi-party |
| `api/routers/balance.py:259` | manual deposit dev endpoint | system top-up |
| `api/routers/balance.py:393` | balance history listing | read-only path |
| `api/routers/payments.py:332` | payment-by-id lookup | read-only path |

Only the user-facing hub-routing flow (`/v2/payments/hub-routing`) is on the commission path. Sprint 3+ can extend coverage.

### Suite delta after iteration 2

| Metric | Iter 1 | Iter 2 |
|---|---|---|
| Passed | 2839 | **2842** |
| New tests vs baseline (2812) | +27 | **+30** |
| Failed | 24 (baseline) | **24 (baseline, identical set)** |

Failure-set diff vs Sprint-1 baseline: **empty** (verified via `diff <(sort iter2) <(sort baseline)` — zero output). Zero regressions.

### Final commit SHA: `b1d05a3`

---

## Honest open issues

- GitNexus index is stale from commit `49c66bb`; rerun `npx gitnexus analyze` before next sprint to refresh blast-radius checks.
- `tier_cache` middleware still not wired (Sprint 3 work).
- The 8 non-commission callers list above should be revisited in Sprint 3 — split-payment and multi-party in particular SHOULD probably charge commission per leg, but that needs a product call from Lead.
