# Sprint 4 Contract — XMR Billing Cron + Grace Handling

> Phase 2 Sprint 4 — last sprint of Revenue phase. Pre-filled by Lead from product-spec.md (AD-4) and lead-decisions.md.
> Generator implements; independent Evaluator verifies.

## Scope reminder

Sprint 3 stubbed `/v2/me/upgrade` and `/v2/me/downgrade` with `# TODO Sprint 4 wires XMR deduction`. Sprint 4 wires the actual money flow:

- Monthly billing cron (1st of each month UTC) deducts subscription cost in XMR from agent balance
- Insufficient balance → set `tier_grace_until = now + 7d`, retry daily
- Grace expired → auto-downgrade to FREE, audit logged
- Mid-month upgrade pro-rates the charge
- Mid-month downgrade refunds unused portion to balance
- Live USD→XMR rate via existing conversion service or new lightweight cache

## What Generator will build

### A. Subscription billing service

1. **`sthrip/services/subscription_billing_service.py`** with:
   - `bill_pro_subscriptions(now: datetime, db: Session) -> dict` — finds all VERIFIED + PREMIUM agents whose next billing date is `<= now`, deducts USD-equivalent XMR from balance, inserts row in `agent_billing_history`, sets next billing date to first-of-next-month.
   - `handle_grace_expiry(now: datetime, db: Session) -> dict` — finds all agents with `tier_grace_until < now`, downgrades to FREE, audit log.
   - `start_grace_period(agent_id: str, db: Session) -> None` — invoked by `bill_pro_subscriptions` on insufficient balance: sets `tier_grace_until = now + 7d`, audit "grace_started" event.
   - `prorate_charge(monthly_cost_usd: Decimal, day_of_month: int, days_in_month: int) -> Decimal` — for mid-month upgrade. Returns fraction of monthly cost.
   - `compute_refund(monthly_cost_usd: Decimal, day_of_month: int, days_in_month: int) -> Decimal` — for mid-month downgrade. Returns unused portion.

2. Tier costs (constants in module):
   - `VERIFIED_USD_MONTHLY = Decimal("29")`
   - `PREMIUM_USD_MONTHLY = Decimal("999")`
   - `FREE_USD_MONTHLY = Decimal("0")`
   - `GRACE_PERIOD_DAYS = 7`

### B. Live USD→XMR rate

1. Reuse `sthrip/services/conversion_service.py` if it exists. If not:
   - Add minimal `conversion_service.py` with:
     - `get_xmr_usd_rate() -> Decimal` — returns cached rate, fetches fresh if stale
     - In-memory or Redis cache, 5-minute TTL
     - Source: CoinGecko free tier (`/api/v3/simple/price?ids=monero&vs_currencies=usd`), no API key needed
     - On API failure: return last cached rate up to 24h old, then raise `RateUnavailableError`
   - Add tests: `test_rate_cache_hit`, `test_rate_cache_expired_refetches`, `test_rate_api_failure_uses_stale_cache`, `test_rate_unavailable_24h_raises`

2. `usd_to_xmr_piconero(usd: Decimal) -> int` helper — multiplies by current rate, converts XMR → piconero (10^12), returns integer piconero.

### C. Wire upgrade/downgrade endpoints

1. **`POST /v2/me/upgrade`** (modify Sprint 3 stub):
   - Compute pro-rated charge for current month based on day_of_month / days_in_month
   - Convert USD → piconero at current rate
   - Verify balance >= charge; if not → 402 Payment Required, do NOT change tier
   - Deduct from balance (atomic with tier change)
   - Insert row in `agent_billing_history` (status=`upgrade_charge`, amount_usd, amount_piconero, rate_applied)
   - Set `agent.tier`, clear `tier_grace_until`
   - Audit log `tier_upgrade` with old → new + amount

2. **`POST /v2/me/downgrade`**:
   - Compute refund for unused portion of current billing period
   - Credit refund to balance
   - Insert row in `agent_billing_history` (status=`downgrade_refund`)
   - Set `agent.tier`, clear `tier_grace_until`
   - Audit log

### D. Schema

1. **Migration `migrations/versions/z7a8b9c0d1e2_billing_history.py`**:
   - New table `agent_billing_history`:
     - `id` BigInt PK
     - `agent_id` (FK)
     - `month_start` DATE NOT NULL — for monthly cron rows
     - `amount_usd` NUMERIC(12,2) NOT NULL
     - `amount_piconero` BigInt NOT NULL
     - `rate_applied` NUMERIC(20,8) NOT NULL — XMR/USD rate used
     - `status` String — values: `monthly_charge`, `monthly_grace_started`, `monthly_grace_retry`, `monthly_grace_expired_downgrade`, `upgrade_charge`, `downgrade_refund`, `monthly_failure_alerted`
     - `created_at` TZ-aware
     - `tier_at_event` String NOT NULL — captured tier at billing time for audit
   - Index `(agent_id, created_at DESC)` for per-agent history; `(status, created_at)` for ops dashboard.
   - Round-trip clean.

### E. Scheduler entries

- Existing scheduler (per Sprint 1 added daily 03:00 UTC purge job and 03:05 UTC canary job).
- Add: monthly billing on **1st of each month at 04:00 UTC** (pick a different minute from purge/canary to avoid collision)
- Add: daily 04:30 UTC `handle_grace_expiry`
- Idempotent: re-running same day must not double-charge. Use `status=monthly_charge` + `month_start` uniqueness check.

### F. THREAT_MODEL.md / PRIVACY_FEATURES.md updates

- Add note: subscription billing is custodial, charges happen in plaintext at hub level, but: amounts and rates are stored only in `agent_billing_history` with retention subject to Phase 1 auto-purge (default 60d).

## Specific testable acceptance criteria

Tests in `tests/test_subscription_billing.py`:

1. **`test_pro_agent_charged_29_usd_in_xmr_at_rate`** — seed VERIFIED agent with balance, mock rate at 1 XMR = $200, run `bill_pro_subscriptions`. Assert balance reduced by $29/200 = 0.145 XMR = 145_000_000_000 piconero. agent_billing_history row inserted with status=monthly_charge.

2. **`test_enterprise_agent_charged_999_usd_in_xmr`** — same logic, $999.

3. **`test_insufficient_balance_starts_grace_period`** — VERIFIED agent with balance < charge. Assert balance unchanged, `tier_grace_until = now + 7d`, status=`monthly_grace_started` history row.

4. **`test_grace_expiry_downgrades_to_free`** — agent with `tier_grace_until = now - 1h`, run `handle_grace_expiry`. Assert tier=FREE, history row `monthly_grace_expired_downgrade`.

5. **`test_balance_topped_up_during_grace_resumes_pro`** — agent in grace, balance topped up before expiry, next daily retry → charge succeeds, `tier_grace_until` cleared.

6. **`test_proration_on_mid_month_upgrade`** — `prorate_charge($29, day_of_month=15, days_in_month=30)` = `$29 * 16/30` ≈ `$15.47` (16 days remaining = day 15 inclusive through day 30 = 16 days; or document a different convention). Use Decimal, round to 2 places.

7. **`test_refund_on_mid_month_downgrade`** — `compute_refund($29, day_of_month=15, days_in_month=30)` returns same proration value (the unused remainder).

8. **`test_upgrade_endpoint_charges_xmr`** — full integration: agent calls POST /v2/me/upgrade with `{tier: PRO}`. Assert balance deducted, tier flipped, history row, audit event.

9. **`test_upgrade_endpoint_402_when_insufficient`** — agent with no XMR. Assert 402 response, tier UNCHANGED.

10. **`test_downgrade_endpoint_refunds_xmr`** — VERIFIED agent on day 15 calls downgrade. Assert balance credited, tier=FREE, history row.

11. **`test_idempotent_cron_run_same_day`** — call `bill_pro_subscriptions` twice on same day. Assert ONE charge happened (not two).

12. **`test_rate_cache_hit_avoids_api_call`** — mock CoinGecko, call `get_xmr_usd_rate` twice within 5 min. Second call uses cache (no second API hit).

13. **`test_rate_unavailable_24h_raises`** — mock cache populated >24h ago, mock API failing, call rate. Expect `RateUnavailableError`.

14. **`test_billing_uses_atomic_transaction`** — simulate failure mid-billing (e.g., raise after balance deduct, before history insert). Assert rollback: balance unchanged, no history row.

15. **`test_billing_skips_FREE_agents`** — seed FREE + VERIFIED agents, run cron, only VERIFIED gets charged.

## How success is verified

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip" && source .venv/bin/activate
pytest tests/test_subscription_billing.py -v --tb=short 2>&1 | tail -50
pytest tests/test_tier_enforcement.py tests/test_commission_on_transfer.py -v --tb=short 2>&1 | tail -30
timeout 600 pytest tests/ -q --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py 2>&1 | tail -30
```

Migration round-trip in isolation.

## Risk callouts (Generator MUST address)

- **External API dependency**: CoinGecko outage MUST NOT cause cascading downgrades. Cache TTL up to 24h, then raise (don't silently use ancient rate). Document in code.
- **Idempotency on cron**: month_start + status check is the lock. NEVER use SELECT-then-INSERT for charge state — use `INSERT ... ON CONFLICT DO NOTHING` or check unique constraint.
- **Atomic billing**: balance deduct + history insert + tier change must be ONE DB transaction. Test #14 verifies.
- **No skim from receiver**: this is subscription billing (operator collects), not a transfer between agents. The deduct goes from agent → operator account (or just out of agent's balance — internal accounting can be tracked elsewhere). Don't reuse the commission `fee_collections` table; use `agent_billing_history`.
- **Pro-rate convention**: lead-decisions.md says "Если агент upgrade'нулся mid-month — pro-rate." Use `(days_remaining_in_month / days_in_month) * monthly_cost`. Document convention precisely.
- **Test isolation**: don't actually hit CoinGecko in tests. Use `respx`/`unittest.mock` for the HTTP layer.

## Out of scope

- Annual prepay discount (lead-decisions: "не в этом цикле")
- Refund to bank/card (custodial XMR balance only)
- TEE migration (Sprints 5-7)
- Admin revenue dashboard ("позже" per product-spec — could be brief addition if time permits, but not required for Sprint 4 PASS)

## Branch and commit

- Single commit: `feat(revenue): XMR subscription billing cron + grace handling (Phase 2 Sprint 4)`
- No push.
