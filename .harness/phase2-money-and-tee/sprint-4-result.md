# Sprint 4 Evaluator Result — XMR Billing Cron + Grace Handling

**Verdict: PASS**
**Commit:** `959377a6ab01613c5a19b270e773d72b7e34f026`
**Branch:** `feat/revenue-and-tee`
**Evaluated:** 2026-05-07 (independent context, no Generator history)

---

## Contract scoring (15 named tests + 5 architecture criteria)

### A. Code presence

| Item | Status | Evidence |
|---|---|---|
| `subscription_billing_service.py` w/ all 5 funcs | PASS | All exported in `__all__` (lines 453-466). `bill_pro_subscriptions`, `handle_grace_expiry`, `start_grace_period`, `prorate_charge`, `compute_refund` all present. |
| `xmr_rate_service.py` (new file) | PASS | Generator chose to keep separate from `conversion_service.py` (which serves stablecoin conversions with fallback rates). Separation is sound — different SLA contracts (5min/24h vs fallback). |
| Migration `z7a8b9c0d1e2_*.py` | PASS | Created `agent_billing_history` w/ partial unique idx on Postgres + composite idx on SQLite. Idempotent (table-exists guard). |
| `/v2/me/upgrade` and `/v2/me/downgrade` actually deduct/refund XMR | PASS | `agents.py:788-869` (upgrade) charges via `BalanceRepository.deduct`. `agents.py:872-965` (downgrade) credits via `.credit`. No stubs. |
| Scheduler entries 04:00 + 04:30 UTC | PASS | `main_v2.py:608-704` `_subscription_billing_loop` (1st of month 04:00) + `_grace_expiry_loop` (daily 04:30). Both registered in lifespan + cleanly cancelled on shutdown. |

### B. Atomic billing

PASS. `bill_pro_subscriptions` (lines 262-350):
- Single transaction wraps deduct → flush → history insert → audit (no intermediate commit).
- On insufficient balance: `BalanceRepository.deduct` raises `ValueError` BEFORE any history row → `start_grace_period` runs in a clean state (sets `tier_grace_until` + writes `monthly_grace_started` row). No partial-charge state possible.
- Idempotency: `_has_monthly_charge` (lines 131-147) checks `(agent_id, month_start, status='monthly_charge')`. Postgres backstop is `uq_agent_billing_monthly_charge` partial unique index.

### C. CoinGecko rate behavior

PASS. `xmr_rate_service.py:128-159`:
- 5-min `RATE_CACHE_TTL` on success — cache hit returns immediately.
- On API failure: stale cache used IFF `< RATE_STALE_LIMIT` (24h).
- After 24h staleness + API down: raises `RateUnavailableError`.
- Tests use `respx` to mock httpx — no real network calls.

### D. Endpoints

PASS. `agents.py`:
- 402 on insufficient balance (line 813), tier UNCHANGED (deduct raises before tier flip).
- Pro-ration formula: `(days_remaining_inclusive / days_in_month) * monthly_cost` quantized HALF_UP to 2 decimal places — matches contract's day-15 example `$29 * 16/30 = $15.47`.
- Refund formula matches (uses same `prorate_charge`).
- Tier change atomic with charge/refund (single DB session, balance op + tier mutation + history insert before commit).
- `tier_grace_until = None` cleared on successful upgrade (line 826) AND downgrade (line 928).
- 503 on `RateUnavailableError` for upgrade. Downgrade path falls through with zero refund + log if rate unavailable (defensible — don't block downgrade on external feed).

### E. Tests run independently

```
tests/test_subscription_billing.py::test_pro_agent_charged_29_usd_in_xmr_at_rate    PASSED
tests/test_subscription_billing.py::test_enterprise_agent_charged_999_usd_in_xmr   PASSED
tests/test_subscription_billing.py::test_insufficient_balance_starts_grace_period  PASSED
tests/test_subscription_billing.py::test_grace_expiry_downgrades_to_free           PASSED
tests/test_subscription_billing.py::test_balance_topped_up_during_grace_resumes_pro PASSED
tests/test_subscription_billing.py::test_proration_on_mid_month_upgrade            PASSED
tests/test_subscription_billing.py::test_refund_on_mid_month_downgrade             PASSED
tests/test_subscription_billing.py::test_upgrade_endpoint_charges_xmr              PASSED
tests/test_subscription_billing.py::test_upgrade_endpoint_402_when_insufficient    PASSED
tests/test_subscription_billing.py::test_downgrade_endpoint_refunds_xmr            PASSED
tests/test_subscription_billing.py::test_idempotent_cron_run_same_day              PASSED
tests/test_subscription_billing.py::test_rate_cache_hit_avoids_api_call            PASSED
tests/test_subscription_billing.py::test_rate_unavailable_24h_raises               PASSED
tests/test_subscription_billing.py::test_billing_uses_atomic_transaction           PASSED
tests/test_subscription_billing.py::test_billing_skips_FREE_agents                 PASSED
============================== 15 passed in 0.50s ==============================
```

#### Spot-read of 3 critical tests

- `test_billing_uses_atomic_transaction` (line 617): patches `_post_charge_audit` to raise `RuntimeError` mid-flow, then asserts post-rollback balance == `Decimal("1")` (untouched) AND `AgentBillingHistory` rows == 0. Real rollback path exercised. **CONFIRMED.**
- `test_idempotent_cron_run_same_day` (line 532): calls `bill_pro_subscriptions` twice, commits between, asserts ONE `monthly_charge` row + balance debited exactly once (`0.855` after $29@$200 = 0.145 XMR). **CONFIRMED.**
- `test_rate_unavailable_24h_raises` (line 598): `XmrRateCache.set_for_test` with `fetched_at=now-25h`, mocks CoinGecko 503 via respx, `pytest.raises(RateUnavailableError)`. **CONFIRMED.**

### F. Sprint 3 carry-over (`tier_limit_fail_open_total`)

PASS. `api/middleware/tier_limit.py:115-133` — `_current_count` catches the DB exception, logs warning, AND increments `metrics.tier_limit_fail_open_total` via getattr+inc, with a nested try/except so a missing prometheus_client cannot break the middleware. Wired correctly.

### G. Sprint 3 regression

```
tests/test_tier_enforcement.py: 14 passed in 1.12s
```

Zero regressions. Generator added two additive lines to the Sprint 3 fixture (table reg + rate mock) so the now-charging endpoints don't try to hit the live network.

### H. Full suite

```
24 failed, 2871 passed, 21 skipped, 3015 warnings in 115.23s
```

Sprint 3 baseline: 2856 passed, 24 failed. Sprint 4: 2871 passed (+15), 24 failed. **Failure-set diff is empty** — same pre-existing failures (mcp_tools auth, e2e_production_readiness, migration_error_handling sentinels, production_fixes UUID, readiness_nonblocking, session_store Redis backend). **Zero regressions.**

### I. Migration round-trip

Clean: `stamp y6z7a8b9c0d1` → `upgrade z7a8b9c0d1e2` (creates table+indexes) → `downgrade -1` (drops them) → `upgrade z7a8b9c0d1e2` (recreates cleanly). Idempotent guards verified.

---

## Code review

### CRITICAL

None.

### HIGH

None.

### MEDIUM

1. **Per-replica in-memory rate cache** (`xmr_rate_service.py`). On a multi-replica deployment, every replica fetches CoinGecko independently. The doc admits this and notes the free-tier 50/min/IP budget absorbs it. Acceptable for current scale; consider Redis-backed cache when replica count grows. Not blocking.

2. **`AgentBillingHistory.id` is `Integer` in ORM, `BigInteger` in migration.** Generator documented this for SQLite-test compatibility. Production schema is BigInt as the contract requires; ORM-level Integer is the typical SQLAlchemy idiom for mixed dialects. Not blocking.

3. **Downgrade with `RateUnavailableError` writes a zero-refund row and proceeds.** Defensible (don't block downgrade on external feed) but the operator must reconcile manually. The audit row captures `rate_applied=0` so it's discoverable. Consider logging an explicit "downgrade_refund_deferred" status as a future enhancement.

### LOW / Nits

1. `start_grace_period` accepts many optional kwargs (`rate_applied`, `amount_usd`, `amount_piconero`, `tier_at_event`, `month_start`). For an internal helper this is a wide signature; could be condensed via a dataclass. Not blocking.
2. `_post_charge_audit` swallows audit exceptions with a `noqa: BLE001` warning — appropriate (audit must never crash billing) but worth a metric counter for ops visibility.

---

## Generator deviations (4 from report)

1. **Separate `xmr_rate_service.py` (not folded into `conversion_service.py`).** ACCEPTED — the existing conversion service has a fundamentally different rate-source contract (fallback rates for stablecoin conversions vs. live spot for billing). Mixing them would couple distinct SLAs.
2. **`AgentBillingHistory.id` ORM Integer / migration BigInteger split.** ACCEPTED — documented test-compat reason, production schema is correct.
3. **`respx` not in venv.** Test-time install only; already imported by `tests/test_cli_client.py`. Dev-deps gap, not a runtime change.
4. **Same-tier "upgrade" no-op.** ACCEPTED — sensible UX; no charge applied, no audit emitted.

---

## Final verdict

**PASS — Phase 2 Sprint 4 closes Phase 2 Revenue.**

- All 5 architecture criteria (A-E) satisfied.
- All 15 named tests pass.
- All 14 Sprint 3 regression tests pass.
- Sprint 3 carry-over (`tier_limit_fail_open_total`) wired correctly.
- Migration round-trip clean.
- Full suite: +15 tests, zero regressions, identical pre-existing failure set.
- No CRITICAL or HIGH findings; 3 MEDIUM are non-blocking architectural notes.

Phase 2 Revenue is complete. Ready to proceed to Phase 2 Sprints 5-7 (TEE migration).
