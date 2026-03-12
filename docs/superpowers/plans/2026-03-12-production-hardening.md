# Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all CRITICAL, HIGH, and select MEDIUM issues identified in the production readiness review to bring Sthrip to a deployable state for real XMR payments.

**Architecture:** Fixes are organized into 3 phases by severity. Each phase is independently deployable. Phase 1 fixes data-loss and security bugs. Phase 2 fixes scalability and consistency issues. Phase 3 fixes code quality and operational gaps.

**Tech Stack:** Python 3.9, FastAPI, SQLAlchemy, PostgreSQL, Redis, tenacity, Alembic, pytest

---

## Chunk 1: Phase 1 — Critical Fixes (data loss & security)

### Task 1: Deposit monitor — replace `db.rollback()` with savepoint

**Files:**
- Modify: `sthrip/services/deposit_monitor.py:273-299`
- Test: `tests/test_deposit_monitor.py`

The current code calls `db.rollback()` on `IntegrityError` in `_handle_new_transfer`, which rolls back ALL balance credits processed so far in the batch — not just the duplicate. On the next poll cycle, those transfers get reprocessed and balances are double-credited.

- [ ] **Step 1: Write failing test for savepoint behavior**

```python
# tests/test_deposit_monitor_savepoint.py
"""Test that duplicate tx_hash in a batch does NOT roll back prior transfers."""
import pytest
from unittest.mock import MagicMock, patch
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.exc import IntegrityError


def test_duplicate_tx_does_not_rollback_prior_transfers(db_session):
    """When transfer #2 in a batch is a duplicate, transfer #1 stays committed."""
    from sthrip.services.deposit_monitor import DepositMonitor
    from sthrip.db.transaction_repo import TransactionRepository
    from sthrip.db.balance_repo import BalanceRepository

    agent_id = uuid4()
    tx_repo = TransactionRepository(db_session)
    bal_repo = BalanceRepository(db_session)

    # Ensure agent balance exists
    bal_repo.get_or_create(agent_id, "XMR")

    monitor = DepositMonitor.__new__(DepositMonitor)
    monitor.min_confirmations = 10
    monitor._network = "stagenet"
    monitor._fire_webhook = MagicMock()

    # Transfer 1: normal (should persist)
    monitor._handle_new_transfer(
        db_session, tx_repo, bal_repo, agent_id,
        txid="aaa111", amount=Decimal("1.0"), confirmations=10, height=100,
    )

    # Transfer 2: make tx_repo.create raise IntegrityError (duplicate)
    original_create = tx_repo.create
    call_count = [0]
    def create_that_fails_second(**kwargs):
        call_count[0] += 1
        if kwargs.get("tx_hash") == "bbb222":
            raise IntegrityError("duplicate", {}, None)
        return original_create(**kwargs)

    tx_repo.create = create_that_fails_second

    # Should NOT raise, should skip the duplicate
    monitor._handle_new_transfer(
        db_session, tx_repo, bal_repo, agent_id,
        txid="bbb222", amount=Decimal("2.0"), confirmations=10, height=101,
    )

    # Transfer 1 balance must still be there
    balance = bal_repo.get_available(agent_id, "XMR")
    assert balance >= Decimal("1.0"), f"Transfer 1 was rolled back! balance={balance}"
```

- [ ] **Step 2: Run test — expect FAIL (rollback destroys transfer 1)**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_deposit_monitor_savepoint.py -v`
Expected: FAIL — balance is 0 because `db.rollback()` killed transfer 1

- [ ] **Step 3: Fix — use `db.begin_nested()` savepoint**

In `sthrip/services/deposit_monitor.py`, replace lines 283-299:

```python
        # Wrap in savepoint so IntegrityError only rolls back THIS insert,
        # not the entire batch transaction.
        is_sqlite = "sqlite" in str(db.bind.url) if db.bind else False
        savepoint = None if is_sqlite else db.begin_nested()
        try:
            tx_repo.create(
                tx_hash=txid,
                network=self._network,
                from_agent_id=None,
                to_agent_id=agent_id,
                amount=amount,
                token="XMR",
                payment_type="hub_routing",
                status=status,
            )
        except IntegrityError:
            # Duplicate tx_hash — roll back only the savepoint, not the outer tx.
            if savepoint is not None:
                savepoint.rollback()
            else:
                db.rollback()
            logger.warning("Duplicate tx_hash %s — skipping (already processed)", txid)
            return
```

- [ ] **Step 4: Run test — expect PASS**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_deposit_monitor_savepoint.py -v`
Expected: PASS

- [ ] **Step 5: Run full deposit monitor test suite**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_deposit_monitor.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
git add sthrip/services/deposit_monitor.py tests/test_deposit_monitor_savepoint.py
git commit -m "fix: use savepoint in deposit monitor to prevent batch rollback on duplicate tx"
```

---

### Task 2: Async wallet RPC — add retry with tenacity AsyncRetrying

**Files:**
- Modify: `sthrip/wallet.py:11,232-255`
- Test: `tests/test_wallet_async_retry.py`

The sync `_call` method has `@retry` for `ConnectionError`/`Timeout`, but `_acall` has none. A single network hiccup on `async_transfer` loses the withdrawal without retry.

- [ ] **Step 1: Write failing test for async retry**

```python
# tests/test_wallet_async_retry.py
"""Test that _acall retries on transient errors."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sthrip.wallet import MoneroWalletRPC, WalletRPCError


@pytest.mark.asyncio
async def test_acall_retries_on_connection_error():
    """_acall should retry up to 3 times on httpx connection errors."""
    import httpx

    wallet = MoneroWalletRPC(host="localhost", port=18082)

    mock_client = AsyncMock()
    # Fail twice, succeed on third
    mock_response = MagicMock()
    mock_response.json.return_value = {"result": {"balance": 100}}
    mock_response.raise_for_status = MagicMock()

    mock_client.post = AsyncMock(side_effect=[
        httpx.ConnectError("connection refused"),
        httpx.ConnectError("connection refused"),
        mock_response,
    ])

    wallet._async_client = mock_client

    result = await wallet._acall("get_balance", {"account_index": 0})
    assert result == {"balance": 100}
    assert mock_client.post.call_count == 3


@pytest.mark.asyncio
async def test_acall_raises_after_max_retries():
    """_acall should raise after exhausting retries."""
    import httpx

    wallet = MoneroWalletRPC(host="localhost", port=18082)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    wallet._async_client = mock_client

    with pytest.raises(httpx.ConnectError):
        await wallet._acall("get_balance")
```

- [ ] **Step 2: Run test — expect FAIL (no retry, fails on first error)**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_wallet_async_retry.py -v`
Expected: FAIL

- [ ] **Step 3: Add async retry to `_acall`**

In `sthrip/wallet.py`, update import at line 11:

```python
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type, AsyncRetrying,
)
```

Replace `_acall` method (lines 232-255):

```python
    async def _acall(self, method: str, params: Optional[Dict] = None) -> Any:
        """Async JSON-RPC call with retry on transient errors."""
        try:
            import httpx
        except ImportError:
            import asyncio
            return await asyncio.to_thread(self._call, method, params)

        payload = {
            "jsonrpc": "2.0",
            "id": "0",
            "method": method,
            "params": params or {},
        }

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
            reraise=True,
        ):
            with attempt:
                client = await self._get_async_client()
                response = await client.post(self.url, json=payload)
                response.raise_for_status()

                result = response.json()
                if "error" in result:
                    raise WalletRPCError(result["error"]["message"])

                return result.get("result")
```

- [ ] **Step 4: Run test — expect PASS**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_wallet_async_retry.py -v`
Expected: PASS

- [ ] **Step 5: Run full wallet test suite**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/ -k wallet -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
git add sthrip/wallet.py tests/test_wallet_async_retry.py
git commit -m "fix: add async retry to wallet RPC _acall via tenacity AsyncRetrying"
```

---

### Task 3: Admin CSRF — fix logout form + replace CDN with local Tailwind

**Files:**
- Modify: `api/admin_ui/templates/base.html:21-23`
- Modify: `api/admin_ui/templates/login.html:7`
- Modify: `api/admin_ui/views.py:344-353`
- Test: `tests/test_admin_ui.py`

Three related issues: (1) logout form missing CSRF token, (2) login.html loads Tailwind from CDN (blocked by own CSP), (3) `_auth_redirect` doesn't clear stale cookie.

- [ ] **Step 1: Write failing test — logout form must contain CSRF token**

```python
# tests/test_admin_csrf_logout.py
"""Test that all authenticated admin pages include CSRF token in logout form."""
import pytest
from unittest.mock import patch, MagicMock


def test_overview_page_has_csrf_in_logout_form(admin_client, admin_session_cookie):
    """The overview page's logout form must include a csrf_token hidden input."""
    response = admin_client.get(
        "/admin/",
        cookies={"admin_session": admin_session_cookie},
    )
    assert response.status_code == 200
    html = response.text
    # The logout form must contain a CSRF hidden input
    assert 'name="csrf_token"' in html, "Logout form is missing CSRF token"
    assert 'action="/admin/logout"' in html


def test_auth_redirect_clears_cookie(admin_client):
    """When session is expired, redirect must delete the admin_session cookie."""
    response = admin_client.get(
        "/admin/",
        cookies={"admin_session": "expired-bogus-token"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # Cookie must be deleted via Set-Cookie with max-age=0 or expires in past
    set_cookie = response.headers.get("set-cookie", "")
    assert "admin_session" in set_cookie
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_admin_csrf_logout.py -v`
Expected: FAIL — no csrf_token in logout form, no cookie deletion

- [ ] **Step 3a: Fix `base.html` — add CSRF token to logout form**

In `api/admin_ui/templates/base.html`, replace lines 21-23:

```html
                <form method="post" action="/admin/logout" class="inline">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                    <button type="submit" class="hover:text-red-300 bg-transparent border-0 text-white cursor-pointer text-sm">Logout</button>
                </form>
```

- [ ] **Step 3b: Fix `views.py` — inject `csrf_token` into all authenticated template contexts**

In `api/admin_ui/views.py`, in `setup_admin_ui()`, after line 348 (`app.include_router(router)`), add a Jinja2 global:

```python
    templates.env.globals["csrf_token"] = _session_store.create_csrf_token
```

**Note:** This makes `{{ csrf_token() }}` available in ALL templates via the callable. Update `base.html` to use `{{ csrf_token() }}` instead of `{{ csrf_token }}`.

Alternatively, pass `csrf_token` explicitly from each view handler — but the global approach is less error-prone.

- [ ] **Step 3c: Fix `login.html` — replace CDN with local Tailwind**

In `api/admin_ui/templates/login.html`, replace line 7:

```html
    <link rel="stylesheet" href="/admin/static/tailwind.css">
```

- [ ] **Step 3d: Fix `_auth_redirect` — delete stale cookie**

In `api/admin_ui/views.py`, replace lines 351-353:

```python
    @app.exception_handler(_AuthRequired)
    async def _auth_redirect(request: Request, exc: _AuthRequired):
        response = RedirectResponse(url="/admin/login", status_code=303)
        response.delete_cookie("admin_session")
        return response
```

- [ ] **Step 4: Run test — expect PASS**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_admin_csrf_logout.py tests/test_admin_ui.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
git add api/admin_ui/templates/base.html api/admin_ui/templates/login.html api/admin_ui/views.py tests/test_admin_csrf_logout.py
git commit -m "fix: add CSRF to logout form, replace CDN Tailwind, clear stale session cookie"
```

---

### Task 4: Rate limiter — fix off-by-one (`>` to `>=`)

**Files:**
- Modify: `sthrip/services/rate_limiter.py:336,346,359,367`
- Test: `tests/test_rate_limiter.py`

`_peek_ip_limit` uses `>` instead of `>=`, allowing `limit + 1` attempts before blocking. For admin auth with `per_ip_limit=5`, attacker gets 6 attempts.

- [ ] **Step 1: Write failing test**

```python
# tests/test_rate_limiter_offbyone.py
"""Test that rate limiter blocks at exactly the limit, not limit+1."""
import time
from sthrip.services.rate_limiter import RateLimiter, RateLimitExceeded
import pytest


def test_peek_blocks_at_exact_limit():
    """After N requests (where N == limit), peek must raise."""
    limiter = RateLimiter(redis_url=None)  # local fallback
    ip_key = "ratelimit:ip:test_peek:1.2.3.4"
    global_key = "ratelimit:global:test_peek"
    limit = 5
    now = time.time()

    # Simulate N requests already counted
    with limiter._cache_lock:
        limiter._local_cache[ip_key] = {"count": limit, "reset_at": now + 60}

    # Peek should raise — we're AT the limit
    with pytest.raises(RateLimitExceeded):
        limiter._peek_ip_limit(ip_key, global_key, limit, 1000, now)
```

- [ ] **Step 2: Run test — expect FAIL (peek allows count == limit)**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_rate_limiter_offbyone.py -v`
Expected: FAIL

- [ ] **Step 3: Fix — change `>` to `>=` in 4 places**

In `sthrip/services/rate_limiter.py`:
- Line 336: `if ip_count > per_ip_limit:` → `if ip_count >= per_ip_limit:`
- Line 346: `if g_count > global_limit:` → `if g_count >= global_limit:`
- Line 359: `if ip_count > per_ip_limit:` → `if ip_count >= per_ip_limit:`
- Line 367: `if g_count > global_limit:` → `if g_count >= global_limit:`

Also update the docstring at line 322-326 to reflect the correct behavior.

- [ ] **Step 4: Run test — expect PASS**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_rate_limiter_offbyone.py tests/test_rate_limiter.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
git add sthrip/services/rate_limiter.py tests/test_rate_limiter_offbyone.py
git commit -m "fix: rate limiter off-by-one — use >= instead of > in _peek_ip_limit"
```

---

### Task 5: Missing database indexes — Alembic migration

**Files:**
- Create: `migrations/versions/c4d5e6f7g8h9_add_missing_indexes.py`
- Modify: `sthrip/db/models.py` (add `__table_args__` where missing)

Missing composite/status indexes on `transactions`, `hub_routes`, `pending_withdrawals`. Full table scans at scale.

- [ ] **Step 1: Add indexes to SQLAlchemy models**

In `sthrip/db/models.py`:

**Transaction** (after line 158, before class EscrowDeal):
```python
    __table_args__ = (
        Index("ix_transactions_status", "status"),
        Index("ix_transactions_from_agent_created", "from_agent_id", "created_at"),
        Index("ix_transactions_to_agent_created", "to_agent_id", "created_at"),
    )
```

**HubRoute** (after line 289):
```python
    __table_args__ = (
        Index("ix_hub_routes_status", "status"),
    )
```

**PendingWithdrawal** (after line 387):
```python
    __table_args__ = (
        Index("ix_pending_withdrawals_status_created", "status", "created_at"),
    )
```

- [ ] **Step 2: Generate Alembic migration**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && alembic revision --autogenerate -m "add missing indexes on transactions, hub_routes, pending_withdrawals"`

- [ ] **Step 3: Review generated migration — ensure only CREATE INDEX statements**

Run: `cat migrations/versions/*add_missing_indexes*.py`
Expected: Only `op.create_index(...)` in upgrade, `op.drop_index(...)` in downgrade. No table alterations.

- [ ] **Step 4: Test migration runs on SQLite (smoke test)**

```python
# tests/test_migration_indexes.py
"""Verify the new indexes exist after model load."""
from sthrip.db.models import Transaction, HubRoute, PendingWithdrawal


def test_transaction_has_status_index():
    indexes = {idx.name for idx in Transaction.__table__.indexes}
    assert "ix_transactions_status" in indexes


def test_hub_route_has_status_index():
    indexes = {idx.name for idx in HubRoute.__table__.indexes}
    assert "ix_hub_routes_status" in indexes


def test_pending_withdrawal_has_status_created_index():
    indexes = {idx.name for idx in PendingWithdrawal.__table__.indexes}
    assert "ix_pending_withdrawals_status_created" in indexes
```

- [ ] **Step 5: Run test**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_migration_indexes.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
git add sthrip/db/models.py migrations/versions/*add_missing_indexes* tests/test_migration_indexes.py
git commit -m "feat: add missing indexes on transactions, hub_routes, pending_withdrawals"
```

---

## Chunk 2: Phase 2 — High Priority Fixes (consistency & encapsulation)

### Task 6: Balance repo — update `total_withdrawn` on deductions for withdrawals

**Files:**
- Modify: `sthrip/db/balance_repo.py:108-115`
- Test: `tests/test_balance_repo_withdrawn.py`

`AgentBalance.total_withdrawn` is never incremented — always 0. The `deduct` method only decrements `available`.

- [ ] **Step 1: Write failing test**

```python
# tests/test_balance_repo_withdrawn.py
"""Test that withdrawal operations update total_withdrawn."""
from decimal import Decimal
from uuid import uuid4


def test_withdraw_updates_total_withdrawn(db_session):
    from sthrip.db.balance_repo import BalanceRepository

    repo = BalanceRepository(db_session)
    agent_id = uuid4()

    # Seed balance
    repo.deposit(agent_id, Decimal("10.0"))
    db_session.flush()

    # Withdraw
    repo.withdraw(agent_id, Decimal("3.0"))
    db_session.flush()

    balance = repo.get_or_create(agent_id, "XMR")
    assert balance.available == Decimal("7.0")
    assert balance.total_withdrawn == Decimal("3.0")
```

- [ ] **Step 2: Run test — expect FAIL (no `withdraw` method)**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_balance_repo_withdrawn.py -v`
Expected: FAIL — AttributeError: 'BalanceRepository' has no `withdraw` method

- [ ] **Step 3: Add `withdraw` method to `BalanceRepository`**

In `sthrip/db/balance_repo.py`, after `deduct` method (after line 115):

```python
    def withdraw(self, agent_id: UUID, amount: Decimal, token: str = "XMR") -> AgentBalance:
        """Deduct from available and increment total_withdrawn (for actual withdrawals)."""
        balance = self._get_for_update(agent_id, token)
        available = balance.available or Decimal("0")
        if available < amount:
            raise ValueError("Insufficient balance")
        balance.available = available - amount
        balance.total_withdrawn = (balance.total_withdrawn or Decimal("0")) + amount
        balance.updated_at = datetime.now(timezone.utc)
        return balance
```

- [ ] **Step 4: Run test — expect PASS**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_balance_repo_withdrawn.py -v`
Expected: PASS

- [ ] **Step 5: Update withdrawal callers to use `withdraw()` instead of `deduct()`**

Search for withdrawal flows that call `deduct`:
- `api/routers/balance.py` — the withdrawal endpoint
- `sthrip/services/withdrawal_recovery.py` — recovery path

Replace `bal_repo.deduct(agent_id, amount)` with `bal_repo.withdraw(agent_id, amount)` only in actual withdrawal paths. Hub routing payments should keep using `deduct`.

- [ ] **Step 6: Run full balance test suite**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_balance.py tests/test_balance_repo_withdrawn.py -v`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
git add sthrip/db/balance_repo.py api/routers/balance.py tests/test_balance_repo_withdrawn.py
git commit -m "fix: add withdraw() method to BalanceRepository, track total_withdrawn"
```

---

### Task 7: Rate limiter encapsulation — move failed auth logic into RateLimiter

**Files:**
- Modify: `sthrip/services/rate_limiter.py`
- Modify: `api/deps.py:161-211`
- Test: `tests/test_auth_rate_limit.py`

`api/deps.py` directly accesses `limiter._cache_lock` and `limiter._local_cache`. Move to public methods.

- [ ] **Step 1: Write test for new public API**

```python
# tests/test_rate_limiter_failed_auth.py
"""Test RateLimiter.check_failed_auth and record_failed_auth public API."""
import pytest
from sthrip.services.rate_limiter import RateLimiter, RateLimitExceeded


def test_record_and_check_failed_auth():
    limiter = RateLimiter(redis_url=None)

    # Record 4 failures — should not raise
    for _ in range(4):
        limiter.record_failed_auth("1.2.3.4")
        limiter.check_failed_auth("1.2.3.4")  # still under limit

    # 5th failure
    limiter.record_failed_auth("1.2.3.4")
    with pytest.raises(RateLimitExceeded):
        limiter.check_failed_auth("1.2.3.4")
```

- [ ] **Step 2: Run test — expect FAIL (methods don't exist)**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_rate_limiter_failed_auth.py -v`
Expected: FAIL

- [ ] **Step 3: Add `check_failed_auth` and `record_failed_auth` to `RateLimiter`**

In `sthrip/services/rate_limiter.py`, add two public methods that encapsulate the logic currently in `api/deps.py:161-211`. Use `self._cache_lock` and `self._local_cache` internally.

```python
    def check_failed_auth(self, ip: str, limit: int = 5, window: int = 900) -> None:
        """Raise RateLimitExceeded if IP has exceeded failed auth limit."""
        key = f"ratelimit:ip:failed_auth:{ip}"
        now = _time.time()

        if self.use_redis:
            data = self.redis.hmget(key, "count", "reset_at")
            count = int(data[0]) if data[0] else 0
            reset_at = float(data[1]) if data[1] else now + window
            if reset_at < now:
                count = 0
            if count >= limit:
                raise RateLimitExceeded(limit=limit, reset_at=reset_at)
        else:
            with self._cache_lock:
                entry = self._local_cache.get(key)
            if entry and entry.get("reset_at", 0) >= now:
                if entry.get("count", 0) >= limit:
                    raise RateLimitExceeded(limit=limit, reset_at=entry["reset_at"])

    def record_failed_auth(self, ip: str, window: int = 900) -> None:
        """Increment failed auth counter for IP (atomic)."""
        key = f"ratelimit:ip:failed_auth:{ip}"
        now = _time.time()

        if self.use_redis:
            pipe = self.redis.pipeline()
            pipe.hincrby(key, "count", 1)
            pipe.hsetnx(key, "reset_at", str(now + window))
            pipe.expire(key, window + 60)
            pipe.execute()
        else:
            with self._cache_lock:
                entry = self._local_cache.get(key)
                if entry and entry.get("reset_at", 0) >= now:
                    self._local_cache[key] = {
                        "count": entry.get("count", 0) + 1,
                        "reset_at": entry["reset_at"],
                    }
                else:
                    self._local_cache[key] = {
                        "count": 1,
                        "reset_at": now + window,
                    }
```

- [ ] **Step 4: Update `api/deps.py` — replace private access with public API**

Replace `_check_failed_auth_limit(limiter, ip)` calls with `limiter.check_failed_auth(ip)`.
Replace `_record_failed_auth(limiter, ip)` calls with `limiter.record_failed_auth(ip)`.
Delete the `_check_failed_auth_limit` and `_record_failed_auth` functions from `deps.py`.

- [ ] **Step 5: Run tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_rate_limiter_failed_auth.py tests/test_auth_rate_limit.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
git add sthrip/services/rate_limiter.py api/deps.py tests/test_rate_limiter_failed_auth.py
git commit -m "refactor: move failed auth logic into RateLimiter, remove private member access from deps.py"
```

---

### Task 8: Stealth address — immutable cache update

**Files:**
- Modify: `sthrip/stealth.py:78-86`
- Test: `tests/test_stealth_immutability.py`

`mark_used` mutates `self._cache[index].used = True` directly.

- [ ] **Step 1: Write failing test**

```python
# tests/test_stealth_immutability.py
"""Test that mark_used creates a new StealthAddress, not mutating the old one."""
import dataclasses
from unittest.mock import MagicMock
from sthrip.stealth import StealthAddressManager, StealthAddress


def test_mark_used_does_not_mutate_original():
    wallet = MagicMock()
    wallet.get_address_index.return_value = {"index": {"minor": 1}}

    mgr = StealthAddressManager(wallet)
    original = StealthAddress(address="addr_1", index=1, label="test", used=False)
    mgr._cache[1] = original

    # Keep a reference to the original object
    original_ref = mgr._cache[1]

    mgr.mark_used("addr_1")

    # The cache should have a NEW object with used=True
    assert mgr._cache[1].used is True
    # The original reference should NOT be mutated
    assert original_ref.used is False, "Original object was mutated instead of replaced"
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_stealth_immutability.py -v`
Expected: FAIL — original_ref.used is True (same object mutated)

- [ ] **Step 3: Fix — use `dataclasses.replace()`**

In `sthrip/stealth.py`, add import at top:

```python
import dataclasses
```

Replace lines 83-84:

```python
            if index in self._cache:
                self._cache[index] = dataclasses.replace(self._cache[index], used=True)
```

Also add `logger.debug` for the swallowed `WalletRPCError`:

```python
        except WalletRPCError:
            logger.debug("mark_used: address %s not found in wallet", address)
```

- [ ] **Step 4: Run test — expect PASS**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_stealth_immutability.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
git add sthrip/stealth.py tests/test_stealth_immutability.py
git commit -m "fix: use dataclasses.replace in StealthAddressManager.mark_used for immutability"
```

---

### Task 9: Fix `network.py` type annotation bug

**Files:**
- Modify: `sthrip/network.py:132,134`

`any` (builtin) used as type annotation instead of `typing.Any`.

- [ ] **Step 1: Fix — replace `any` with `Any`**

In `sthrip/network.py`:

Ensure `Any` is imported from typing (check existing imports).

Line 132: `self.connections: Dict[str, any] = {}` → `self.connections: Dict[str, Any] = {}`
Line 134: `def get_connection(self) -> any:` → `def get_connection(self) -> Any:`

- [ ] **Step 2: Run mypy check on file**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m mypy sthrip/network.py --ignore-missing-imports 2>&1 | head -20`
Expected: No `valid-type` errors on lines 132/134

- [ ] **Step 3: Commit**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
git add sthrip/network.py
git commit -m "fix: use typing.Any instead of builtin any in network.py type annotations"
```

---

### Task 10: Guard `drop_tables()` and dead `Database` class

**Files:**
- Modify: `sthrip/db/database.py:77-81,118-143`

`drop_tables()` is callable in production. `Database` class is dead code.

- [ ] **Step 1: Guard `drop_tables()` with environment check**

In `sthrip/db/database.py`, replace lines 77-81:

```python
def drop_tables():
    """Drop all tables. Only allowed in dev/test environments."""
    from .models import Base
    from sthrip.config import get_settings
    settings = get_settings()
    if settings.environment not in ("dev", "test"):
        raise RuntimeError(
            f"drop_tables() is disabled in '{settings.environment}' environment"
        )
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
```

- [ ] **Step 2: Remove dead `Database` class (lines 118-143)**

Delete the entire `Database` class. It duplicates module-level functions and is not imported anywhere.

- [ ] **Step 3: Run tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_database.py -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
git add sthrip/db/database.py
git commit -m "fix: guard drop_tables() for non-dev environments, remove dead Database class"
```

---

## Chunk 3: Phase 3 — Medium Priority (code quality & operational)

### Task 11: Fix mypy errors in production-path code

**Files:**
- Modify: `sthrip/privacy.py:218,306`
- Modify: `sthrip/services/metrics.py:50-53`
- Modify: `sthrip/db/models.py:59,67,80,145,146,184,220,283,302,358,383`
- Modify: `sthrip/config.py:165`

- [ ] **Step 1: Fix `privacy.py` float/int assignments**

Lines 218, 306: Cast result to `int` where the variable is typed as `int`:
```python
# Line 218: was float, should be int
delay = int(base_delay * (1 + random.random()))
```

- [ ] **Step 2: Fix `metrics.py` Prometheus Noop types**

Lines 50-53: Use `Union` type or cast:
```python
from typing import Union
from prometheus_client import Counter, Histogram
from prometheus_client.metrics import MetricWrapperBase

# Use broader type that encompasses both real metrics and noops
_MetricType = Union[Counter, "prometheus_client.metrics._Noop"]
```

Or simpler — just add `# type: ignore[assignment]` since this is an expected prometheus_client pattern.

- [ ] **Step 3: Fix `models.py` missing type annotations on enum columns**

For each enum column (lines 59, 67, 80, 145, 146, etc.), add explicit `Column[str]` annotation or silence with `# type: ignore[var-annotated]` since SQLAlchemy enums don't have clean mypy support.

Recommended approach — add a `# type: ignore[var-annotated]` comment to each line, since these are standard SQLAlchemy enum columns and mypy's complaint is a known false positive.

- [ ] **Step 4: Fix `config.py:165` missing `admin_api_key`**

Check if `Settings()` truly requires `admin_api_key`. If the field has a default or is only required in production (via validator), this may be a mypy false positive. If it IS required, `get_settings()` relies on the env var being set — which is correct. Add `# type: ignore[call-arg]` with comment explaining pydantic-settings loads from env.

- [ ] **Step 5: Run mypy on core modules**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m mypy sthrip/config.py sthrip/db/models.py sthrip/services/metrics.py sthrip/privacy.py --ignore-missing-imports 2>&1 | head -30`
Expected: No new errors

- [ ] **Step 6: Commit**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
git add sthrip/privacy.py sthrip/services/metrics.py sthrip/db/models.py sthrip/config.py
git commit -m "fix: resolve mypy errors in production-path modules"
```

---

### Task 12: `_auth_redirect` — clear stale cookie + `SQL_ECHO` production guard

**Files:**
- Modify: `sthrip/config.py` (add SQL_ECHO guard in `_validate_settings`)
- Test: `tests/test_config.py`

Already partially done in Task 3 (`_auth_redirect` cookie clear). This task covers the remaining `config.py` guard.

- [ ] **Step 1: Write failing test**

```python
# tests/test_config_sql_echo.py
"""Test that SQL_ECHO=true is rejected in production."""
import os
import pytest


def test_sql_echo_rejected_in_production(monkeypatch):
    from importlib import reload
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SQL_ECHO", "true")
    monkeypatch.setenv("ADMIN_API_KEY", "a" * 32)
    monkeypatch.setenv("API_KEY_HMAC_SECRET", "b" * 32)
    monkeypatch.setenv("WEBHOOK_ENCRYPTION_KEY", "c" * 44 + "=")

    import sthrip.config
    reload(sthrip.config)
    from sthrip.config import Settings

    with pytest.raises(SystemExit):
        Settings()
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_config_sql_echo.py -v`
Expected: FAIL — no guard exists

- [ ] **Step 3: Add guard in `_validate_settings` or `model_post_init`**

In `sthrip/config.py`, in the `Settings` class validators, add:

```python
    @model_validator(mode="after")
    def _reject_sql_echo_in_production(self) -> "Settings":
        if self.sql_echo and self.environment == "production":
            raise SystemExit("SQL_ECHO must not be enabled in production")
        return self
```

- [ ] **Step 4: Run test — expect PASS**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_config_sql_echo.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
git add sthrip/config.py tests/test_config_sql_echo.py
git commit -m "fix: reject SQL_ECHO=true in production environment"
```

---

### Task 13: Idempotency key minimum length + `Retry-After` header

**Files:**
- Modify: `api/routers/payments.py` (idempotency_key min_length)
- Modify: `api/routers/balance.py` (idempotency_key min_length)
- Modify: `api/deps.py` or `api/middleware.py` (Retry-After header on 429)

- [ ] **Step 1: Add `min_length=8` to idempotency key headers**

In `api/routers/payments.py:138` and `api/routers/balance.py:48,187`:

```python
idempotency_key: Optional[str] = Header(None, min_length=8, max_length=255),
```

- [ ] **Step 2: Add `Retry-After` header to RateLimitExceeded responses**

Find where `RateLimitExceeded` is caught and converted to HTTPException (likely in `deps.py` or middleware). Add:

```python
headers={"Retry-After": str(int(exc.reset_at - time.time()))}
```

- [ ] **Step 3: Run existing tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_rate_limiter.py tests/test_idempotency.py -v`
Expected: All pass (update any tests that use short idempotency keys)

- [ ] **Step 4: Commit**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
git add api/routers/payments.py api/routers/balance.py api/deps.py
git commit -m "fix: add idempotency key min_length=8, add Retry-After header on 429"
```

---

### Task 14: Final — run full test suite and verify coverage

- [ ] **Step 1: Run full test suite**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/ -v --tb=short 2>&1 | tail -30`
Expected: All tests pass, no regressions

- [ ] **Step 2: Check coverage**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/ --cov=sthrip --cov=api --cov-report=term-missing 2>&1 | tail -30`
Expected: >= 80% coverage

- [ ] **Step 3: Run mypy on core modules**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m mypy sthrip/wallet.py sthrip/services/deposit_monitor.py sthrip/services/rate_limiter.py sthrip/db/balance_repo.py sthrip/stealth.py sthrip/network.py sthrip/config.py --ignore-missing-imports`
Expected: Reduced error count vs baseline (80 errors → target < 30)

- [ ] **Step 4: Final commit if any cleanup needed**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
git add -A
git commit -m "chore: post-hardening cleanup and test verification"
```
