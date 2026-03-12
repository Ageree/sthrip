# Production Readiness Fixes

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all CRITICAL and HIGH issues from the production readiness review to bring the score from 7.0 to 9.0+.

**Architecture:** Fix metrics definitions, make withdrawals atomic via pending state, centralize config through `Settings`, escape ILIKE wildcards, normalize Prometheus paths, add CSRF tokens to admin forms.

**Tech Stack:** FastAPI, SQLAlchemy, Pydantic Settings, prometheus-client, Redis

---

## Task 1: Fix Prometheus Metrics Label Definitions

**Files:**
- Modify: `sthrip/services/metrics.py:25-34`
- Modify: `api/routers/payments.py:153`
- Modify: `api/routers/balance.py:86,185`
- Test: `tests/test_metrics_labels.py`

**Context:** `hub_payments_total` declares `["status"]` but is called with `(tier, urgency)`. `balance_ops_total` declares `["operation"]` but is called with `(operation, token)`. This crashes at runtime with real prometheus-client.

**Step 1: Write the failing test**

```python
# tests/test_metrics_labels.py
"""Tests that Prometheus metric labels match actual usage."""

import pytest


def test_hub_payments_total_labels():
    """hub_payments_total must accept (status, tier) labels."""
    from sthrip.services.metrics import PROMETHEUS_AVAILABLE
    if not PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus-client not installed")
    from sthrip.services.metrics import hub_payments_total
    # Should not raise ValueError
    hub_payments_total.labels(status="completed", tier="standard").inc()


def test_balance_ops_total_labels():
    """balance_ops_total must accept (operation, token) labels."""
    from sthrip.services.metrics import PROMETHEUS_AVAILABLE
    if not PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus-client not installed")
    from sthrip.services.metrics import balance_ops_total
    # Should not raise ValueError
    balance_ops_total.labels(operation="deposit", token="XMR").inc()
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_metrics_labels.py -v`
Expected: FAIL or SKIP (if prometheus-client not installed, install it first: `pip install prometheus-client`)

**Step 3: Fix metric definitions in `metrics.py`**

Update `sthrip/services/metrics.py` lines 25-34:

```python
hub_payments_total = Counter(
    "hub_payments_total",
    "Hub routing payments created",
    ["status", "tier"],
)
balance_ops_total = Counter(
    "balance_operations_total",
    "Balance operations (deposit/withdraw)",
    ["operation", "token"],
)
```

**Step 4: Fix call sites to use named labels**

In `api/routers/payments.py:153`:
```python
hub_payments_total.labels(status="completed", tier=agent.tier.value).inc()
```

In `api/routers/balance.py:86`:
```python
balance_ops_total.labels(operation="deposit", token="XMR").inc()
```

In `api/routers/balance.py:185`:
```python
balance_ops_total.labels(operation="withdrawal", token="XMR").inc()
```

**Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_metrics_labels.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add sthrip/services/metrics.py api/routers/payments.py api/routers/balance.py tests/test_metrics_labels.py
git commit -m "fix: align Prometheus metric labels with actual usage"
```

---

## Task 2: Normalize Prometheus Endpoint Labels

**Files:**
- Modify: `api/middleware.py:29-38`
- Test: `tests/test_middleware.py` (add test cases)

**Context:** Raw URL paths like `/v2/agents/uuid-here` create unbounded label cardinality. Must normalize to route templates like `/v2/agents/{agent_name}`.

**Step 1: Write the failing test**

Add to existing test file or create new:

```python
# In tests/test_metrics_normalization.py
def test_normalize_path_strips_uuids():
    from api.middleware import _normalize_path
    assert _normalize_path("/v2/agents/550e8400-e29b-41d4-a716-446655440000") == "/v2/agents/{id}"


def test_normalize_path_preserves_static():
    from api.middleware import _normalize_path
    assert _normalize_path("/v2/balance") == "/v2/balance"


def test_normalize_path_strips_multiple_dynamic():
    from api.middleware import _normalize_path
    assert _normalize_path("/v2/agents/my-bot-123/payments") == "/v2/agents/{id}/payments"
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_metrics_normalization.py -v`
Expected: FAIL (function does not exist)

**Step 3: Add `_normalize_path` to middleware**

In `api/middleware.py`, add before `configure_middleware`:

```python
import re as _re

_UUID_RE = _re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    _re.IGNORECASE,
)
_DYNAMIC_SEGMENT_RE = _re.compile(r'/[0-9a-f\-]{20,}', _re.IGNORECASE)

def _normalize_path(path: str) -> str:
    """Replace dynamic path segments (UUIDs, long IDs) with {id}."""
    path = _UUID_RE.sub("{id}", path)
    path = _DYNAMIC_SEGMENT_RE.sub("/{id}", path)
    return path
```

**Step 4: Use it in the middleware**

In `track_metrics`, change line 35:
```python
endpoint = _normalize_path(request.url.path)
```

**Step 5: Run tests**

Run: `python -m pytest tests/test_metrics_normalization.py tests/test_middleware.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add api/middleware.py tests/test_metrics_normalization.py
git commit -m "fix: normalize Prometheus endpoint labels to prevent cardinality explosion"
```

---

## Task 3: Atomic Withdrawal with Pending State

**Files:**
- Modify: `sthrip/db/models.py` (add `PendingWithdrawal` model)
- Modify: `sthrip/db/repository.py` (add `PendingWithdrawalRepository`)
- Modify: `api/routers/balance.py:105-201` (rewrite withdrawal flow)
- Test: `tests/test_withdrawal_atomic.py`

**Context:** Current flow: deduct → RPC → rollback on failure. Crash between deduct and RPC = lost funds. Fix: use a `pending_withdrawals` record as a saga journal.

**Step 1: Write the failing test**

```python
# tests/test_withdrawal_atomic.py
"""Tests for atomic withdrawal with pending state."""

import pytest
from unittest.mock import MagicMock, patch
from decimal import Decimal


def test_withdrawal_creates_pending_record(db_session):
    """Withdrawal must create a pending record before calling RPC."""
    from sthrip.db.repository import PendingWithdrawalRepository
    repo = PendingWithdrawalRepository(db_session)
    pw = repo.create(
        agent_id="test-agent-id",
        amount=Decimal("1.5"),
        address="addr123",
    )
    assert pw.status == "pending"
    assert pw.amount == Decimal("1.5")


def test_withdrawal_marks_completed(db_session):
    """After RPC success, pending withdrawal must be marked completed."""
    from sthrip.db.repository import PendingWithdrawalRepository
    repo = PendingWithdrawalRepository(db_session)
    pw = repo.create(agent_id="test-agent-id", amount=Decimal("1.5"), address="addr123")
    repo.mark_completed(pw.id, tx_hash="abc123")
    updated = repo.get_by_id(pw.id)
    assert updated.status == "completed"
    assert updated.tx_hash == "abc123"


def test_withdrawal_marks_failed_restores_balance(db_session):
    """On RPC failure, mark failed and credit balance back in same transaction."""
    from sthrip.db.repository import PendingWithdrawalRepository, BalanceRepository
    pw_repo = PendingWithdrawalRepository(db_session)
    bal_repo = BalanceRepository(db_session)

    # Setup balance
    bal_repo.deposit("test-agent-id", Decimal("5.0"))

    # Create pending (simulating deduction already happened)
    pw = pw_repo.create(agent_id="test-agent-id", amount=Decimal("1.5"), address="addr123")

    # Mark failed + restore balance in same session
    pw_repo.mark_failed(pw.id, error="RPC timeout")
    bal_repo.credit("test-agent-id", Decimal("1.5"))
    db_session.commit()

    updated = pw_repo.get_by_id(pw.id)
    assert updated.status == "failed"
    balance = bal_repo.get_or_create("test-agent-id")
    assert balance.available >= Decimal("5.0")
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_withdrawal_atomic.py -v`
Expected: FAIL (PendingWithdrawalRepository not found)

**Step 3: Add `PendingWithdrawal` model**

In `sthrip/db/models.py`, add:

```python
class PendingWithdrawal(Base):
    """Saga journal for withdrawal operations."""
    __tablename__ = "pending_withdrawals"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    agent_id = Column(String(36), ForeignKey("agents.id"), nullable=False, index=True)
    amount = Column(Numeric(precision=18, scale=12), nullable=False)
    address = Column(String(256), nullable=False)
    status = Column(String(20), nullable=False, default="pending")  # pending, completed, failed
    tx_hash = Column(String(128), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime(timezone=True), nullable=True)
```

**Step 4: Add `PendingWithdrawalRepository`**

In `sthrip/db/repository.py`, add:

```python
class PendingWithdrawalRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, agent_id: str, amount: Decimal, address: str) -> models.PendingWithdrawal:
        pw = models.PendingWithdrawal(
            agent_id=str(agent_id),
            amount=amount,
            address=address,
            status="pending",
        )
        self.db.add(pw)
        self.db.flush()
        return pw

    def get_by_id(self, pw_id: str) -> Optional[models.PendingWithdrawal]:
        return self.db.query(models.PendingWithdrawal).filter_by(id=pw_id).first()

    def get_pending(self) -> list:
        return self.db.query(models.PendingWithdrawal).filter_by(status="pending").all()

    def mark_completed(self, pw_id: str, tx_hash: str) -> None:
        self.db.query(models.PendingWithdrawal).filter_by(id=pw_id).update({
            "status": "completed",
            "tx_hash": tx_hash,
            "completed_at": datetime.now(timezone.utc),
        })
        self.db.flush()

    def mark_failed(self, pw_id: str, error: str) -> None:
        self.db.query(models.PendingWithdrawal).filter_by(id=pw_id).update({
            "status": "failed",
            "error": error,
            "completed_at": datetime.now(timezone.utc),
        })
        self.db.flush()
```

**Step 5: Rewrite withdrawal endpoint in `api/routers/balance.py`**

Replace the withdrawal flow (lines 120-175) with:

```python
    try:
        amount = req.amount

        # Single DB transaction: deduct balance + create pending record
        with get_db() as db:
            repo = BalanceRepository(db)
            pw_repo = PendingWithdrawalRepository(db)
            try:
                repo.deduct(agent.id, amount)
            except ValueError:
                raise HTTPException(status_code=400, detail="Insufficient balance for this withdrawal")
            balance = repo.get_or_create(agent.id)
            balance.total_withdrawn = (balance.total_withdrawn or Decimal("0")) + amount
            pending = pw_repo.create(agent_id=str(agent.id), amount=amount, address=req.address)
            pending_id = pending.id

        if hub_mode == "onchain":
            wallet_svc = get_wallet_service()
            try:
                tx_result = await asyncio.to_thread(wallet_svc.send_withdrawal, req.address, amount)
            except Exception as e:
                # Rollback: mark failed + restore balance in ONE transaction
                with get_db() as db:
                    pw_repo = PendingWithdrawalRepository(db)
                    bal_repo = BalanceRepository(db)
                    pw_repo.mark_failed(pending_id, error=str(e))
                    bal_repo.credit(agent.id, amount)
                    bal = bal_repo.get_or_create(agent.id)
                    bal.total_withdrawn = (bal.total_withdrawn or Decimal("0")) - amount
                logger.error("Withdrawal RPC failed for agent=%s pw=%s: %s", agent.id, pending_id, e)
                raise HTTPException(status_code=502, detail="Withdrawal processing failed. Please try again later.")

            network = os.getenv("MONERO_NETWORK", "stagenet")
            with get_db() as db:
                pw_repo = PendingWithdrawalRepository(db)
                pw_repo.mark_completed(pending_id, tx_hash=tx_result["tx_hash"])
                tx_repo = TransactionRepository(db)
                tx_repo.create(
                    tx_hash=tx_result["tx_hash"],
                    network=network,
                    from_agent_id=agent.id,
                    to_agent_id=None,
                    amount=amount,
                    fee=tx_result.get("fee", Decimal("0")),
                    payment_type="hub_routing",
                    status="pending",
                )
            # ... rest unchanged
```

Add import at top: `from sthrip.db.repository import BalanceRepository, TransactionRepository, PendingWithdrawalRepository`

**Step 6: Run all tests**

Run: `python -m pytest tests/test_withdrawal_atomic.py tests/test_balance.py -v`
Expected: PASS

**Step 7: Commit**

```bash
git add sthrip/db/models.py sthrip/db/repository.py api/routers/balance.py tests/test_withdrawal_atomic.py
git commit -m "fix: atomic withdrawal with pending state saga journal"
```

---

## Task 4: Escape ILIKE Wildcards in Search

**Files:**
- Modify: `sthrip/services/agent_registry.py:216-217`
- Modify: `api/admin_ui/views.py:223`
- Create: `sthrip/utils.py` (shared escape function)
- Test: `tests/test_ilike_escape.py`

**Context:** User input with `%` or `_` chars is passed directly to ILIKE, allowing wildcard injection for enumeration or DoS via full table scans.

**Step 1: Write the failing test**

```python
# tests/test_ilike_escape.py
def test_escape_ilike_wildcards():
    from sthrip.utils import escape_ilike
    assert escape_ilike("normal") == "normal"
    assert escape_ilike("100%") == r"100\%"
    assert escape_ilike("under_score") == r"under\_score"
    assert escape_ilike("%_%") == r"\%\_\%"
    assert escape_ilike(r"back\slash") == r"back\\slash"
```

**Step 2: Run test — FAIL**

Run: `python -m pytest tests/test_ilike_escape.py -v`

**Step 3: Create `sthrip/utils.py`**

```python
"""Shared utility functions."""


def escape_ilike(value: str) -> str:
    """Escape SQL ILIKE wildcard characters in user input."""
    return (
        value
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
```

**Step 4: Apply in `agent_registry.py:216-217`**

```python
from sthrip.utils import escape_ilike
# ...
agents = db.query(Agent).filter(
    Agent.agent_name.ilike(f"%{escape_ilike(query_str)}%"),
    Agent.is_active == True
).limit(limit).all()
```

**Step 5: Apply in `admin_ui/views.py:223`**

```python
from sthrip.utils import escape_ilike
# ...
query = query.filter(Agent.agent_name.ilike(f"%{escape_ilike(search)}%"))
```

**Step 6: Run tests**

Run: `python -m pytest tests/test_ilike_escape.py -v`
Expected: PASS

**Step 7: Commit**

```bash
git add sthrip/utils.py sthrip/services/agent_registry.py api/admin_ui/views.py tests/test_ilike_escape.py
git commit -m "fix: escape ILIKE wildcards to prevent pattern injection"
```

---

## Task 5: Centralize Config via Settings

**Files:**
- Modify: `sthrip/config.py` (add missing fields: `alert_webhook_url`, `log_format`, `sql_echo`)
- Modify: `api/main_v2.py` (use `get_settings()` instead of `os.getenv()`)
- Modify: `api/middleware.py` (use `get_settings()`)
- Modify: `api/helpers.py` (use `get_settings()`)
- Modify: `api/deps.py` (use `get_settings()`)
- Modify: `api/admin_ui/views.py` (use `get_settings()`)
- Modify: `api/routers/balance.py` (use `get_settings()`)
- Modify: `api/routers/health.py` (use `get_settings()`)
- Modify: `sthrip/db/database.py` (use `get_settings()`)
- Modify: `sthrip/services/rate_limiter.py` (use `get_settings()`)
- Modify: `sthrip/services/deposit_monitor.py` (use `get_settings()`)
- Modify: `sthrip/services/idempotency.py` (use `get_settings()`)
- Modify: `sthrip/services/url_validator.py` (use `get_settings()`)
- Modify: `sthrip/logging_config.py` (use `get_settings()`)
- Test: `tests/test_config.py`

**Context:** The `Settings` class exists in `config.py` with validators, but ALL code uses `os.getenv()` directly, bypassing validation entirely.

**Step 1: Write the failing test**

```python
# tests/test_config.py
"""Tests for centralized Settings config."""
import os
import pytest


def test_settings_loads_from_env(monkeypatch):
    from sthrip.config import Settings
    monkeypatch.setenv("ADMIN_API_KEY", "test-secure-key-12345")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///test.db")
    s = Settings()
    assert s.admin_api_key == "test-secure-key-12345"
    assert s.environment == "dev"


def test_settings_rejects_weak_admin_key_in_production(monkeypatch):
    from sthrip.config import Settings
    monkeypatch.setenv("ADMIN_API_KEY", "change_me")
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(Exception):
        Settings()


def test_settings_has_all_required_fields():
    """Ensure Settings defines fields for all env vars used in the codebase."""
    from sthrip.config import Settings
    fields = Settings.model_fields
    required = [
        "environment", "database_url", "redis_url", "admin_api_key",
        "hub_mode", "monero_rpc_host", "monero_rpc_port",
        "monero_network", "monero_min_confirmations",
        "cors_origins", "trusted_proxy_hosts", "sentry_dsn",
        "log_level", "port", "deposit_poll_interval",
    ]
    for field in required:
        assert field in fields, f"Missing field: {field}"
```

**Step 2: Run test — verify baseline**

Run: `python -m pytest tests/test_config.py -v`
Expected: Most should pass (Settings already has most fields)

**Step 3: Add missing fields to `config.py`**

Add to `Settings` class:
```python
    # Logging
    log_format: str = "text"
    betterstack_source_token: str = ""

    # Database
    sql_echo: bool = False

    # Alerting (optional)
    alert_webhook_url: str = ""
```

**Step 4: Replace `os.getenv()` calls across the codebase**

This is a mechanical replacement. For each file, import `get_settings` and replace `os.getenv("X", default)` with `get_settings().x`.

Example for `api/middleware.py`:
```python
from sthrip.config import get_settings

# Before:
if os.getenv("ENVIRONMENT", "production") != "dev":
# After:
if get_settings().environment != "dev":
```

Repeat for ALL files listed above. Each replacement is one line.

**Important**: Keep `os.getenv` only in `sthrip/client.py` (CLI tool, not part of the app) and `migrations/env.py` (Alembic standalone).

**Step 5: Run full test suite**

Run: `python -m pytest tests/ -v --tb=short`
Expected: PASS (may need to set ADMIN_API_KEY in test env)

**Step 6: Commit**

```bash
git add sthrip/config.py api/ sthrip/ tests/test_config.py
git commit -m "refactor: centralize all config through Settings, remove scattered os.getenv"
```

---

## Task 6: Add CSRF Token to Admin Login

**Files:**
- Modify: `api/admin_ui/views.py` (generate + verify CSRF token)
- Modify: `api/admin_ui/templates/login.html` (include hidden CSRF field)
- Test: `tests/test_admin_ui.py` (add CSRF tests)

**Context:** Admin login form has no CSRF protection beyond SameSite=Strict cookie. Add defense-in-depth with a per-form CSRF token.

**Step 1: Write the failing test**

```python
# Add to tests/test_admin_ui.py

def test_login_form_contains_csrf_token(client):
    """GET /admin/login must include a CSRF token in the form."""
    resp = client.get("/admin/login")
    assert "csrf_token" in resp.text


def test_login_rejects_missing_csrf(client):
    """POST /admin/login without CSRF token must fail."""
    resp = client.post("/admin/login", data={"admin_key": "test-key"})
    assert resp.status_code in (403, 422)


def test_login_rejects_invalid_csrf(client):
    """POST /admin/login with wrong CSRF token must fail."""
    resp = client.post("/admin/login", data={"admin_key": "test-key", "csrf_token": "wrong"})
    assert resp.status_code == 403
```

**Step 2: Run test — FAIL**

**Step 3: Implement CSRF in views.py**

Add to `_SessionStore`:
```python
def create_csrf_token(self) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    # Store with short TTL (10 min)
    if self._redis:
        self._redis.setex(f"csrf:{token_hash}", 600, "1")
    else:
        self._local[f"csrf:{token_hash}"] = {"expires": time.time() + 600}
    return token

def verify_csrf_token(self, token: str) -> bool:
    if not token:
        return False
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key = f"csrf:{token_hash}"
    if self._redis:
        result = self._redis.get(key)
        if result:
            self._redis.delete(key)  # single-use
            return True
        return False
    else:
        entry = self._local.pop(key, None)
        return entry is not None and entry["expires"] > time.time()
```

Update `login_page` to generate and pass CSRF token:
```python
@router.get("/login")
async def login_page(request: Request):
    csrf_token = _session_store.create_csrf_token()
    return templates.TemplateResponse("login.html", {
        "request": request, "error": None, "csrf_token": csrf_token
    })
```

Update `login_submit` to verify:
```python
@router.post("/login")
async def login_submit(request: Request, admin_key: str = Form(...), csrf_token: str = Form("")):
    if not _session_store.verify_csrf_token(csrf_token):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid form submission", "csrf_token": _session_store.create_csrf_token()},
            status_code=403,
        )
    # ... rest unchanged
```

Update `login.html` to include hidden field:
```html
<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_admin_ui.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add api/admin_ui/views.py api/admin_ui/templates/login.html tests/test_admin_ui.py
git commit -m "fix: add CSRF token to admin login form"
```

---

## Task 7: Fix `get_stats()` N+1 Query

**Files:**
- Modify: `sthrip/services/agent_registry.py:312-341`
- Test: `tests/test_registry.py` (add test)

**Context:** `get_stats()` makes 6 separate queries. Consolidate into one with `GROUP BY`.

**Step 1: Write the test**

```python
def test_get_stats_returns_correct_structure(db_session):
    from sthrip.services.agent_registry import AgentRegistry
    registry = AgentRegistry()
    stats = registry.get_stats()
    assert "total_agents" in stats
    assert "by_tier" in stats
    assert "verified_count" in stats
```

**Step 2: Rewrite `get_stats`**

```python
def get_stats(self) -> dict:
    with get_db() as db:
        from sqlalchemy import func
        # Single query: count + group by tier
        rows = db.query(
            Agent.rate_limit_tier, func.count(Agent.id)
        ).filter(Agent.is_active == True).group_by(Agent.rate_limit_tier).all()

        by_tier = {row[0].value: row[1] for row in rows}
        total = sum(by_tier.values())

        verified = db.query(func.count(Agent.id)).filter(
            Agent.is_active == True, Agent.is_verified == True
        ).scalar()

        return {
            "total_agents": total,
            "by_tier": by_tier,
            "verified_count": verified,
        }
```

**Step 3: Run tests**

Run: `python -m pytest tests/ -k "stats" -v`
Expected: PASS

**Step 4: Commit**

```bash
git add sthrip/services/agent_registry.py tests/test_registry.py
git commit -m "perf: consolidate get_stats into 2 queries instead of 6"
```

---

## Task 8: Make DATABASE_URL a Hard Failure

**Files:**
- Modify: `api/main_v2.py:85-95` (change from warning to SystemExit)
- Test: `tests/test_startup.py`

**Context:** Missing DATABASE_URL only logs a warning. Must be a hard failure.

**Step 1: Write the test**

```python
# tests/test_startup.py
import pytest
import os


def test_startup_fails_without_database_url(monkeypatch):
    """App must refuse to start without DATABASE_URL."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_API_KEY", "test-key")
    monkeypatch.setenv("ENVIRONMENT", "dev")

    # Import and call lifespan check
    # This should raise SystemExit or ValueError
    with pytest.raises(SystemExit):
        from api.main_v2 import _validate_required_env
        _validate_required_env()
```

**Step 2: Extract validation into `_validate_required_env` and raise SystemExit**

In `api/main_v2.py`, replace the warning with:

```python
def _validate_required_env():
    required = ["DATABASE_URL", "ADMIN_API_KEY"]
    missing = [var for var in required if not os.getenv(var)]
    if missing:
        for var in missing:
            logger.critical("REQUIRED env var %s is not set!", var)
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}")
```

**Step 3: Run tests**

Run: `python -m pytest tests/test_startup.py -v`

**Step 4: Commit**

```bash
git add api/main_v2.py tests/test_startup.py
git commit -m "fix: hard fail on missing DATABASE_URL at startup"
```

---

## Summary

| Task | Severity | Description |
|------|----------|-------------|
| 1 | CRITICAL | Fix Prometheus metrics label mismatch |
| 2 | HIGH | Normalize Prometheus endpoint labels |
| 3 | HIGH | Atomic withdrawal with pending state |
| 4 | HIGH | Escape ILIKE wildcards |
| 5 | MEDIUM | Centralize config via Settings |
| 6 | MEDIUM | Add CSRF token to admin login |
| 7 | MEDIUM | Fix get_stats N+1 query |
| 8 | MEDIUM | Hard fail on missing DATABASE_URL |

**Estimated total**: 8 tasks, each 5-15 min implementation.
**After completion**: Re-run production readiness review to verify score improvement.
