# Sprint 3 Contract — Subscription Tier + Enforcement

> Phase 2 — Revenue. Pre-filled by Lead from product-spec.md (AD-5) and lead-decisions.md.
> Generator implements; independent Evaluator verifies against this contract.

## Lead clarification on tier naming (IMPORTANT — do not re-litigate)

The existing `AgentTier` enum is `{FREE, VERIFIED, PREMIUM}`. Sprint 2 wired commission rates: FREE → 30 bps, VERIFIED+PREMIUM → 10 bps. **Do NOT rename the enum.** Sprint 3 maps user-facing labels:

| Enum value  | User-facing label | Monthly $ | Limit (tx/month) |
|-------------|-------------------|-----------|------------------|
| `FREE`      | "Free"            | $0        | 100              |
| `VERIFIED`  | "Pro"             | $29       | unlimited        |
| `PREMIUM`   | "Enterprise"      | $999      | unlimited        |

API endpoints accept both forms (alias `pro` → `VERIFIED`, `enterprise` → `PREMIUM`) on the `POST /v2/me/upgrade` body.

## What Generator will build

### A. Schema additions

1. **Migration `migrations/versions/y6z7a8b9c0d1_tier_grace_and_stats.py`**:
   - Add column `agents.tier_grace_until TIMESTAMPTZ NULL` — set when subscription billing fails; auto-downgrade after this time
   - New table `agent_monthly_stats`:
     - `agent_id` (FK to agents, NOT NULL)
     - `month_start` (DATE, NOT NULL — first of month UTC)
     - `transaction_count` (INTEGER, NOT NULL DEFAULT 0)
     - `last_updated` (TIMESTAMPTZ, NOT NULL)
     - PK on `(agent_id, month_start)`
     - Index on `(agent_id, month_start DESC)`
   - Idempotent migration (`IF NOT EXISTS` / `IF EXISTS`)
   - Round-trip clean

### B. Stats counter

1. **`sthrip/services/agent_stats_service.py`** with:
   - `record_transaction(agent_id: str, db: Session) -> None` — upserts a row in `agent_monthly_stats` for current month, incrementing `transaction_count`. Idempotent under contention (use `INSERT ... ON CONFLICT (agent_id, month_start) DO UPDATE SET transaction_count = agent_monthly_stats.transaction_count + 1`).
   - `get_current_month_count(agent_id: str, db: Session) -> int` — returns count for current UTC month, 0 if no row.
   - `reset_for_month(month_start: date) -> int` — utility to clear/archive old months (callable by future cron — not required to wire in Sprint 3).

2. **Wire stats counter** into the commission path: `transaction_repo.create_with_commission` calls `record_transaction(sender_id, db)` AFTER fee row insert, BEFORE commit. Single DB transaction. Same lock semantics.

### C. Tier enforcement middleware

1. **`api/middleware/tier_limit.py`** — FastAPI middleware that:
   - Identifies the agent by API key / auth (use existing pattern from `api/middleware/`)
   - Reads `agent.tier`. If `FREE` → check current month count.
   - If FREE AND count >= 100 → return `429 Too Many Requests` with body:
     ```json
     {
       "error": "tier_limit_reached",
       "current_count": 100,
       "limit": 100,
       "upgrade_url": "/v2/me/upgrade",
       "message": "Free tier limit reached. Upgrade to Pro ($29/month) for unlimited transactions."
     }
     ```
   - If `VERIFIED` or `PREMIUM` → bypass (unlimited).
   - **Apply only to payment-creating endpoints** — list specific paths: `/v2/payments/hub-routing`, `/v2/payments/internal-transfer`, etc. (whatever creates Transactions). Do NOT apply to GET endpoints, balance lookups, marketplace browsing, admin, etc.
   - **Honor `tier_grace_until`** — if set and in future, treat agent as their declared tier (don't downgrade just because billing missed). If set and in past, treat as FREE.

2. Register middleware in `api/main_v2.py` after auth middleware.

### D. Self-service tier endpoints

1. **`POST /v2/me/upgrade`** — accepts `{tier: "PRO"|"ENTERPRISE"|"VERIFIED"|"PREMIUM"}`:
   - Validates input (case-insensitive, accepts both label and enum forms).
   - **Pro-rates** if mid-month: charge fraction of $29 / $999 in XMR equivalent (use existing `conversion_service.py` if present — if not, FAIL FAST with NotImplementedError; Sprint 4 wires actual billing).
   - For Sprint 3 scope: just changes `agent.tier` directly (Sprint 4 wires actual XMR deduction). Stub the billing call but log "TODO: Sprint 4 will deduct XMR".
   - Audit log entry: `tier_upgrade` with old → new tier.
   - Returns updated agent state.

2. **`POST /v2/me/downgrade`** — accepts target tier, validates it's lower. Same audit, refund unused portion to balance (Sprint 4 wires the refund; for Sprint 3 just returns).

3. **`GET /v2/me/tier`** — returns:
   ```json
   {
     "tier": "FREE",
     "label": "Free",
     "current_month_count": 42,
     "limit": 100,
     "remaining": 58,
     "tier_grace_until": null
   }
   ```

### E. Existing-agents grandfather invariant

- All existing agents have `tier = FREE` already (verified by Sprint 2 reading existing data) OR `tier = VERIFIED` from old logic. Per lead-decisions.md "ВСЕ grandfathered to Free": Sprint 3 migration should set `agents.tier = 'FREE' WHERE tier IS NULL`. Existing VERIFIED/PREMIUM agents keep their tier (those got it deliberately, presumably).
- Document this in migration comments.

## Specific testable acceptance criteria

Tests in `tests/test_tier_enforcement.py`:

1. **`test_free_tier_blocked_at_101st_transfer`** — seed FREE agent, simulate 100 successful transfers, assert 101st returns 429 with body matching contract format.

2. **`test_pro_tier_unlimited`** — seed VERIFIED agent, simulate 200 transfers, all succeed (or at least 101 to prove no 100-cap).

3. **`test_enterprise_tier_unlimited`** — same for PREMIUM.

4. **`test_upgrade_endpoint_changes_tier_FREE_to_PRO`** — POST `/v2/me/upgrade` with `{"tier": "PRO"}`. Assert agent.tier flipped to VERIFIED. Assert audit log entry created.

5. **`test_upgrade_endpoint_accepts_label_or_enum`** — `"pro"`, `"PRO"`, `"VERIFIED"`, `"verified"` all map to VERIFIED.

6. **`test_downgrade_endpoint`** — VERIFIED agent downgrades to FREE; tier flipped; audit logged.

7. **`test_get_tier_returns_usage_stats`** — `GET /v2/me/tier` returns current_month_count, limit, remaining, label.

8. **`test_month_rollover_resets_count`** — seed agent with 50 transfers in May, advance time to June 1, assert June count starts at 0 (next transfer increments to 1, not 51).

9. **`test_429_response_includes_upgrade_hint`** — body has `upgrade_url`, `message`, `limit`.

10. **`test_grace_period_preserves_tier`** — VERIFIED agent with `tier_grace_until = now + 3 days`, current count irrelevant, assert NOT 429 (treat as VERIFIED for limit check).

11. **`test_grace_expired_treats_as_free`** — VERIFIED agent with `tier_grace_until = now - 1 hour`, simulate 101st transfer, assert 429 (grace expired = effectively FREE for enforcement).

12. **`test_record_transaction_idempotent_under_concurrency`** — sequential simulation of two simultaneous transfers from same agent, assert count increments by exactly 2 (not 1 due to race).

13. **`test_stats_counter_only_on_commission_path`** — direct `tx_repo.create()` (legacy path, system ops) does NOT increment count. Only `create_with_commission` does. Verify by calling each and checking count.

14. **`test_existing_FREE_agents_grandfathered`** — migration test: seed agents with `tier = NULL`, run migration, all become FREE.

## How success is verified

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip" && source .venv/bin/activate
pytest tests/test_tier_enforcement.py -v --tb=short 2>&1 | tail -50
pytest tests/test_commission_on_transfer.py tests/test_fee_calculator.py -v --tb=short 2>&1 | tail -20
timeout 600 pytest tests/ -q --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py 2>&1 | tail -30
```

Migration round-trip in isolation: same pattern as prior sprints.

## Risk callouts (Generator MUST address)

- **Middleware order**: tier_limit must run AFTER auth middleware (need agent_id) but BEFORE the payment handler. Test wiring with curl/integration test.
- **Concurrency on counter**: `INSERT ... ON CONFLICT DO UPDATE` is the atomic primitive. Don't use SELECT-then-UPDATE pattern.
- **Backward compat**: existing payment tests (Sprint 2 et al) MUST keep passing. If they create transactions without going through middleware, fine — middleware is request-level. If they go through HTTP integration, may need to seed the agent's tier or stats.
- **Don't break legacy `tx_repo.create()` callers** — they should NOT increment `agent_monthly_stats` (system ops). Stats counter is wired ONLY to `create_with_commission`.
- **Stub billing**: explicit `# TODO: Sprint 4 wires XMR deduction` comments in upgrade/downgrade handlers. Don't fake it.

## Out of scope (Sprint 3)

- Actual XMR deduction for subscriptions (Sprint 4)
- Monthly billing cron (Sprint 4)
- Grace period auto-downgrade cron (Sprint 4)
- Admin revenue dashboard (Sprint 4)
- TEE migration (Sprints 5-7)

## Branch and commit

- Branch: `feat/revenue-and-tee`.
- Single commit (or 2 if natural split): `feat(revenue): subscription tier enforcement + self-service endpoints (Phase 2 Sprint 3)`.
- No push.
