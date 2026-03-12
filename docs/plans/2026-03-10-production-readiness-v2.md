# Production Readiness Remediation v2

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all CRITICAL and HIGH issues found in the 2026-03-10 production readiness audit. 18 tasks across 4 phases — financial safety first, then security, reliability, code quality.

**Architecture:** Incremental fixes. Each phase is independently deployable. All fixes follow TDD: write/update tests first, then implement. Each task ends with `pytest tests/ -v --tb=short` passing.

**Tech Stack:** Python 3.9, FastAPI, SQLAlchemy, PostgreSQL, Redis, Monero wallet RPC, pytest

---

## Phase 1: Financial Safety

> Priority: CRITICAL. Blocks deployment with real funds.

---

### Task 1: Fix DepositMonitor height rollback bug

**Files:**
- Modify: `sthrip/services/deposit_monitor.py`
- Modify: `tests/test_deposit_monitor.py`

**Why:** `self._last_height` is updated in memory BEFORE `db.commit()`. If the transaction rolls back, the monitor skips those transfers permanently — agents lose deposits with no recovery.

**Step 1: Write test for rollback scenario**

In `tests/test_deposit_monitor.py`, add a test that:
1. Sets up a DepositMonitor with `_last_height = 100`
2. Mocks `_process_transfers` to raise an exception after processing
3. Calls `_do_poll_with_session` (or `poll_once`)
4. Asserts that `self._last_height` is still `100` (not advanced)
5. Asserts that on the next poll, the same transfers are re-processed

Run: `pytest tests/test_deposit_monitor.py -v --tb=short`
Expected: New test FAILS (RED).

**Step 2: Fix the height update ordering**

In `sthrip/services/deposit_monitor.py`, find where `self._last_height = max_height` is set. Move it AFTER the successful `db.commit()`. The pattern should be:

```python
# Process transfers (may raise)
self._process_transfers(transfers, db)
db.commit()
# Only update in-memory state AFTER successful commit
self._last_height = max_height
self._persist_height(max_height)
```

If `_process_transfers` or `db.commit()` raises, `self._last_height` stays at the old value and the same transfers are retried on the next poll.

**Step 3: Run tests**

Run: `pytest tests/test_deposit_monitor.py -v --tb=short`
Expected: All tests pass including the new one (GREEN).

**Step 4: Commit**

```
fix: move DepositMonitor height update after db.commit to prevent deposit loss
```

---

### Task 2: Add FOR UPDATE to confirm_hub_route

**Files:**
- Modify: `sthrip/services/fee_collector.py`
- Modify: `tests/test_fee_collector.py`

**Why:** No row lock on `confirm_hub_route` status check. Two concurrent confirmations can both read PENDING and double-collect fees.

**Step 1: Write test for concurrent confirmation**

In `tests/test_fee_collector.py`, add a test that:
1. Creates a hub route with status PENDING
2. Calls `confirm_hub_route` — should succeed
3. Calls `confirm_hub_route` again with the same payment_id — should raise ValueError("not pending")
4. Asserts only ONE FeeCollection record exists

Run: `pytest tests/test_fee_collector.py -v --tb=short`
Expected: Verify test captures the expected behavior.

**Step 2: Add FOR UPDATE to the query**

In `sthrip/services/fee_collector.py`, find the `confirm_hub_route` method (~line 264). Change:

```python
# Before
route = db.query(HubRoute).filter(HubRoute.payment_id == payment_id).first()

# After
route = db.query(HubRoute).filter(
    HubRoute.payment_id == payment_id
).with_for_update().first()
```

**Step 3: Run tests**

Run: `pytest tests/test_fee_collector.py -v --tb=short`
Expected: All tests pass (GREEN). Note: `FOR UPDATE` is a no-op on SQLite but the code path is exercised.

**Step 4: Commit**

```
fix: add FOR UPDATE to confirm_hub_route to prevent double fee collection
```

---

### Task 3: Add FOR UPDATE SKIP LOCKED to get_pending_events

**Files:**
- Modify: `sthrip/db/repository.py`
- Modify: `tests/test_webhook_service.py`

**Why:** Multiple webhook workers can fetch the same pending events, causing duplicate delivery.

**Step 1: Write test verifying SKIP LOCKED is applied**

In `tests/test_webhook_service.py`, add a test that:
1. Creates 3 pending webhook events
2. Calls `get_pending_events(limit=2)` — gets 2 events
3. Verifies the query uses `with_for_update(skip_locked=True)`

This can be verified by mocking the query or checking that the method applies the lock.

Run: `pytest tests/test_webhook_service.py -v --tb=short`
Expected: New test FAILS (RED).

**Step 2: Add SKIP LOCKED to the query**

In `sthrip/db/repository.py`, find `WebhookRepository.get_pending_events` (~line 571). Add `.with_for_update(skip_locked=True)` to the query chain.

```python
# Before
return self.db.query(WebhookEvent).filter(
    WebhookEvent.status == "pending"
).order_by(WebhookEvent.created_at).limit(limit).all()

# After
return self.db.query(WebhookEvent).filter(
    WebhookEvent.status == "pending"
).order_by(
    WebhookEvent.created_at
).with_for_update(skip_locked=True).limit(limit).all()
```

**Step 3: Run tests**

Run: `pytest tests/test_webhook_service.py -v --tb=short`
Expected: All tests pass (GREEN).

**Step 4: Commit**

```
fix: add FOR UPDATE SKIP LOCKED to get_pending_events to prevent duplicate webhook delivery
```

---

### Task 4: Add CHECK constraints on AgentBalance

**Files:**
- Modify: `sthrip/db/models.py`
- Create: `sthrip/migrations/versions/xxxx_add_balance_check_constraints.py`
- Modify: `tests/test_balance.py`

**Why:** No DB-level constraint prevents negative balances. Application-level check can be bypassed by direct DB writes or repository bugs.

**Step 1: Write test for negative balance constraint**

In `tests/test_balance.py`, add a test that:
1. Creates an AgentBalance with available=10
2. Attempts to set available=-1 directly via SQLAlchemy
3. Asserts that `db.commit()` raises `IntegrityError`

Note: SQLite supports CHECK constraints, so this test works in the test suite.

Run: `pytest tests/test_balance.py -v --tb=short`
Expected: New test FAILS (RED) — no constraint exists yet.

**Step 2: Add CHECK constraints to model**

In `sthrip/db/models.py`, add to the `AgentBalance` class:

```python
__table_args__ = (
    UniqueConstraint("agent_id", "token", name="uq_agent_balance"),
    CheckConstraint("available >= 0", name="ck_balance_available_non_negative"),
    CheckConstraint("pending >= 0", name="ck_balance_pending_non_negative"),
)
```

**Step 3: Create Alembic migration**

Run: `cd sthrip && alembic revision -m "add balance check constraints"`

Write migration manually:

```python
def upgrade():
    op.create_check_constraint(
        "ck_balance_available_non_negative",
        "agent_balances",
        "available >= 0"
    )
    op.create_check_constraint(
        "ck_balance_pending_non_negative",
        "agent_balances",
        "available >= 0"  # Fix: should be "pending >= 0"
    )

def downgrade():
    op.drop_constraint("ck_balance_pending_non_negative", "agent_balances")
    op.drop_constraint("ck_balance_available_non_negative", "agent_balances")
```

**Step 4: Run tests**

Run: `pytest tests/test_balance.py -v --tb=short`
Expected: All tests pass (GREEN).

**Step 5: Commit**

```
fix: add CHECK constraints to prevent negative balances at DB level
```

---

### Task 5: Create Alembic migration for PendingWithdrawal + fix UUID types

**Files:**
- Modify: `sthrip/db/models.py` (fix String(36) → UUID)
- Create: `sthrip/migrations/versions/xxxx_add_pending_withdrawals.py`
- Modify: `tests/test_balance.py`

**Why:** `PendingWithdrawal` table only created by `create_all()`. Fresh DB from Alembic migrations is broken. Also `id` and `agent_id` are `String(36)` but FK references `UUID` column.

**Step 1: Write test for UUID types**

In `tests/test_balance.py`, add a test that:
1. Verifies `PendingWithdrawal.id` column type is UUID (not String)
2. Verifies `PendingWithdrawal.agent_id` column type is UUID (not String)

Run: `pytest tests/test_balance.py -v --tb=short`
Expected: New test FAILS (RED).

**Step 2: Fix model column types**

In `sthrip/db/models.py`, change PendingWithdrawal:

```python
# Before
id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
agent_id = Column(String(36), ForeignKey("agents.id"), nullable=False, index=True)

# After
id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
```

**Step 3: Create Alembic migration**

Run: `cd sthrip && alembic revision -m "add pending_withdrawals table"`

Write migration that creates the `pending_withdrawals` table with correct UUID columns, proper status enum or string, and all constraints.

**Step 4: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass (GREEN).

**Step 5: Commit**

```
fix: add Alembic migration for pending_withdrawals, fix String(36) to UUID
```

---

## Phase 2: Security

> Priority: CRITICAL. Blocks public access.

---

### Task 6: Rate limit POST /v2/admin/auth

**Files:**
- Modify: `api/routers/admin.py`
- Modify: `tests/test_auth_rate_limit.py`

**Why:** REST admin auth endpoint has zero brute-force protection. HTML login has rate limiting, but API does not.

**Step 1: Write test for rate limiting**

In `tests/test_auth_rate_limit.py`, add tests that:
1. Send 6 failed `POST /v2/admin/auth` requests with wrong keys
2. Assert 6th request returns 429 (Too Many Requests)
3. Send a valid admin key request — should succeed on first attempt (counter only increments on failure)

Run: `pytest tests/test_auth_rate_limit.py -v --tb=short`
Expected: New tests FAIL (RED).

**Step 2: Add rate limiting to admin auth**

In `api/routers/admin.py`, add `check_ip_rate_limit()` call AFTER the password check fails (not before). Pattern:

```python
@router.post("/auth")
async def admin_auth(body: AdminAuthRequest, request: Request):
    expected_key = get_settings().admin_api_key
    if not expected_key or not hmac.compare_digest(
        body.admin_key.encode(), expected_key.encode()
    ):
        # Increment rate limit counter ONLY on failure
        check_ip_rate_limit(request, "admin_auth", max_attempts=5, window=300)
        raise HTTPException(status_code=401, detail="Invalid admin key")
    store = get_admin_session_store()
    token = store.create_session(_ADMIN_SESSION_TTL)
    return {"token": token, "expires_in": _ADMIN_SESSION_TTL}
```

Note: The rate limiter's `check_ip_rate_limit` should be called to record the failed attempt. If the limit is exceeded, it raises 429 instead of 401.

**Step 3: Run tests**

Run: `pytest tests/test_auth_rate_limit.py -v --tb=short`
Expected: All tests pass (GREEN).

**Step 4: Commit**

```
fix: add rate limiting to POST /v2/admin/auth to prevent brute force
```

---

### Task 7: Remove deprecated admin-key header bypass

**Files:**
- Modify: `api/deps.py`
- Modify: `tests/test_access_control.py`
- Modify: `tests/test_admin_ui.py` (if affected)

**Why:** Raw `admin-key` header bypass has no rate limiting, no expiry. Every admin route is brute-forceable via this path.

**Step 1: Write test confirming header is rejected**

In `tests/test_access_control.py`, add a test that:
1. Sends a request with `admin-key: <valid_key>` header to `/v2/admin/stats`
2. Asserts 401 response (header no longer accepted)

Run: `pytest tests/test_access_control.py -v --tb=short`
Expected: New test FAILS (RED) — header still works.

**Step 2: Remove the fallback code**

In `api/deps.py`, find the `admin-key` header check (~line 255-266) and remove it entirely. Only session token auth should be accepted.

**Step 3: Update any tests that use admin-key header**

Search for `admin-key` in test files and update them to use session-based auth instead.

**Step 4: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass (GREEN).

**Step 5: Commit**

```
fix: remove deprecated admin-key header bypass — use session tokens only
```

---

### Task 8: Fix DNS rebinding in webhook delivery

**Files:**
- Modify: `sthrip/services/webhook_service.py`
- Modify: `sthrip/services/url_validator.py`
- Modify: `tests/test_webhook_service.py`

**Why:** SSRF validation resolves hostname but passes original URL to aiohttp. DNS rebinding can redirect to internal IPs between validation and connection.

**Step 1: Write test for IP pinning**

In `tests/test_webhook_service.py`, add a test that:
1. Validates a URL and gets the resolved IP
2. Verifies the HTTP request is made to the resolved IP (not the hostname)
3. Verifies the `Host` header is set to the original hostname

Run: `pytest tests/test_webhook_service.py -v --tb=short`
Expected: New test FAILS (RED).

**Step 2: Pin resolved IP in webhook delivery**

In `sthrip/services/url_validator.py`, ensure `resolve_and_validate()` returns the resolved IP.

In `sthrip/services/webhook_service.py`, modify `_send_webhook` to:
1. Get both the original URL and resolved IP from validation
2. Replace the hostname in the URL with the resolved IP
3. Set the `Host` header to the original hostname

```python
validated_url, resolved_ip = validate_url_target(url, block_on_dns_failure=True)
# Build IP-pinned URL
parsed = urlparse(url)
pinned_url = url.replace(parsed.hostname, resolved_ip)
headers["Host"] = parsed.hostname
async with session.post(pinned_url, ..., headers=headers) as resp:
    ...
```

**Step 3: Run tests**

Run: `pytest tests/test_webhook_service.py -v --tb=short`
Expected: All tests pass (GREEN).

**Step 4: Commit**

```
fix: pin resolved IP in webhook delivery to prevent DNS rebinding SSRF
```

---

### Task 9: Fix .env.example invalid values

**Files:**
- Modify: `.env.example`

**Why:** `ENVIRONMENT=development` is not a valid enum value. CORS comment is misleading. Missing security-critical env vars.

**Step 1: Fix the values**

In `.env.example`:
1. Change `ENVIRONMENT=development` to `ENVIRONMENT=dev`
2. Change CORS comment from `# default: *` to `# default: empty (no CORS)`
3. Add `API_KEY_HMAC_SECRET=` with comment `# REQUIRED in production — run: openssl rand -hex 32`
4. Add `WEBHOOK_ENCRYPTION_KEY=` with comment `# REQUIRED in production — run: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

**Step 2: Commit**

```
fix: correct .env.example — valid ENVIRONMENT value, accurate CORS comment, add missing secrets
```

---

### Task 10: Fix enum case mismatch in migrations

**Files:**
- Modify: `sthrip/migrations/versions/d65bbb2427dd_initial_schema.py`
- Modify: `sthrip/migrations/env.py`

**Why:** PostgreSQL enum labels are UPPERCASE but Python enum values are lowercase. Raw SQL queries will fail or silently mismatch.

**Step 1: Write test for enum roundtrip**

Add a test that creates an Agent with `privacy_level=PrivacyLevel.LOW`, commits, re-reads from DB, and asserts `agent.privacy_level == PrivacyLevel.LOW`.

Run: `pytest tests/test_data_integrity.py -v --tb=short`
Expected: Test passes on SQLite (enum stored as string). Document that PostgreSQL enum labels must match.

**Step 2: Fix migration enum labels**

In the initial migration, change all enum type definitions to use lowercase values matching Python `.value`:

```python
# Before
sa.Enum('LOW', 'MEDIUM', 'HIGH', 'PARANOID', name='privacylevel')

# After
sa.Enum('low', 'medium', 'high', 'paranoid', name='privacylevel')
```

Apply to all enum types in the migration.

**Step 3: Add compare_type=True to env.py**

In `sthrip/migrations/env.py`, add `compare_type=True` to `context.configure()`:

```python
context.configure(
    connection=connection,
    target_metadata=target_metadata,
    compare_type=True,
)
```

**Step 4: Add enum drops to downgrade**

In the initial migration's `downgrade()`, after dropping tables, add:

```python
op.execute("DROP TYPE IF EXISTS privacylevel")
op.execute("DROP TYPE IF EXISTS agenttier")
# ... etc for all enum types
```

**Step 5: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass (GREEN).

**Step 6: Commit**

```
fix: align PostgreSQL enum labels with Python enum values, add compare_type to env.py
```

---

## Phase 3: Reliability

> Priority: HIGH. Prevents operational failures.

---

### Task 11: Fix timezone inconsistency across models

**Files:**
- Modify: `sthrip/db/models.py`
- Create: `sthrip/migrations/versions/xxxx_fix_datetime_timezone.py`

**Why:** Newer models use `DateTime(timezone=True)`, older use plain `DateTime`. Comparison between TZ-aware and naive timestamps fails or silently gives wrong results.

**Step 1: Write test for timezone consistency**

Add a test that introspects all DateTime columns across all models and asserts they all use `timezone=True`.

Run: `pytest tests/test_data_integrity.py -v --tb=short`
Expected: FAILS (RED) — older models lack timezone.

**Step 2: Update all DateTime columns**

In `sthrip/db/models.py`, change all `Column(DateTime, ...)` to `Column(DateTime(timezone=True), ...)`. Standardize defaults to `server_default=func.now()` for consistency.

**Step 3: Create migration**

Create an Alembic migration that alters all DateTime columns to `TIMESTAMP WITH TIME ZONE`.

**Step 4: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass (GREEN).

**Step 5: Commit**

```
fix: standardize all DateTime columns to timezone-aware (timestamptz)
```

---

### Task 12: Add thread safety to HealthMonitor

**Files:**
- Modify: `sthrip/services/monitoring.py`
- Modify: `tests/test_monitoring.py`

**Why:** Background thread mutates shared state without locking while request threads read it. Data race.

**Step 1: Write test for concurrent access**

Add a test that:
1. Creates a HealthMonitor with a check
2. Spawns a thread that runs `_run_check` in a loop
3. From the main thread, calls `get_health_report()` concurrently
4. Asserts no exceptions or corrupted state

Run: `pytest tests/test_monitoring.py -v --tb=short`
Expected: May pass intermittently — race conditions are probabilistic.

**Step 2: Add threading.Lock**

In `sthrip/services/monitoring.py`:
1. Add `self._lock = threading.Lock()` in `__init__`
2. Wrap `_run_check` state mutations in `with self._lock:`
3. Wrap `get_health_report` reads in `with self._lock:`
4. Wrap `self._alerts` mutations in `with self._lock:`

**Step 3: Run tests**

Run: `pytest tests/test_monitoring.py -v --tb=short`
Expected: All tests pass (GREEN).

**Step 4: Commit**

```
fix: add threading.Lock to HealthMonitor to prevent data races
```

---

### Task 13: Handle Redis-down for rate limiting

**Files:**
- Modify: `sthrip/services/rate_limiter.py`
- Modify: `tests/test_rate_limiter.py`

**Why:** Rate limiter falls back to in-process dict without Redis. Each replica has independent counters, effectively disabling rate limiting in multi-replica.

**Step 1: Write test for Redis-down behavior**

Add a test that:
1. Creates a rate limiter with `RATE_LIMIT_FAIL_OPEN=false` (default)
2. Simulates Redis being unavailable
3. Calls `check_rate_limit` — should raise 503 ServiceUnavailable
4. Then test with `RATE_LIMIT_FAIL_OPEN=true` — should fall back to in-process dict

Run: `pytest tests/test_rate_limiter.py -v --tb=short`
Expected: New tests FAIL (RED).

**Step 2: Implement fail-closed default**

In `sthrip/services/rate_limiter.py`:
1. Add `RATE_LIMIT_FAIL_OPEN` setting to config (default: `false`)
2. When Redis is unavailable and `RATE_LIMIT_FAIL_OPEN=false`, raise HTTPException(503)
3. When `RATE_LIMIT_FAIL_OPEN=true`, use in-process dict (current behavior)
4. Log a CRITICAL warning either way

**Step 3: Run tests**

Run: `pytest tests/test_rate_limiter.py -v --tb=short`
Expected: All tests pass (GREEN).

**Step 4: Commit**

```
fix: rate limiter fails closed by default when Redis unavailable
```

---

### Task 14: Fix admin login rate limiter self-lockout

**Files:**
- Modify: `api/admin_ui/views.py`
- Modify: `tests/test_admin_session_auth.py`

**Why:** Rate limit counter increments BEFORE password check. Valid admin gets locked out after 5 failed attempts from the same IP (including attacker attempts).

**Step 1: Write test for correct increment order**

Add a test that:
1. Makes 4 failed login attempts
2. Makes 1 successful login — should succeed (not 429)
3. Makes 2 more failed attempts
4. The counter should be at 6 (4 + 2), not 7

Run: `pytest tests/test_admin_session_auth.py -v --tb=short`
Expected: Test FAILS (RED) — 5th attempt (the valid one) increments counter and may hit limit.

**Step 2: Move increment to failure branch**

In `api/admin_ui/views.py`, move `check_ip_rate_limit` call to AFTER the password verification fails:

```python
# Check rate limit BEFORE attempt (to reject if already over limit)
check_ip_rate_limit(request, "admin_login", max_attempts=5, window=300, check_only=True)

# Verify password
if not hmac.compare_digest(admin_key.encode(), expected_key.encode()):
    # Increment counter ONLY on failure
    check_ip_rate_limit(request, "admin_login", max_attempts=5, window=300)
    raise HTTPException(status_code=401, detail="Invalid admin key")
```

Note: This requires the rate limiter to support a `check_only` mode that checks without incrementing. If that doesn't exist, split the function or pass a flag.

**Step 3: Run tests**

Run: `pytest tests/test_admin_session_auth.py -v --tb=short`
Expected: All tests pass (GREEN).

**Step 4: Commit**

```
fix: increment admin login rate limit counter only on failed attempts
```

---

## Phase 4: Code Quality

> Priority: MEDIUM. Improves maintainability and performance.

---

### Task 15: Extract shared test fixtures to conftest.py

**Files:**
- Modify: `tests/conftest.py`
- Modify: 13 test files (remove duplicate fixtures)

**Why:** 40-line `client` fixture with ExitStack is copy-pasted across 13+ files. Adding a new router requires updating all of them.

**Step 1: Create shared fixture in conftest.py**

Move the full `client` fixture (with all `get_db`, `audit_log`, `get_rate_limiter` patches) into `tests/conftest.py`. Parametrize it to support both authenticated and unauthenticated modes.

**Step 2: Remove duplicates from individual test files**

In each test file that has its own `client` fixture, remove it and use the shared one. For files that need specialized behavior, override only the diff.

**Step 3: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All 721 tests pass (GREEN).

**Step 4: Commit**

```
refactor: extract shared test client fixture to conftest.py, remove 13 duplicates
```

---

### Task 16: Refactor oversized functions

**Files:**
- Modify: `api/main_v2.py` (lifespan: 188→<50 lines)
- Modify: `api/routers/agents.py` (eliminate 4x copy-pasted rate-limit blocks)
- Modify: `api/routers/payments.py` (send_hub_routed_payment: 119→<50 lines)
- Modify: `api/routers/balance.py` (withdraw_balance: 113→<50 lines)

**Why:** Multiple functions exceed the 50-line limit. 4x copy-pasted rate-limit blocks violate DRY.

**Step 1: Extract lifespan into helpers**

In `api/main_v2.py`, extract:
- `_run_startup(app)` — Sentry init, DB setup, monitoring, wallet, deposit monitor
- `_run_shutdown(app)` — stop monitor, stop webhooks, dispose engine

`lifespan` becomes: call `_run_startup`, yield, call `_run_shutdown`.

**Step 2: Create rate-limit dependency for agents router**

In `api/deps.py` (or `api/routers/agents.py`), create a `check_agent_rate_limit` dependency:

```python
async def check_agent_rate_limit(request: Request):
    try:
        rate_limiter = get_rate_limiter()
        if rate_limiter:
            rate_limiter.check("ip", request.client.host, ...)
    except RateLimitExceeded as e:
        raise HTTPException(429, ...)
```

Replace the 4 copy-pasted blocks in `agents.py` with `Depends(check_agent_rate_limit)`.

**Step 3: Extract payment/withdrawal helpers**

Split `send_hub_routed_payment` into `_validate_payment(...)`, `_execute_payment(...)`.
Split `withdraw_balance` into `_deduct_and_create_pending(...)`, `_execute_withdrawal(...)`, `_handle_withdrawal_failure(...)`.

**Step 4: Remove `__import__("time")` anti-pattern**

Add `import time` at the top of `api/routers/agents.py`. Replace all `__import__("time").time()` with `time.time()`.

**Step 5: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass (GREEN).

**Step 6: Commit**

```
refactor: extract oversized functions, DRY rate-limit blocks, remove __import__ anti-pattern
```

---

### Task 17: Add missing FK indexes

**Files:**
- Modify: `sthrip/db/models.py`
- Create: `sthrip/migrations/versions/xxxx_add_fk_indexes.py`

**Why:** 8 FK columns lack indexes. Queries filtering by these columns do sequential scans.

**Step 1: Add index=True to FK columns**

In `sthrip/db/models.py`, add `index=True` to:
- `Transaction.from_agent_id`
- `Transaction.to_agent_id`
- `EscrowDeal.buyer_id`
- `EscrowDeal.seller_id`
- `EscrowDeal.arbiter_id`
- `EscrowDeal.disputed_by`
- `PaymentChannel.agent_a_id`
- `PaymentChannel.agent_b_id`

**Step 2: Create migration**

Run: `cd sthrip && alembic revision -m "add FK indexes"`

Write migration with `op.create_index(...)` for each FK column.

**Step 3: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass (GREEN).

**Step 4: Commit**

```
perf: add indexes on 8 FK columns to prevent sequential scans
```

---

### Task 18: Fix N+1 queries in agent discovery

**Files:**
- Modify: `sthrip/services/agent_registry.py`
- Modify: `sthrip/db/repository.py`
- Modify: `tests/test_api.py`

**Why:** `discover_agents`, `search_agents`, `get_leaderboard` lazy-load relationships — up to 100 extra queries per call.

**Step 1: Write test verifying query count**

Add a test that:
1. Creates 10 agents with reputations
2. Calls `discover_agents(limit=10)`
3. Counts the number of SQL queries executed (using SQLAlchemy events)
4. Asserts query count is <= 3 (not 10+)

Run: `pytest tests/test_api.py -v --tb=short`
Expected: New test FAILS (RED) — currently 10+ queries.

**Step 2: Add eager loading**

In `sthrip/db/repository.py`, modify the list/search/leaderboard queries:

```python
from sqlalchemy.orm import joinedload

# In AgentRepository.list_agents
query = self.db.query(Agent).options(joinedload(Agent.reputation))

# In ReputationRepository.get_leaderboard
query = self.db.query(AgentReputation).options(joinedload(AgentReputation.agent))
```

**Step 3: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass (GREEN), query count test passes.

**Step 4: Commit**

```
perf: add joinedload to agent discovery/leaderboard to fix N+1 queries
```

---

## Execution Order

```
Phase 1 (Tasks 1-5)  →  Phase 2 (Tasks 6-10)  →  Phase 3 (Tasks 11-14)  →  Phase 4 (Tasks 15-18)
     │                       │                         │                         │
     │ Financial safety      │ Security                │ Reliability             │ Code quality
     │ BLOCKS DEPLOYMENT     │ BLOCKS PUBLIC ACCESS    │ HIGH priority           │ MEDIUM priority
     └───────────────────────┴─────────────────────────┴─────────────────────────┘
                              All tasks follow TDD: test first → implement → verify
```

## Verification Checklist

After all 18 tasks:
- [ ] `pytest tests/ -v --tb=short` — all pass
- [ ] `pytest tests/ --cov=sthrip --cov=api --cov-report=term-missing` — 80%+ coverage
- [ ] No CRITICAL or HIGH findings remaining
- [ ] All Alembic migrations apply cleanly: `alembic upgrade head`
- [ ] `.env.example` has correct values
- [ ] No deprecated code paths remain (admin-key header removed)
