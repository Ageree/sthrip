# Production Hardening — Final Fixes

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all CRITICAL and IMPORTANT issues from the production readiness review to bring the score from 6/10 to 8+/10.

**Architecture:** Replace SHA-256 API key hashing with HMAC+server-secret, add pending withdrawal recovery on startup, unify auth session with request session, encrypt webhook secrets at rest, add thread-safe singletons, bundle Tailwind locally, use configured pool sizes, cap alert list.

**Tech Stack:** Python 3.9, FastAPI, SQLAlchemy, cryptography (Fernet), hmac, threading

---

## Task 1: HMAC-based API Key Hashing (C1)

**Problem:** API keys hashed with unsalted SHA-256. DB leak → trivially brute-forceable.

**Solution:** Use `hmac.new(server_secret, api_key, sha256)` — still O(1) queryable, safe against DB-only compromise.

**Files:**
- Modify: `sthrip/config.py` — add `api_key_hmac_secret` setting
- Modify: `sthrip/db/repository.py:39,69` — replace `hashlib.sha256` with HMAC
- Create: `tests/test_api_key_hmac.py`

**Step 1: Write the failing test**

```python
# tests/test_api_key_hmac.py
"""Tests for HMAC-based API key hashing."""
import hmac
import hashlib
import os
from unittest.mock import patch

import pytest


def test_hmac_hash_differs_from_sha256():
    """HMAC hash must differ from plain SHA-256."""
    from sthrip.db.repository import AgentRepository
    api_key = "sk_test_key_1234"
    sha256_hash = hashlib.sha256(api_key.encode()).hexdigest()
    hmac_hash = AgentRepository._hash_api_key(api_key)
    assert hmac_hash != sha256_hash


def test_hmac_hash_deterministic():
    """Same key must produce same hash."""
    from sthrip.db.repository import AgentRepository
    key = "sk_test_deterministic"
    assert AgentRepository._hash_api_key(key) == AgentRepository._hash_api_key(key)


def test_hmac_hash_uses_server_secret():
    """Changing server secret must change hash."""
    from sthrip.db.repository import AgentRepository
    key = "sk_test_secret_change"
    hash1 = AgentRepository._hash_api_key(key)
    with patch("sthrip.db.repository._get_hmac_secret", return_value="different_secret"):
        hash2 = AgentRepository._hash_api_key(key)
    assert hash1 != hash2


def test_create_agent_uses_hmac(db_session):
    """AgentRepository.create_agent must store HMAC hash, not SHA-256."""
    from sthrip.db.repository import AgentRepository
    repo = AgentRepository(db_session)
    agent = repo.create_agent("hmac_test_agent")
    plain_key = agent._plain_api_key
    sha256 = hashlib.sha256(plain_key.encode()).hexdigest()
    assert agent.api_key_hash != sha256
    assert agent.api_key_hash == AgentRepository._hash_api_key(plain_key)


def test_get_by_api_key_uses_hmac(db_session):
    """Lookup must use HMAC hash."""
    from sthrip.db.repository import AgentRepository
    repo = AgentRepository(db_session)
    agent = repo.create_agent("hmac_lookup_agent")
    db_session.flush()
    found = repo.get_by_api_key(agent._plain_api_key)
    assert found is not None
    assert found.id == agent.id
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_api_key_hmac.py -v`
Expected: FAIL — `AgentRepository._hash_api_key` does not exist

**Step 3: Add `api_key_hmac_secret` to Settings**

In `sthrip/config.py`, add field:
```python
    # API key HMAC secret (used to hash API keys in DB)
    api_key_hmac_secret: str = Field(default="dev-hmac-secret-change-in-prod")
```

Add validator:
```python
    @field_validator("api_key_hmac_secret")
    @classmethod
    def validate_hmac_secret(cls, v: str, info) -> str:
        env = info.data.get("environment", "production")
        if env != "dev" and v == "dev-hmac-secret-change-in-prod":
            raise ValueError(
                "API_KEY_HMAC_SECRET must be set to a secure random value in non-dev environments"
            )
        return v
```

**Step 4: Replace SHA-256 with HMAC in repository.py**

At the top of `sthrip/db/repository.py`, add helper:
```python
import hmac as _hmac

def _get_hmac_secret() -> str:
    from sthrip.config import get_settings
    return get_settings().api_key_hmac_secret
```

In class `AgentRepository`, add static method and update two methods:
```python
    @staticmethod
    def _hash_api_key(api_key: str) -> str:
        """Hash API key using HMAC-SHA256 with server secret."""
        secret = _get_hmac_secret()
        return _hmac.new(secret.encode(), api_key.encode(), hashlib.sha256).hexdigest()

    def create_agent(self, ...):
        ...
        api_key_hash = self._hash_api_key(api_key)  # was: hashlib.sha256(api_key.encode()).hexdigest()
        ...

    def get_by_api_key(self, api_key: str):
        api_key_hash = self._hash_api_key(api_key)  # was: hashlib.sha256(api_key.encode()).hexdigest()
        ...
```

**Step 5: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_api_key_hmac.py -v`
Expected: PASS

**Step 6: Run full test suite to check for regressions**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/ -x -q`
Expected: All pass (existing tests create agents in-memory, HMAC is transparent)

**Step 7: Commit**

```bash
git add sthrip/config.py sthrip/db/repository.py tests/test_api_key_hmac.py
git commit -m "feat: replace SHA-256 API key hashing with HMAC+server-secret (C1)"
```

---

## Task 2: Pending Withdrawal Recovery (C2)

**Problem:** If process crashes after wallet RPC success but before recording tx_hash, XMR is sent but `PendingWithdrawal` stays `pending` forever. No recovery mechanism.

**Solution:** On startup, scan `pending` withdrawals older than 5 minutes, check wallet's outgoing transfers, reconcile.

**Files:**
- Modify: `sthrip/db/repository.py` — add `get_stale_pending()` method
- Create: `sthrip/services/withdrawal_recovery.py` — recovery logic
- Modify: `api/main_v2.py` — call recovery in lifespan
- Create: `tests/test_withdrawal_recovery.py`

**Step 1: Write the failing test**

```python
# tests/test_withdrawal_recovery.py
"""Tests for pending withdrawal recovery on startup."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest


def test_get_stale_pending_returns_old_records(db_session):
    """get_stale_pending returns withdrawals older than threshold."""
    from sthrip.db.repository import PendingWithdrawalRepository
    from sthrip.db.models import PendingWithdrawal
    import uuid

    agent_id = str(uuid.uuid4())
    # Create a stale record (10 min old)
    pw = PendingWithdrawal(
        agent_id=agent_id,
        amount=Decimal("1.0"),
        address="addr_stale",
        status="pending",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    db_session.add(pw)
    db_session.flush()

    repo = PendingWithdrawalRepository(db_session)
    stale = repo.get_stale_pending(max_age_minutes=5)
    assert len(stale) == 1
    assert stale[0].id == pw.id


def test_get_stale_pending_ignores_recent(db_session):
    """get_stale_pending ignores records younger than threshold."""
    from sthrip.db.repository import PendingWithdrawalRepository
    from sthrip.db.models import PendingWithdrawal
    import uuid

    agent_id = str(uuid.uuid4())
    pw = PendingWithdrawal(
        agent_id=agent_id,
        amount=Decimal("1.0"),
        address="addr_recent",
        status="pending",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    db_session.add(pw)
    db_session.flush()

    repo = PendingWithdrawalRepository(db_session)
    stale = repo.get_stale_pending(max_age_minutes=5)
    assert len(stale) == 0


def test_recovery_marks_completed_when_tx_found():
    """Recovery marks pending as completed when wallet shows matching tx."""
    from sthrip.services.withdrawal_recovery import recover_pending_withdrawals
    from unittest.mock import MagicMock
    from decimal import Decimal

    mock_pw = MagicMock()
    mock_pw.id = "pw-1"
    mock_pw.address = "addr_found"
    mock_pw.amount = Decimal("1.5")
    mock_pw.agent_id = "agent-1"

    mock_wallet = MagicMock()
    mock_wallet.get_outgoing_transfers.return_value = [
        {"address": "addr_found", "amount": 1.5, "tx_hash": "abc123"}
    ]

    mock_pw_repo = MagicMock()
    mock_pw_repo.get_stale_pending.return_value = [mock_pw]

    recovered = recover_pending_withdrawals(
        pw_repo=mock_pw_repo,
        wallet_service=mock_wallet,
    )
    mock_pw_repo.mark_completed.assert_called_once_with("pw-1", tx_hash="abc123")
    assert recovered == 1


def test_recovery_marks_failed_when_no_tx():
    """Recovery marks pending as failed when no matching wallet tx found."""
    from sthrip.services.withdrawal_recovery import recover_pending_withdrawals

    mock_pw = MagicMock()
    mock_pw.id = "pw-2"
    mock_pw.address = "addr_missing"
    mock_pw.amount = Decimal("2.0")
    mock_pw.agent_id = "agent-2"

    mock_wallet = MagicMock()
    mock_wallet.get_outgoing_transfers.return_value = []

    mock_pw_repo = MagicMock()
    mock_pw_repo.get_stale_pending.return_value = [mock_pw]

    mock_bal_repo = MagicMock()

    recovered = recover_pending_withdrawals(
        pw_repo=mock_pw_repo,
        wallet_service=mock_wallet,
        balance_repo=mock_bal_repo,
    )
    mock_pw_repo.mark_failed.assert_called_once()
    mock_bal_repo.credit.assert_called_once_with("agent-2", Decimal("2.0"))
    assert recovered == 1
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_withdrawal_recovery.py -v`
Expected: FAIL — modules don't exist

**Step 3: Add `get_stale_pending()` to PendingWithdrawalRepository**

In `sthrip/db/repository.py`, in class `PendingWithdrawalRepository`:
```python
    def get_stale_pending(self, max_age_minutes: int = 5) -> list:
        """Get pending withdrawals older than max_age_minutes."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        return self.db.query(PendingWithdrawal).filter(
            PendingWithdrawal.status == "pending",
            PendingWithdrawal.created_at < cutoff,
        ).all()
```

**Step 4: Create withdrawal recovery service**

```python
# sthrip/services/withdrawal_recovery.py
"""Startup recovery for pending withdrawals (saga completion)."""
import logging
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("sthrip")


def recover_pending_withdrawals(
    pw_repo,
    wallet_service=None,
    balance_repo=None,
    max_age_minutes: int = 5,
) -> int:
    """Scan stale pending withdrawals and reconcile with wallet.

    Returns the number of records recovered.
    """
    stale = pw_repo.get_stale_pending(max_age_minutes=max_age_minutes)
    if not stale:
        return 0

    logger.info("Found %d stale pending withdrawals, reconciling...", len(stale))

    # Get recent outgoing transfers from wallet (if available)
    outgoing = []
    if wallet_service is not None:
        try:
            outgoing = wallet_service.get_outgoing_transfers()
        except Exception as e:
            logger.error("Failed to fetch outgoing transfers for recovery: %s", e)
            return 0

    recovered = 0
    for pw in stale:
        tx_match = _find_matching_transfer(pw, outgoing)
        if tx_match:
            pw_repo.mark_completed(pw.id, tx_hash=tx_match["tx_hash"])
            logger.info(
                "Recovery: marked pw=%s as completed (tx=%s)",
                pw.id, tx_match["tx_hash"],
            )
        else:
            # No matching on-chain tx → mark failed + restore balance
            pw_repo.mark_failed(pw.id, error="Recovery: no matching on-chain tx found")
            if balance_repo is not None:
                balance_repo.credit(pw.agent_id, pw.amount)
                logger.info(
                    "Recovery: restored %.12f to agent=%s (pw=%s)",
                    pw.amount, pw.agent_id, pw.id,
                )
        recovered += 1

    return recovered


def _find_matching_transfer(pw, outgoing: list) -> Optional[dict]:
    """Find an outgoing transfer matching a pending withdrawal."""
    for tx in outgoing:
        if tx.get("address") == pw.address:
            tx_amount = Decimal(str(tx.get("amount", 0)))
            if abs(tx_amount - pw.amount) < Decimal("0.000000000001"):
                return tx
    return None
```

**Step 5: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_withdrawal_recovery.py -v`
Expected: PASS

**Step 6: Wire recovery into lifespan**

In `api/main_v2.py`, after database migration block and before "Start health monitoring", add:
```python
    # Recover stale pending withdrawals
    if hub_mode == "onchain":
        try:
            from sthrip.services.withdrawal_recovery import recover_pending_withdrawals
            from sthrip.db.repository import PendingWithdrawalRepository, BalanceRepository
            wallet_svc = get_wallet_service()
            with get_db() as db:
                pw_repo = PendingWithdrawalRepository(db)
                bal_repo = BalanceRepository(db)
                recovered = recover_pending_withdrawals(
                    pw_repo=pw_repo,
                    wallet_service=wallet_svc,
                    balance_repo=bal_repo,
                )
                if recovered:
                    logger.info("Recovered %d stale pending withdrawals", recovered)
        except Exception as e:
            logger.error("Withdrawal recovery failed (non-fatal): %s", e)
```

**Step 7: Run full test suite**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/ -x -q`
Expected: All pass

**Step 8: Commit**

```bash
git add sthrip/db/repository.py sthrip/services/withdrawal_recovery.py api/main_v2.py tests/test_withdrawal_recovery.py
git commit -m "feat: add pending withdrawal recovery on startup (C2)"
```

---

## Task 3: Unify Auth Session with Request Session (C3)

**Problem:** `get_current_agent` opens its own DB session, returns detached ORM object. Route handler opens a different session → TOCTOU gap.

**Solution:** Make `get_current_agent` accept `db: Session = Depends(get_db_session)` and reuse the same session.

**Files:**
- Modify: `api/deps.py:52-103` — accept `db` via `Depends`
- Create: `tests/test_auth_session_unity.py`

**Step 1: Write the failing test**

```python
# tests/test_auth_session_unity.py
"""Tests that auth and request handler share the same DB session."""
import inspect
from unittest.mock import MagicMock

import pytest
from fastapi import Depends


def test_get_current_agent_accepts_db_param():
    """get_current_agent must accept a db session parameter."""
    from api.deps import get_current_agent
    sig = inspect.signature(get_current_agent)
    assert "db" in sig.parameters, "get_current_agent must accept a 'db' parameter"


def test_get_current_agent_does_not_call_get_db_directly():
    """get_current_agent must NOT call get_db() directly — it should use injected session."""
    import ast
    import textwrap
    from api import deps
    source = inspect.getsource(deps.get_current_agent)
    # Should not contain `with get_db()` pattern
    assert "with get_db()" not in source, (
        "get_current_agent must use injected db session, not call get_db() directly"
    )
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_auth_session_unity.py -v`
Expected: FAIL — current impl calls `with get_db()` and has no `db` parameter

**Step 3: Refactor `get_current_agent` to use injected session**

In `api/deps.py`, modify:
```python
async def get_current_agent(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    request: Request = None,
    db: Session = Depends(get_db_session),
) -> Agent:
    """Authenticate agent using the same DB session as the request handler."""
    client_ip = request.client.host if request and request.client else "unknown"
    limiter = get_rate_limiter()

    _check_failed_auth_limit(limiter, client_ip)

    if not credentials:
        _record_failed_auth(limiter, client_ip)
        audit_log("auth.failed", ip_address=client_ip, details={"reason": "missing_api_key"}, success=False)
        raise HTTPException(status_code=401, detail="Missing API key")

    api_key = credentials.credentials

    repo = AgentRepository(db)
    agent = repo.get_by_api_key(api_key)

    if not agent:
        _record_failed_auth(limiter, client_ip)
        audit_log("auth.failed", ip_address=client_ip, details={"reason": "invalid_api_key"}, success=False)
        raise HTTPException(status_code=401, detail="Invalid API key")

    if not agent.is_active:
        raise HTTPException(status_code=403, detail="Agent account disabled")

    repo.update_last_seen(agent.id)

    try:
        path = request.url.path if request else "/"
        limiter.check_rate_limit(
            agent_id=str(agent.id),
            tier=agent.rate_limit_tier.value,
            endpoint=path
        )
    except RateLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Rate limit exceeded",
                "limit": e.limit,
                "reset_at": e.reset_at
            }
        )

    return agent
```

**Step 4: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_auth_session_unity.py -v`
Expected: PASS

**Step 5: Run full test suite, fix any broken patches**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/ -x -q`

Note: Some existing tests mock `get_db` in `api.deps`. These patches may need updating since `get_current_agent` no longer calls `get_db()` directly — it uses `get_db_session` via Depends. Tests that mock `get_current_agent` directly should be unaffected.

**Step 6: Commit**

```bash
git add api/deps.py tests/test_auth_session_unity.py
git commit -m "fix: unify auth DB session with request session — eliminate TOCTOU gap (C3)"
```

---

## Task 4: Encrypt Webhook Secrets at Rest (C4)

**Problem:** `webhook_secret` stored as plaintext in DB. Compromised DB → attacker can forge webhooks.

**Solution:** Use Fernet symmetric encryption with a key from env var.

**Files:**
- Modify: `sthrip/config.py` — add `webhook_encryption_key` setting
- Create: `sthrip/crypto.py` — encrypt/decrypt helpers
- Modify: `sthrip/db/repository.py` — encrypt on create, decrypt on read
- Modify: `sthrip/services/webhook_service.py` — decrypt before signing
- Create: `tests/test_webhook_encryption.py`

**Step 1: Write the failing test**

```python
# tests/test_webhook_encryption.py
"""Tests for webhook secret encryption at rest."""
import pytest


def test_encrypt_decrypt_roundtrip():
    """Encrypting then decrypting returns original value."""
    from sthrip.crypto import encrypt_value, decrypt_value
    original = "whsec_test123456"
    encrypted = encrypt_value(original)
    assert encrypted != original
    assert decrypt_value(encrypted) == original


def test_encrypted_value_is_not_plaintext():
    """Encrypted value must not contain the original string."""
    from sthrip.crypto import encrypt_value
    original = "whsec_abcdef"
    encrypted = encrypt_value(original)
    assert "whsec_" not in encrypted


def test_create_agent_stores_encrypted_secret(db_session):
    """Agent webhook_secret in DB must be encrypted."""
    from sthrip.db.repository import AgentRepository
    repo = AgentRepository(db_session)
    agent = repo.create_agent("encrypt_test_agent", webhook_url="https://example.com/hook")
    db_session.flush()
    # The stored value should be encrypted (not start with whsec_)
    assert not agent.webhook_secret.startswith("whsec_")


def test_get_webhook_secret_decrypted(db_session):
    """Reading webhook secret via repo must return decrypted value."""
    from sthrip.db.repository import AgentRepository
    repo = AgentRepository(db_session)
    agent = repo.create_agent("decrypt_test_agent", webhook_url="https://example.com/hook")
    db_session.flush()
    decrypted = repo.get_webhook_secret(agent.id)
    assert decrypted.startswith("whsec_")
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_webhook_encryption.py -v`
Expected: FAIL — `sthrip.crypto` does not exist

**Step 3: Add `webhook_encryption_key` to Settings**

In `sthrip/config.py`:
```python
    # Webhook secret encryption key (Fernet key, base64-encoded 32 bytes)
    webhook_encryption_key: str = Field(default="")
```

**Step 4: Create crypto module**

```python
# sthrip/crypto.py
"""Symmetric encryption for secrets at rest."""
import base64
import hashlib

from cryptography.fernet import Fernet

from sthrip.config import get_settings

_fernet_instance = None


def _get_fernet() -> Fernet:
    """Get or create Fernet instance from config key."""
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    key = get_settings().webhook_encryption_key
    if not key:
        # Derive from HMAC secret as fallback (always available)
        raw = get_settings().api_key_hmac_secret.encode()
        derived = hashlib.sha256(raw).digest()
        key = base64.urlsafe_b64encode(derived).decode()

    _fernet_instance = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet_instance


def encrypt_value(plaintext: str) -> str:
    """Encrypt a string value. Returns base64-encoded ciphertext."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a base64-encoded ciphertext back to plaintext."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()
```

**Step 5: Update AgentRepository to encrypt/decrypt webhook secrets**

In `sthrip/db/repository.py`, in `create_agent`:
```python
        from sthrip.crypto import encrypt_value
        webhook_secret = f"whsec_{secrets.token_hex(24)}"
        encrypted_secret = encrypt_value(webhook_secret)
        # Store encrypted, but keep plain for one-time return
        ...
        agent = models.Agent(
            ...
            webhook_secret=encrypted_secret,
            ...
        )
```

Add method to AgentRepository:
```python
    def get_webhook_secret(self, agent_id: UUID) -> Optional[str]:
        """Get decrypted webhook secret for agent."""
        from sthrip.crypto import decrypt_value
        agent = self.get_by_id(agent_id)
        if not agent or not agent.webhook_secret:
            return None
        try:
            return decrypt_value(agent.webhook_secret)
        except Exception:
            # Legacy unencrypted value
            return agent.webhook_secret
```

**Step 6: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_webhook_encryption.py -v`
Expected: PASS

**Step 7: Run full test suite**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/ -x -q`
Expected: All pass

**Step 8: Commit**

```bash
git add sthrip/config.py sthrip/crypto.py sthrip/db/repository.py tests/test_webhook_encryption.py
git commit -m "feat: encrypt webhook secrets at rest with Fernet (C4)"
```

---

## Task 5: Thread-Safe Singleton Factories (I2)

**Problem:** `get_fee_collector()`, `get_registry()`, `get_webhook_service()`, `get_monitor()` use bare `global` without locks. Race condition under concurrent requests.

**Solution:** Add `threading.Lock()` guards to each.

**Files:**
- Modify: `sthrip/services/fee_collector.py:405-414`
- Modify: `sthrip/services/agent_registry.py:342-351`
- Modify: `sthrip/services/webhook_service.py:287-295`
- Modify: `sthrip/services/monitoring.py:450-459`
- Create: `tests/test_singleton_thread_safety.py`

**Step 1: Write the failing test**

```python
# tests/test_singleton_thread_safety.py
"""Tests that singleton factories are thread-safe."""
import threading
import inspect

import pytest


@pytest.mark.parametrize("module_path,func_name", [
    ("sthrip.services.fee_collector", "get_fee_collector"),
    ("sthrip.services.agent_registry", "get_registry"),
    ("sthrip.services.webhook_service", "get_webhook_service"),
    ("sthrip.services.monitoring", "get_monitor"),
])
def test_singleton_factory_uses_lock(module_path, func_name):
    """Each singleton factory must use a threading lock."""
    import importlib
    mod = importlib.import_module(module_path)
    source = inspect.getsource(getattr(mod, func_name))
    assert "_lock" in source or "Lock" in source, (
        f"{module_path}.{func_name} must use a threading Lock for thread safety"
    )
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_singleton_thread_safety.py -v`
Expected: FAIL — no locks in these functions

**Step 3: Add locks to all four singletons**

Pattern for each (example: `fee_collector.py`):
```python
import threading

_collector: Optional[FeeCollector] = None
_collector_lock = threading.Lock()

def get_fee_collector() -> FeeCollector:
    global _collector
    if _collector is None:
        with _collector_lock:
            if _collector is None:
                _collector = FeeCollector()
    return _collector
```

Apply same pattern to `agent_registry.py`, `webhook_service.py`, `monitoring.py`.

**Step 4: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_singleton_thread_safety.py -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/ -x -q`
Expected: All pass

**Step 6: Commit**

```bash
git add sthrip/services/fee_collector.py sthrip/services/agent_registry.py sthrip/services/webhook_service.py sthrip/services/monitoring.py tests/test_singleton_thread_safety.py
git commit -m "fix: add thread-safe locks to all singleton factories (I2)"
```

---

## Task 6: Use Configured DB Pool Sizes (I6)

**Problem:** `pool_size=10` and `max_overflow=20` are hardcoded in `database.py` despite `Settings` having the values.

**Files:**
- Modify: `sthrip/db/database.py:37-45`
- Create: `tests/test_db_pool_config.py`

**Step 1: Write the failing test**

```python
# tests/test_db_pool_config.py
"""Tests that database uses configured pool sizes."""
import inspect

import pytest


def test_init_engine_uses_settings_pool_size():
    """init_engine must read pool_size from Settings, not hardcode."""
    from sthrip.db import database
    source = inspect.getsource(database.init_engine)
    # Should reference settings, not have literal 10 for pool_size
    assert "db_pool_size" in source or "settings.db_pool_size" in source, (
        "init_engine must use settings.db_pool_size instead of hardcoded value"
    )
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_db_pool_config.py -v`
Expected: FAIL

**Step 3: Use settings in init_engine**

In `sthrip/db/database.py`, modify `init_engine`:
```python
def init_engine(database_url: Optional[str] = None):
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine

    url = database_url or get_database_url()
    settings = get_settings()

    _engine = create_engine(
        url,
        poolclass=QueuePool,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_overflow,
        pool_pre_ping=True,
        pool_recycle=3600,
        echo=settings.sql_echo,
    )
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine
```

**Step 4: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_db_pool_config.py tests/ -x -q`
Expected: All pass

**Step 5: Commit**

```bash
git add sthrip/db/database.py tests/test_db_pool_config.py
git commit -m "fix: use configured pool_size/max_overflow from Settings (I6)"
```

---

## Task 7: Bundle Tailwind CSS Locally (I5)

**Problem:** Admin dashboard loads Tailwind from CDN. CDN compromise = XSS in admin panel.

**Solution:** Use Tailwind CSS standalone CLI to generate a static CSS file, serve it locally.

**Files:**
- Create: `api/admin_ui/static/tailwind.min.css` — pre-built Tailwind CSS
- Modify: `api/admin_ui/templates/base.html` — reference local CSS instead of CDN
- Modify: `api/middleware.py:105-112` — remove CDN from CSP
- Modify: `api/main_v2.py` or `api/admin_ui/views.py` — mount static files
- Create: `tests/test_tailwind_local.py`

**Step 1: Write the failing test**

```python
# tests/test_tailwind_local.py
"""Tests that Tailwind is served locally, not from CDN."""
import pytest


def test_csp_does_not_allow_cdn():
    """CSP must not reference cdn.tailwindcss.com."""
    from api import middleware
    import inspect
    source = inspect.getsource(middleware.configure_middleware)
    assert "cdn.tailwindcss.com" not in source, (
        "CSP must not allow cdn.tailwindcss.com — bundle Tailwind locally"
    )


def test_base_template_no_cdn_reference():
    """Base template must not reference CDN Tailwind."""
    with open("api/admin_ui/templates/base.html") as f:
        content = f.read()
    assert "cdn.tailwindcss.com" not in content, (
        "Base template must use local Tailwind CSS, not CDN"
    )
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_tailwind_local.py -v`
Expected: FAIL

**Step 3: Download Tailwind CSS standalone**

Run: `curl -sL https://cdn.tailwindcss.com/3.4.1 -o api/admin_ui/static/tailwind.min.js`

Actually, for a CSS-only approach, use the Tailwind Play CDN output. Since the admin uses utility classes only, we can use a pre-built full Tailwind CSS:

Run: `curl -sL "https://unpkg.com/tailwindcss@3.4.1/src/css/preflight.css" -o /dev/null` — actually simplest approach: download the standalone Tailwind CSS build.

Better approach: since admin UI uses Tailwind as a runtime script, replace with a static CSS file containing all used utilities. Use the Tailwind standalone CLI:

```bash
npx tailwindcss -i /dev/null --content "api/admin_ui/templates/**/*.html" -o api/admin_ui/static/tailwind.css --minify
```

Or if npx is not available, use a pre-built CDN-extracted CSS (the full Tailwind CSS is ~300KB minified).

**Step 4: Mount static files and update templates**

In `api/admin_ui/views.py`, add static files mount:
```python
from fastapi.staticfiles import StaticFiles
# In setup_admin_ui():
app.mount("/admin/static", StaticFiles(directory="api/admin_ui/static"), name="admin_static")
```

In `api/admin_ui/templates/base.html`, replace CDN script tag:
```html
<!-- Replace: <script src="https://cdn.tailwindcss.com"></script> -->
<link rel="stylesheet" href="/admin/static/tailwind.css">
```

**Step 5: Update CSP in middleware.py**

Remove `https://cdn.tailwindcss.com` from `script-src`:
```python
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
```

**Step 6: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_tailwind_local.py -v`
Expected: PASS

**Step 7: Run full test suite**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/ -x -q`
Expected: All pass

**Step 8: Commit**

```bash
git add api/admin_ui/static/ api/admin_ui/templates/base.html api/admin_ui/views.py api/middleware.py tests/test_tailwind_local.py
git commit -m "fix: bundle Tailwind CSS locally, remove CDN from CSP (I5)"
```

---

## Task 8: Cap Alert List & Remove Stale Rate Limit Headers Docs (I8, M1)

**Problem:** (1) Alert list grows unbounded. (2) API docs promise rate limit headers that don't exist.

**Files:**
- Modify: `sthrip/services/monitoring.py:136` — cap `_alerts` list
- Modify: `api/docs.py` — remove rate limit header documentation
- Create: `tests/test_alert_cap.py`

**Step 1: Write the failing test**

```python
# tests/test_alert_cap.py
"""Tests that alert list has a maximum size."""
from sthrip.services.monitoring import HealthMonitor, AlertSeverity


def test_alerts_capped_at_max():
    """Alert list must not grow beyond max size."""
    monitor = HealthMonitor()
    max_alerts = 1000  # Expected cap
    for i in range(max_alerts + 100):
        monitor._create_alert(
            AlertSeverity.INFO, f"Test {i}", f"Message {i}", "test"
        )
    assert len(monitor._alerts) <= max_alerts
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_alert_cap.py -v`
Expected: FAIL — list grows to 1100

**Step 3: Add cap to `_create_alert`**

In `sthrip/services/monitoring.py`, in `_create_alert`:
```python
    _MAX_ALERTS = 1000

    def _create_alert(self, severity, title, message, source):
        ...
        self._alerts.append(alert)
        # Cap alert list to prevent unbounded growth
        if len(self._alerts) > self._MAX_ALERTS:
            self._alerts = self._alerts[-self._MAX_ALERTS:]
        ...
```

**Step 4: Remove false rate limit header docs from `api/docs.py`**

Find and remove the section documenting `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` headers.

**Step 5: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_alert_cap.py tests/ -x -q`
Expected: All pass

**Step 6: Commit**

```bash
git add sthrip/services/monitoring.py api/docs.py tests/test_alert_cap.py
git commit -m "fix: cap alert list at 1000 entries, remove undocumented rate limit headers (I8, M1)"
```

---

## Summary

| Task | Issue | Priority | Estimated Effort |
|------|-------|----------|-----------------|
| 1 | HMAC API key hashing | CRITICAL | 15 min |
| 2 | Withdrawal recovery | CRITICAL | 20 min |
| 3 | Unified auth session | CRITICAL | 15 min |
| 4 | Encrypted webhook secrets | CRITICAL | 20 min |
| 5 | Thread-safe singletons | IMPORTANT | 10 min |
| 6 | Configured pool sizes | IMPORTANT | 5 min |
| 7 | Local Tailwind CSS | IMPORTANT | 15 min |
| 8 | Alert cap + docs fix | MINOR | 10 min |

After completing all 8 tasks, run final verification:
```bash
cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/ -v --tb=short
```

Expected outcome: All tests pass, production readiness score rises from 6/10 to 8+/10.
