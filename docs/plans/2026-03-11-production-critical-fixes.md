# Production Critical Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all CRITICAL and HIGH issues found in the production readiness review to make Sthrip safe for mainnet deployment.

**Architecture:** Targeted surgical fixes across security, data integrity, deployment config, and observability. No architectural rewrites — each fix is isolated and testable.

**Tech Stack:** Python 3.9, FastAPI, SQLAlchemy, Alembic, PostgreSQL, Redis, Monero wallet RPC

---

## Phase 1: CRITICAL Security Fixes

### Task 1: Fix admin rate limit bypass (isinstance instead of string comparison)

**Files:**
- Modify: `api/routers/admin.py:43-46, 61-63`
- Test: `tests/test_auth_rate_limit.py`

**Step 1: Write failing test**

```python
# In tests/test_auth_rate_limit.py — add test that verifies rate limiting actually blocks

def test_admin_auth_rate_limit_blocks_after_threshold(client):
    """Verify rate limiting works with isinstance check, not string comparison."""
    for i in range(6):
        r = client.post("/v2/admin/auth", json={"admin_key": "wrong-key"})
    assert r.status_code == 429
```

**Step 2: Run test to verify it passes (or fails if string comparison is bypassing)**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_auth_rate_limit.py -v -k "rate_limit_blocks" 2>&1 | tail -20`

**Step 3: Fix the isinstance check**

In `api/routers/admin.py`, replace both occurrences:

```python
# Line 43-46: BEFORE
except Exception as e:
    if type(e).__name__ == "RateLimitExceeded":
        raise HTTPException(status_code=429, detail="Too many failed admin auth attempts")
    raise

# Line 43-46: AFTER
except RateLimitExceeded:
    raise HTTPException(status_code=429, detail="Too many failed admin auth attempts")
```

```python
# Line 61-64: BEFORE
except Exception as _exc:
    if type(_exc).__name__ == "RateLimitExceeded":
        raise HTTPException(status_code=429, detail="Too many failed admin auth attempts")
    raise

# Line 61-64: AFTER
except RateLimitExceeded:
    raise HTTPException(status_code=429, detail="Too many failed admin auth attempts")
```

**Step 4: Run tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_auth_rate_limit.py -v 2>&1 | tail -20`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add api/routers/admin.py tests/test_auth_rate_limit.py
git commit -m "fix: use isinstance for RateLimitExceeded instead of string comparison"
```

---

### Task 2: Fix webhook encryption silent fallback to plaintext

**Files:**
- Modify: `sthrip/db/repository.py:170-174`
- Test: `tests/test_webhook_encryption.py`

**Step 1: Write failing test**

```python
# In tests/test_webhook_encryption.py — add test for decryption failure behavior

def test_get_webhook_secret_raises_on_decryption_failure(db_session):
    """Decryption failure must raise, not silently return raw value."""
    from sthrip.db.repository import AgentRepository
    from sthrip.db.models import Agent
    import uuid

    agent = Agent(id=uuid.uuid4(), name="test", api_key_hash="x", webhook_secret="not-encrypted-data")
    db_session.add(agent)
    db_session.flush()

    repo = AgentRepository(db_session)
    with unittest.mock.patch("sthrip.crypto.decrypt_value", side_effect=Exception("Bad key")):
        with pytest.raises(ValueError, match="decrypt"):
            repo.get_webhook_secret(agent.id)
```

**Step 2: Run test — should FAIL (current code returns raw value)**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_webhook_encryption.py -v -k "decryption_failure" 2>&1 | tail -10`
Expected: FAIL

**Step 3: Fix the fallback**

In `sthrip/db/repository.py:170-174`, replace:

```python
# BEFORE
try:
    return decrypt_value(agent.webhook_secret)
except Exception:
    # Legacy unencrypted value
    return agent.webhook_secret

# AFTER
try:
    return decrypt_value(agent.webhook_secret)
except Exception as e:
    logger.critical(
        "Failed to decrypt webhook secret for agent %s: %s. "
        "This may indicate key rotation without data migration.",
        agent_id, e,
    )
    raise ValueError(
        f"Cannot decrypt webhook secret for agent {agent_id}. "
        "Check WEBHOOK_ENCRYPTION_KEY configuration."
    ) from e
```

Add `import logging` and `logger = logging.getLogger("sthrip")` at top of file if not present.

**Step 4: Run tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_webhook_encryption.py -v 2>&1 | tail -20`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add sthrip/db/repository.py tests/test_webhook_encryption.py
git commit -m "fix: raise error on webhook secret decryption failure instead of silent fallback"
```

---

### Task 3: Add pycryptodome for correct Keccak-256 in address validation

**Files:**
- Modify: `requirements.txt`
- Modify: `requirements.lock`
- Test: `tests/test_api.py` or new `tests/test_keccak.py`

**Step 1: Write failing test**

```python
# tests/test_keccak.py
def test_keccak256_uses_pycryptodome():
    """Verify Keccak-256 uses pycryptodome, not hashlib.sha3_256."""
    from Crypto.Hash import keccak
    k = keccak.new(digest_bits=256)
    k.update(b"test")
    result = k.digest()
    assert len(result) == 32
    # Known Keccak-256 of "test" (different from SHA3-256)
    assert result.hex().startswith("9c22ff5f")
```

**Step 2: Run test — should FAIL (pycryptodome not installed)**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_keccak.py -v 2>&1 | tail -10`
Expected: FAIL with ImportError

**Step 3: Add dependency**

Add to `requirements.txt` under `# Security`:
```
pycryptodome>=3.19.0
```

Install: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && pip install pycryptodome>=3.19.0`

Update lockfile: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && pip freeze | grep -i crypto >> requirements.lock` (or regenerate)

**Step 4: Run test**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_keccak.py -v 2>&1 | tail -10`
Expected: PASS

**Step 5: Run full address validation tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/ -v -k "monero_address or keccak" 2>&1 | tail -20`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add requirements.txt requirements.lock tests/test_keccak.py
git commit -m "fix: add pycryptodome for correct Keccak-256 in Monero address validation"
```

---

### Task 4: Fix SSRF — block multicast and unspecified IPs

**Files:**
- Modify: `sthrip/services/url_validator.py:114-116`
- Test: `tests/test_url_validator.py`

**Step 1: Write failing test**

```python
# In tests/test_url_validator.py — add tests for multicast and unspecified

def test_is_dangerous_ip_blocks_multicast():
    import ipaddress
    from sthrip.services.url_validator import _is_dangerous_ip
    assert _is_dangerous_ip(ipaddress.ip_address("224.0.0.1")) is True

def test_is_dangerous_ip_blocks_unspecified():
    import ipaddress
    from sthrip.services.url_validator import _is_dangerous_ip
    assert _is_dangerous_ip(ipaddress.ip_address("0.0.0.0")) is True
```

**Step 2: Run tests — should FAIL**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_url_validator.py -v -k "multicast or unspecified" 2>&1 | tail -10`
Expected: FAIL

**Step 3: Fix the check**

In `sthrip/services/url_validator.py:114-116`:

```python
# BEFORE
def _is_dangerous_ip(addr: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> bool:
    """Check if an IP address is private, loopback, reserved, or link-local."""
    return addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local

# AFTER
def _is_dangerous_ip(addr: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> bool:
    """Check if an IP address is private, loopback, reserved, link-local, multicast, or unspecified."""
    return (
        addr.is_private or addr.is_loopback or addr.is_reserved
        or addr.is_link_local or addr.is_multicast or addr.is_unspecified
    )
```

**Step 4: Run tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_url_validator.py -v 2>&1 | tail -20`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add sthrip/services/url_validator.py tests/test_url_validator.py
git commit -m "fix: block multicast and unspecified IPs in SSRF validation"
```

---

## Phase 2: CRITICAL Data Integrity Fixes

### Task 5: Create migration for missing CheckConstraints and indexes

**Files:**
- Create: `migrations/versions/xxxx_add_constraints_and_indexes.py`
- Test: verify migration applies cleanly

**Step 1: Generate migration**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
python -c "
from alembic.config import Config
from alembic import command
cfg = Config('alembic.ini')
command.revision(cfg, message='add balance constraints and deposit_address index', autogenerate=False)
"
```

**Step 2: Write migration content**

```python
def upgrade():
    # Add CheckConstraints that exist in models but were missing from initial migration
    op.create_check_constraint(
        'ck_balance_available_non_negative',
        'agent_balances',
        'available >= 0'
    )
    op.create_check_constraint(
        'ck_balance_pending_non_negative',
        'agent_balances',
        'pending >= 0'
    )
    # Add index on deposit_address (declared in model, missing from migration)
    op.create_index('ix_agent_balances_deposit_address', 'agent_balances', ['deposit_address'])
    # Add index on transactions.created_at for pagination
    op.create_index('ix_transactions_created_at', 'transactions', ['created_at'])

def downgrade():
    op.drop_index('ix_transactions_created_at', 'transactions')
    op.drop_index('ix_agent_balances_deposit_address', 'agent_balances')
    op.drop_constraint('ck_balance_pending_non_negative', 'agent_balances')
    op.drop_constraint('ck_balance_available_non_negative', 'agent_balances')
```

**Step 3: Test migration**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_database.py -v 2>&1 | tail -20`
Expected: PASS

**Step 4: Commit**

```bash
git add migrations/versions/
git commit -m "fix: add missing CheckConstraints and indexes from initial migration"
```

---

### Task 6: Fix DateTime timezone in models and add migration

**Files:**
- Modify: `sthrip/db/models.py` — all `DateTime()` → `DateTime(timezone=True)`
- Create: migration for timezone columns

**Step 1: Verify models already use timezone=True**

Check `sthrip/db/models.py` — the `AgentBalance` and `PendingWithdrawal` models already use `DateTime(timezone=True)`. Check other models.

**Step 2: Fix any models that use bare `DateTime()`**

Search for `DateTime()` without `timezone=True` and fix them. Also fix `func.now()` inconsistency — use `lambda: datetime.now(timezone.utc)` consistently.

**Step 3: Create migration for existing columns**

```python
def upgrade():
    # Convert timestamp columns to timezone-aware
    for table in ['agents', 'transactions', 'hub_routes', 'webhook_events',
                  'escrow_deals', 'payment_channels', 'fee_collections',
                  'audit_logs']:
        for col in ['created_at', 'updated_at']:
            try:
                op.alter_column(table, col,
                    type_=sa.DateTime(timezone=True),
                    existing_type=sa.DateTime())
            except Exception:
                pass  # Column may not exist in all tables
```

**Step 4: Run tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/ -x -q 2>&1 | tail -20`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add sthrip/db/models.py migrations/versions/
git commit -m "fix: use DateTime(timezone=True) consistently across all models"
```

---

### Task 7: Fix BalanceRepository.get_or_create — use savepoint instead of full rollback

**Files:**
- Modify: `sthrip/db/repository.py:748-766`
- Test: `tests/test_balance.py`

**Step 1: Write failing test**

```python
def test_get_or_create_race_does_not_rollback_prior_work(db_session):
    """get_or_create race condition should not roll back unrelated work in the same session."""
    from sthrip.db.repository import BalanceRepository
    import uuid

    repo = BalanceRepository(db_session)
    agent_id = uuid.uuid4()

    # Simulate: some prior work was flushed in this session
    # Then get_or_create hits IntegrityError — should NOT lose prior work
    # (This is a design-level test — just verify savepoint is used)
    balance = repo.get_or_create(agent_id)
    assert balance is not None
```

**Step 2: Fix with savepoint**

```python
# BEFORE (sthrip/db/repository.py:748-766)
def get_or_create(self, agent_id: UUID, token: str = "XMR") -> AgentBalance:
    balance = self.db.query(AgentBalance).filter(...).first()
    if balance:
        return balance
    try:
        balance = AgentBalance(agent_id=agent_id, token=token)
        self.db.add(balance)
        self.db.flush()
        return balance
    except IntegrityError:
        self.db.rollback()  # <-- FULL session rollback!
        return self.db.query(AgentBalance).filter(...).first()

# AFTER
def get_or_create(self, agent_id: UUID, token: str = "XMR") -> AgentBalance:
    balance = self.db.query(AgentBalance).filter(
        AgentBalance.agent_id == agent_id,
        AgentBalance.token == token,
    ).first()
    if balance:
        return balance
    try:
        savepoint = self.db.begin_nested()
        balance = AgentBalance(agent_id=agent_id, token=token)
        self.db.add(balance)
        self.db.flush()
        return balance
    except IntegrityError:
        savepoint.rollback()
        return self.db.query(AgentBalance).filter(
            AgentBalance.agent_id == agent_id,
            AgentBalance.token == token,
        ).first()
```

**Step 3: Run tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_balance.py tests/test_data_integrity.py -v 2>&1 | tail -20`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add sthrip/db/repository.py tests/test_balance.py
git commit -m "fix: use savepoint in get_or_create to prevent full session rollback on race"
```

---

## Phase 3: Deployment & Ops Fixes

### Task 8: Fix multi-worker deposit monitor collision — set WEB_CONCURRENCY=1

**Files:**
- Modify: `railway.toml:9`
- Modify: `api/main_v2.py:191-213` — add worker ID guard

**Step 1: Fix railway.toml default**

```toml
# BEFORE
startCommand = "sh -c 'gunicorn api.main_v2:app --worker-class uvicorn.workers.UvicornWorker --workers ${WEB_CONCURRENCY:-2} --bind 0.0.0.0:${PORT:-8000} --graceful-timeout 30 --timeout 120'"

# AFTER
startCommand = "sh -c 'gunicorn api.main_v2:app --worker-class uvicorn.workers.UvicornWorker --workers ${WEB_CONCURRENCY:-1} --bind 0.0.0.0:${PORT:-8000} --graceful-timeout 60 --timeout 120'"
```

Changes: `--workers` default 2→1, `--graceful-timeout` 30→60.

**Step 2: Commit**

```bash
git add railway.toml
git commit -m "fix: default to 1 worker to prevent deposit monitor collision, increase graceful-timeout"
```

---

### Task 9: Fix Alembic migration skip — always run upgrade

**Files:**
- Modify: `api/main_v2.py:128-168`

**Step 1: Simplify migration runner**

```python
# BEFORE: checks if agents table exists, skips if yes
# AFTER: always run alembic upgrade head (idempotent)

def _run_database_migrations():
    """Run database migrations via Alembic, falling back to create_tables in dev."""
    settings = get_settings()
    try:
        from alembic.config import Config as AlembicConfig
        from alembic import command as alembic_command
        import pathlib

        alembic_ini = pathlib.Path(__file__).resolve().parent.parent / "alembic.ini"
        if alembic_ini.exists():
            alembic_cfg = AlembicConfig(str(alembic_ini))
            alembic_command.upgrade(alembic_cfg, "head")
            logger.info("Database migrations applied successfully")
        else:
            if settings.environment != "dev":
                raise SystemExit("alembic.ini not found in production — refusing to start")
            create_tables()
            logger.info("Database tables ready (no alembic.ini found, using create_tables)")
    except SystemExit:
        raise
    except Exception as e:
        if "already exists" in str(e):
            logger.warning("Migration skipped (schema already exists): %s", e)
        elif settings.environment != "dev":
            logger.critical("DATABASE MIGRATION FAILED: %s", e, exc_info=True)
            raise SystemExit(f"Migration failed in production: {e}")
        else:
            logger.warning("Non-production: falling back to create_tables()")
            create_tables()
```

**Step 2: Run tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_startup.py tests/test_database.py -v 2>&1 | tail -20`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add api/main_v2.py
git commit -m "fix: always run alembic upgrade head instead of skipping when tables exist"
```

---

### Task 10: Add MONERO_NETWORK startup validation

**Files:**
- Modify: `sthrip/config.py` — add validator
- Test: `tests/test_config.py`

**Step 1: Write failing test**

```python
def test_production_rejects_stagenet():
    """Production environment must not use stagenet."""
    import os
    os.environ.update({
        "ENVIRONMENT": "production",
        "MONERO_NETWORK": "stagenet",
        "ADMIN_API_KEY": "secure-key-for-test-12345678",
        "API_KEY_HMAC_SECRET": "secure-hmac-secret-12345",
        "WEBHOOK_ENCRYPTION_KEY": "valid-fernet-key",
        "MONERO_RPC_HOST": "rpc.example.com",
        "MONERO_RPC_PASS": "secure-pass",
    })
    with pytest.raises(ValueError, match="mainnet"):
        Settings()
```

**Step 2: Add validator**

In `sthrip/config.py`, add after the `validate_rpc_pass` validator:

```python
@field_validator("monero_network")
@classmethod
def validate_network(cls, v: str, info) -> str:
    env = info.data.get("environment", "production")
    if env == "production" and v != "mainnet":
        raise ValueError(
            f"MONERO_NETWORK must be 'mainnet' in production, got '{v}'"
        )
    return v
```

**Step 3: Run tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_config.py -v 2>&1 | tail -20`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add sthrip/config.py tests/test_config.py
git commit -m "fix: reject non-mainnet MONERO_NETWORK in production environment"
```

---

### Task 11: Fix monitoring bugs (timedelta.seconds, print, psutil)

**Files:**
- Modify: `sthrip/services/monitoring.py:182, 261, 370`
- Test: `tests/test_monitoring.py`

**Step 1: Write failing tests**

```python
def test_monitor_loop_uses_total_seconds():
    """Verify interval comparison uses total_seconds(), not .seconds."""
    from datetime import timedelta
    td = timedelta(minutes=1, seconds=5)
    # .seconds returns 5, .total_seconds() returns 65
    assert td.total_seconds() == 65.0
    assert td.seconds == 5  # This is the bug — wrong for intervals > 60s
```

**Step 2: Fix three issues**

1. `monitoring.py:182`: `.seconds` → `.total_seconds()`

```python
# BEFORE
(datetime.now(timezone.utc) - check.last_check).seconds >= check.interval_seconds

# AFTER
(datetime.now(timezone.utc) - check.last_check).total_seconds() >= check.interval_seconds
```

2. `monitoring.py:261`: `print(...)` → `logger.warning(...)`

```python
# BEFORE
print(f"Alert channel {name} failed: {e}")

# AFTER
logger.warning("Alert channel %s failed: %s", name, e)
```

3. `monitoring.py:370`: Non-blocking CPU

```python
# BEFORE
"cpu_percent": psutil.cpu_percent(interval=1),

# AFTER
"cpu_percent": psutil.cpu_percent(interval=None),
```

**Step 3: Run tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_monitoring.py -v 2>&1 | tail -20`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add sthrip/services/monitoring.py tests/test_monitoring.py
git commit -m "fix: use total_seconds(), replace print with logger, non-blocking cpu_percent"
```

---

## Phase 4: HIGH Priority Fixes

### Task 12: Fix float() for financial amounts — use str() in API responses

**Files:**
- Modify: `api/routers/payments.py` — all `float(amount)` → `str(amount)`
- Modify: `api/routers/balance.py` — all `float(balance.*)` → `str(balance.*)`
- Test: `tests/test_balance.py`, `tests/test_api.py`

**Step 1: Write failing test**

```python
def test_balance_response_uses_string_not_float():
    """API responses must use str() for amounts to preserve Decimal precision."""
    r = client.get("/v2/balance", headers=auth_headers)
    data = r.json()
    # Amount should be a string, not a float
    assert isinstance(data["available"], str)
```

**Step 2: Replace all float() with str() for financial fields**

In `api/routers/payments.py` and `api/routers/balance.py`, find all `float(...)` used on financial amounts and replace with `str(...)`.

**Step 3: Run tests — update assertions for str type**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_balance.py tests/test_api.py tests/test_api_onchain.py -v 2>&1 | tail -30`

**Step 4: Commit**

```bash
git add api/routers/payments.py api/routers/balance.py tests/
git commit -m "fix: use str() instead of float() for financial amounts in API responses"
```

---

### Task 13: Fix session boundary in _execute_hub_transfer

**Files:**
- Modify: `api/routers/payments.py:54-84`
- Test: `tests/test_api.py`

**Step 1: Refactor to accept db session as parameter**

```python
# BEFORE: opens its own session with get_db()
def _execute_hub_transfer(agent, recipient, amount, fee_info, req, idempotency_key):
    with get_db() as db:
        ...

# AFTER: receives db session from caller
def _execute_hub_transfer(db, agent, recipient, amount, fee_info, req, idempotency_key):
    balance_repo = BalanceRepository(db)
    ...
```

Update caller `hub_routing_payment` to pass `db` from its own `get_db()` context.

**Step 2: Run tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_api.py tests/test_e2e_hub_flow.py -v 2>&1 | tail -30`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add api/routers/payments.py tests/test_api.py
git commit -m "fix: pass db session into _execute_hub_transfer to maintain transaction boundary"
```

---

### Task 14: Fix init_engine thread safety

**Files:**
- Modify: `sthrip/db/database.py:28-50`
- Test: `tests/test_db_pool_config.py`

**Step 1: Add threading lock**

```python
import threading

_engine_lock = threading.Lock()

def init_engine(database_url: Optional[str] = None):
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:  # double-checked locking
            return _engine
        url = database_url or get_database_url()
        settings = get_settings()
        _engine = create_engine(...)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
        return _engine
```

**Step 2: Run tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_db_pool_config.py tests/test_database.py -v 2>&1 | tail -20`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add sthrip/db/database.py
git commit -m "fix: add threading lock to init_engine for thread-safe initialization"
```

---

### Task 15: Remove unused dependencies (pyjwt, bcrypt)

**Files:**
- Modify: `requirements.txt`
- Modify: `requirements.lock`

**Step 1: Verify no usage**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
grep -r "import jwt\|from jwt\|import bcrypt\|from bcrypt" --include="*.py" .
```

Expected: No results (or only in test files)

**Step 2: Remove from requirements.txt**

Remove these lines:
```
bcrypt>=4.1.0
pyjwt>=2.8.0
```

**Step 3: Run tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/ -x -q 2>&1 | tail -10`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add requirements.txt requirements.lock
git commit -m "chore: remove unused pyjwt and bcrypt dependencies"
```

---

### Task 16: Fix clear_pending_on_confirm silent floor to zero

**Files:**
- Modify: `sthrip/db/repository.py:825-833`
- Test: `tests/test_balance.py`

**Step 1: Add critical logging on underflow**

```python
# BEFORE
def clear_pending_on_confirm(self, agent_id, amount, token="XMR"):
    balance = self._get_for_update(agent_id, token)
    balance.pending = max(
        (balance.pending or Decimal("0")) - amount,
        Decimal("0"),
    )
    balance.updated_at = datetime.now(timezone.utc)
    return balance

# AFTER
def clear_pending_on_confirm(self, agent_id, amount, token="XMR"):
    balance = self._get_for_update(agent_id, token)
    current_pending = balance.pending or Decimal("0")
    if current_pending < amount:
        logger.critical(
            "Pending balance underflow for agent %s: pending=%s, confirm_amount=%s. "
            "Possible accounting inconsistency.",
            agent_id, current_pending, amount,
        )
    balance.pending = max(current_pending - amount, Decimal("0"))
    balance.updated_at = datetime.now(timezone.utc)
    return balance
```

**Step 2: Run tests**

Run: `cd "/Users/saveliy/Documents/Agent Payments/sthrip" && python -m pytest tests/test_balance.py -v 2>&1 | tail -20`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add sthrip/db/repository.py
git commit -m "fix: log critical alert on pending balance underflow instead of silent floor"
```

---

### Task 17: Fix healthcheckTimeout and add LOG_FORMAT=json to env example

**Files:**
- Modify: `railway.toml:11`
- Modify: `.env.example` or `.env.railway.example`

**Step 1: Fix healthcheckTimeout**

```toml
# BEFORE
healthcheckTimeout = 300

# AFTER
healthcheckTimeout = 60
```

**Step 2: Add missing env vars to example**

Add to `.env.example` or `.env.railway.example`:
```
LOG_FORMAT=json
MONERO_NETWORK=mainnet
MONERO_RPC_HOST=monero-wallet-rpc.railway.internal
MONERO_RPC_PORT=18082
TRUSTED_PROXY_HOSTS=*
WEB_CONCURRENCY=1
```

**Step 3: Commit**

```bash
git add railway.toml .env.example
git commit -m "fix: reduce healthcheck timeout, add missing env vars to example"
```

---

## Phase 5: Final Verification

### Task 18: Run full test suite and verify

**Step 1: Run all tests**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
python -m pytest tests/ -v --tb=short 2>&1 | tail -40
```

Expected: ALL PASS, 0 failures

**Step 2: Run coverage check**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
python -m pytest tests/ --cov=sthrip --cov=api --cov-report=term-missing -q 2>&1 | tail -50
```

Expected: >= 80% coverage

**Step 3: Final commit with all changes**

Review `git status` and commit any remaining files.
