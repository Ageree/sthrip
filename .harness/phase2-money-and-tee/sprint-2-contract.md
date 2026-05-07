# Sprint 2 Contract — Commission on Transfers

> Phase 2 — Revenue. Pre-filled by Lead from product-spec.md (AD-3) and lead-decisions.md.
> Generator implements; independent Evaluator verifies against this contract.

## What Generator will build

### A. Fee calculator

1. **`sthrip/services/fee_calculator.py`** with:
   - `compute_fee(amount: int, agent_tier: AgentTier) -> int` — returns piconero fee.
     - Free → `max(amount * 0.003, 1)` (0.3%)
     - Pro / Enterprise → `max(amount * 0.001, 1)` (0.1%)
     - Floor: 1 piconero (atomic unit), guaranteed even on dust.
     - Use integer arithmetic / Decimal — NO float. Round half-up to nearest piconero.
   - Module-level constants: `FREE_TIER_RATE_BPS = 30` (basis points), `PRO_TIER_RATE_BPS = 10`, `MIN_FEE_PICONERO = 1`.

2. **`AgentTier` enum**: must align with existing `Agent.tier` field. If `AgentTier` enum doesn't exist yet, add it in `sthrip/db/enums.py` with values `FREE`, `PRO`, `ENTERPRISE`. Default value FREE.

### B. Commission deduction at write time

1. **Modify `sthrip/db/transaction_repo.create_transaction(...)`** (or whichever function writes a Transaction row from the payment hub-routing path). Logic to add **after envelope encryption, before commit**:
   - Look up sender's `Agent.tier` (cached if request_id present in context, else direct DB read).
   - Compute fee via `fee_calculator.compute_fee(amount, tier)`.
   - Verify sender balance >= amount + fee. If not → raise `InsufficientBalanceError` (existing or new).
   - Deduct `fee` from sender balance (atomic, same transaction as transfer).
   - Insert row in `fee_collections(payer_agent_id, amount_piconero, rate_applied_bps, transaction_ref, collected_at)`.
   - Receiver receives the original `amount` (full amount, no skim from receiver).
2. Use `SELECT ... FOR UPDATE` or PostgreSQL advisory lock per agent to prevent race on concurrent transfers (per existing patterns).

### C. Migration

- **`migrations/versions/x5y6z7a8b9c0_fee_collections.py`** — verify or extend the `fee_collections` table:
  - If table already exists (per Sprint 1 plan note re: existing `fee_collector` tests), inspect schema. Add any missing columns: `id`, `payer_agent_id` (FK to agents), `amount_piconero` (BigInt, NOT NULL), `rate_applied_bps` (Int, NOT NULL — stores 30 or 10), `transaction_ref` (String, FK or text reference to transaction), `collected_at` (TIMESTAMPTZ, NOT NULL, default now()).
  - Indexes: `(payer_agent_id, collected_at DESC)` for per-agent revenue queries; `(collected_at)` for global MTD queries.
  - Migration round-trip clean (up → down → up).
  - Idempotent guards (`IF NOT EXISTS` / `IF EXISTS`).

### D. Tier lookup caching

- **`sthrip/services/tier_cache.py`** (new) or extend existing request context module:
  - In-request memoization of `agent.tier` by `agent_id`. Cleared on request end.
  - Fallback: if no request context, do direct DB read (don't fail, just slower).
  - Must NOT cache across requests (tier changes mid-month are honored on next request).

### E. Existing fee_collector compatibility

- Existing `tests/test_fee_collector.py` — must remain green. If logic conflicts → adapt the new code to consume the existing collector, not rebuild. Read the existing module FIRST.

### F. THREAT_MODEL.md update

- Add to revenue section: "0.3% Free / 0.1% Pro+ commission on internal transfers; deducted from sender at write time; recorded in fee_collections aggregation table; per-agent caching prevents tier-bypass across single request."

## Specific testable acceptance criteria

Tests in `tests/test_fee_calculator.py` and `tests/test_commission_on_transfer.py`:

1. **`test_free_tier_pays_03_percent`** — `compute_fee(100_000_000, AgentTier.FREE)` (0.1 XMR) = `300_000` piconero (0.0003 XMR exactly).

2. **`test_pro_tier_pays_01_percent`** — `compute_fee(100_000_000, AgentTier.PRO)` = `100_000` piconero.

3. **`test_enterprise_tier_pays_01_percent`** — `compute_fee(100_000_000, AgentTier.ENTERPRISE)` = `100_000` piconero (same as Pro).

4. **`test_dust_transfer_pays_floor`** — `compute_fee(100, AgentTier.FREE)` = 1 piconero (floor enforced; 0.003 * 100 = 0.3 → rounded but floored to 1).

5. **`test_zero_amount_pays_floor`** — `compute_fee(0, AgentTier.FREE)` = 1 piconero (consistency: even zero pays minimum).

6. **`test_no_float_arithmetic`** — assertion that compute_fee result is `int` type, not `float`. Use Decimal or integer math internally.

7. **`test_commission_deducted_from_sender_at_create`** — full integration: seed sender with 1_000_000 piconero, FREE tier, create transaction of 100_000 to receiver. Assert sender balance afterward = 1_000_000 - 100_000 - 300 = 899_700. Assert receiver balance = 100_000 (full amount, no skim).

8. **`test_commission_deducted_pro_tier`** — same setup, PRO tier sender, fee = 100 piconero. Sender balance afterward = 1_000_000 - 100_000 - 100 = 899_900.

9. **`test_fee_collection_row_inserted`** — after transaction create, query fee_collections; assert one row exists with correct payer_agent_id, amount_piconero=300 (FREE 0.3% of 100k), rate_applied_bps=30, collected_at recent.

10. **`test_insufficient_balance_for_fee_blocks_transfer`** — sender has exactly `amount` but not `amount + fee`. Transaction creation raises InsufficientBalanceError. No partial deduction (balance unchanged).

11. **`test_concurrent_transfers_no_double_fee`** — two threads attempt simultaneous transfers from same sender. Both succeed (or one fails on lock); fee_collections has exactly 2 rows (or 1 if one transfer rejected); no double-charge.

12. **`test_existing_fee_collector_tests_still_green`** — meta-test or just re-running: existing `tests/test_fee_collector.py` continues to pass after Sprint 2 changes.

## How success is verified

Evaluator runs:

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
source .venv/bin/activate
pytest tests/test_fee_calculator.py tests/test_commission_on_transfer.py tests/test_fee_collector.py -v --tb=short 2>&1 | tail -50
timeout 600 pytest tests/ -q --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py 2>&1 | tail -30
alembic upgrade x5y6z7a8b9c0 && alembic downgrade -1 && alembic upgrade x5y6z7a8b9c0
```

All criteria pass. No regressions vs Sprint 1 baseline (2812 passed / 24 pre-existing failures).

## Risk callouts (Generator MUST address)

- **HIGH risk: edits payment hot path.** Run `gitnexus_impact({target: "create_transaction", direction: "upstream"})` BEFORE editing. Report blast radius. Update all d=1 callers if signature changes.
- **Race condition**: per-agent advisory lock (or `SELECT FOR UPDATE`) is mandatory. Test #11 verifies.
- **No double-deduction**: ensure idempotency_key check happens BEFORE fee deduction. If retry hits same idempotency_key → return cached result, do NOT re-deduct.
- **Existing fee_collector**: read first, integrate; don't duplicate.
- **Tier enum location**: if `AgentTier` doesn't exist, add to `sthrip/db/enums.py` (don't scatter).
- **Migration on Postgres only**: same constraint as Sprint 1 (Postgres-only INET in earlier migrations). Verify in isolation OK.

## Out of scope (Sprint 2)

- Subscription billing (Sprint 4).
- Tier enforcement middleware (Sprint 3).
- Admin revenue dashboard (Sprint 4).
- Tier upgrade endpoint (Sprint 3).
- TEE migration (Sprints 5-7).

## Branch and commit

- Branch: `feat/revenue-and-tee` (already on it).
- Single commit: `feat(revenue): commission on transfers (0.3% Free / 0.1% Pro+, Phase 2 Sprint 2)`.
- No push. Lead handles merge after all 7 sprints.
