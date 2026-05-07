# Sprint 4 Generator Report — XMR Billing Cron + Grace Handling

**Branch:** `feat/revenue-and-tee`
**Baseline:** Sprint 3 head `dd29657`
**Commit SHA:** `959377a6ab01613c5a19b270e773d72b7e34f026`
**Phase:** 2 Sprint 4 — closes Phase 2 Revenue.

## Files added

- `sthrip/services/subscription_billing_service.py` — `bill_pro_subscriptions`,
  `handle_grace_expiry`, `start_grace_period`, `prorate_charge`,
  `compute_refund`, plus `record_upgrade_charge` /
  `record_downgrade_refund` helpers used by endpoints.
- `sthrip/services/xmr_rate_service.py` — CoinGecko free-tier USD/XMR
  fetcher, in-memory cache (5-min fresh, 24h stale, raises
  `RateUnavailableError` beyond), `usd_to_xmr_piconero` helper.
- `migrations/versions/z7a8b9c0d1e2_billing_history.py` — creates
  `agent_billing_history` table; partial unique index
  `uq_agent_billing_monthly_charge` on Postgres,
  `ix_agent_billing_monthly_charge_sqlite` composite on SQLite.
- `tests/test_subscription_billing.py` — 15 tests.

## Files modified

- `sthrip/db/models.py` — `AgentBillingHistory` ORM model. Note: PK uses
  `Integer` (SQLite-compatible autoincrement); migration uses BigInteger
  on Postgres. Added to test schema in `tests/conftest.py`.
- `api/routers/agents.py` — `/v2/me/upgrade` and `/v2/me/downgrade` now
  perform real XMR deduction / refund. Added `_billing_now()` test seam
  for proration determinism. Same-tier "upgrade" is a no-op (no charge).
  402 on insufficient balance (no tier change). Added
  `_tier_monthly_usd` helper sourced from
  `subscription_billing_service` constants.
- `api/main_v2.py` — `_subscription_billing_loop` (1st of month 04:00
  UTC) and `_grace_expiry_loop` (daily 04:30 UTC) following Sprint 1's
  `_purge_loop` pattern: `_seconds_until_utc` sleep, distributed Redis
  lease, single DB session per cycle. Lifespan registers both as tasks
  and shutdown cancels them.
- `api/middleware/tier_limit.py` — Sprint 3 carry-over: `_current_count`
  fail-open path now bumps `tier_limit_fail_open_total` Prometheus counter
  in addition to the existing warning log.
- `sthrip/services/metrics.py` — added `tier_limit_fail_open_total`,
  `subscription_billing_total` counters with no-op fallbacks.
- `tests/conftest.py` — added `AgentBillingHistory.__table__` to
  `_COMMON_TEST_TABLES`.
- `tests/test_tier_enforcement.py` — added `AgentBillingHistory` to the
  Sprint-3 fixture's table list (endpoints now write to it) and patched
  `get_xmr_usd_rate` to a constant so tests don't hit the network.
- `PRIVACY_FEATURES.md`, `docs/THREAT_MODEL.md` — noted billing data
  retention follows Phase 1 auto-purge.

Pip dep: `respx` was missing from the active venv; installed for tests
(already used in `tests/test_cli_client.py`, so this is a dev-deps gap,
not a runtime change).

## gitnexus_impact summary

`gitnexus_impact({target: "upgrade_tier", direction: "upstream", repo:
"sthrip"})` and the same for `downgrade_tier` returned **"target not
found"** — the index is stale (last analyzed commit `49c66bb`, predates
Sprint 3 dd29657 which introduced these endpoints). Manual review of
the call graph: both endpoints are HTTP-only routes registered on
`api/routers/agents.py:router`; no internal Python callers, only
external HTTP clients (Sprint 3 test fixture, future SDK). Blast radius
is **LOW** — additive XMR-charge behavior at d=0; the endpoints' return
shape is a strict superset of Sprint 3 (added `amount_charged_usd`,
`amount_charged_piconero`, `rate_applied`, `amount_refunded_usd`,
`amount_refunded_piconero`). The Sprint 3 tier_enforcement test fixture
needed two additive lines (table reg + rate mock) to keep passing —
caught and applied. Index re-analyze is queued as a separate
non-blocking task per the post-commit hook reminder.

## 15 test results (all PASS)

| Test | Result |
|---|---|
| `test_pro_agent_charged_29_usd_in_xmr_at_rate` | PASS |
| `test_enterprise_agent_charged_999_usd_in_xmr` | PASS |
| `test_insufficient_balance_starts_grace_period` | PASS |
| `test_grace_expiry_downgrades_to_free` | PASS |
| `test_balance_topped_up_during_grace_resumes_pro` | PASS |
| `test_proration_on_mid_month_upgrade` | PASS |
| `test_refund_on_mid_month_downgrade` | PASS |
| `test_upgrade_endpoint_charges_xmr` | PASS |
| `test_upgrade_endpoint_402_when_insufficient` | PASS |
| `test_downgrade_endpoint_refunds_xmr` | PASS |
| `test_idempotent_cron_run_same_day` | PASS |
| `test_rate_cache_hit_avoids_api_call` | PASS |
| `test_rate_unavailable_24h_raises` | PASS |
| `test_billing_uses_atomic_transaction` | PASS |
| `test_billing_skips_FREE_agents` | PASS |

Run: `pytest tests/test_subscription_billing.py -v --tb=short` →
`15 passed in 0.41s`.

## Sprint 3 carry-over

Done. `api/middleware/tier_limit.py:_current_count` now increments
`metrics.tier_limit_fail_open_total` whenever the DB lookup raises and
the middleware fails open. Counter is no-op if `prometheus_client` is
not installed (graceful degradation matches existing metrics module
pattern). Added `subscription_billing_total` counter as a parallel
billing-ops observability hook for ops dashboards.

## Suite delta

```
baseline (Sprint-3 head dd29657): 2856 passed, 24 failed, 21 skipped
Sprint-4 head 959377a:           2871 passed, 24 failed, 21 skipped (+15)
```

Failure-set diff vs Sprint 3 baseline: **empty** — all 24 failures are
the same pre-existing set (channel_api close-after-settlement,
e2e_production_readiness idempotency E2E mocks, mcp_tools 19-tool
count + auth requires-auth, migration_error_handling production-mode
sentinels, production_fixes UUID type-bound, readiness_nonblocking
wallet stub, session_store Redis backend). **Zero regressions.**

## Migration round-trip

```
=== UP ===   z7a8b9c0d1e2 applied (created agent_billing_history,
             3 indexes including ix_agent_billing_monthly_charge_sqlite)
=== DOWN === z7a8b9c0d1e2 reverted (dropped indexes + table)
=== UP AGAIN === re-applied cleanly, table + indexes present
```

SQLite isolation-mode round-trip clean. Same constraint as prior
sprints: full chain replay against SQLite hits the existing
`api_sessions.ip_address INET` column from a much earlier migration;
isolation-mode round-trip is the standard verification path documented
in Sprints 1-3.

## Anti-fantasy verification

- CoinGecko is **never hit during tests** — all 15 tests mock the
  `get_xmr_usd_rate` callsite (or use `respx` for the rate-cache tests
  that exercise the HTTP path).
- No `time.sleep(...)` in any test; the loops use `_seconds_until_utc`
  and the tests inject deterministic `now` parameters.
- Atomic-tx test (#14) verified: `_post_charge_audit` raises mid-flow,
  the whole block rolls back via the caller's transaction. Balance
  unchanged, history rows = 0.
- `RateUnavailableError` test (#13) seeds a 25h-old cache + 503 from
  CoinGecko → `pytest.raises` confirms the abort path is taken.
- 402 (#9) on insufficient balance verified — tier remains FREE in DB.
- Idempotency anchor test (#11) — two runs on the same UTC day yield
  exactly one `monthly_charge` row, balance debited exactly once.

## Endpoint behavior summary

- `POST /v2/me/upgrade` — accepts `{tier: "pro"|"enterprise"|...}`.
  Pro-rates `(days_remaining_inclusive / days_in_month) * monthly_cost`
  to 2 decimal places (HALF_UP). Converts USD → piconero at live rate.
  Atomic: deduct + tier flip + grace clear + history insert + audit.
  Returns `{tier, amount_charged_usd, amount_charged_piconero,
  rate_applied, ...}`. 402 on insufficient balance (tier UNCHANGED).
  503 on `RateUnavailableError`. Same-tier upgrade is a no-op.
- `POST /v2/me/downgrade` — refunds the same proration to balance.
  Returns `{tier, amount_refunded_usd, amount_refunded_piconero,
  rate_applied, ...}`. Continues with zero refund (logged) if rate is
  unavailable so the downgrade itself isn't blocked by an external
  feed outage.

## Scheduler entries

- `_subscription_billing_loop`: 1st of month at 04:00 UTC, 1h Redis
  lease TTL, no-op on non-1st days. Idempotent on
  `(agent_id, month_start, status='monthly_charge')`.
- `_grace_expiry_loop`: daily 04:30 UTC. First runs
  `bill_pro_subscriptions` (top-ups during grace get a successful
  retry) then `handle_grace_expiry`. 15-min Redis lease.
- Both registered in `_startup_services` and torn down cleanly in
  `_shutdown_services`.

## Deviations from contract

None functionally. Two test-deps notes:

1. `respx` was not in the active venv. Installed via
   `pip install respx`. Already imported by `tests/test_cli_client.py`,
   so it's a venv-state gap rather than a missing dependency.
2. `AgentBillingHistory.id` uses `Integer` not `BigInteger` in the ORM
   model so SQLite autoincrement works for in-memory tests; the
   migration uses `BigInteger` on Postgres so production capacity is
   unaffected. Documented inline in the model.

## Files (absolute paths)

- `/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/services/subscription_billing_service.py`
- `/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/services/xmr_rate_service.py`
- `/Users/saveliy/Documents/Agent Payments/sthrip/migrations/versions/z7a8b9c0d1e2_billing_history.py`
- `/Users/saveliy/Documents/Agent Payments/sthrip/api/routers/agents.py`
- `/Users/saveliy/Documents/Agent Payments/sthrip/api/main_v2.py`
- `/Users/saveliy/Documents/Agent Payments/sthrip/api/middleware/tier_limit.py`
- `/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/db/models.py`
- `/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/services/metrics.py`
- `/Users/saveliy/Documents/Agent Payments/sthrip/tests/test_subscription_billing.py`
- `/Users/saveliy/Documents/Agent Payments/sthrip/tests/test_tier_enforcement.py`
- `/Users/saveliy/Documents/Agent Payments/sthrip/tests/conftest.py`
- `/Users/saveliy/Documents/Agent Payments/sthrip/PRIVACY_FEATURES.md`
- `/Users/saveliy/Documents/Agent Payments/sthrip/docs/THREAT_MODEL.md`
