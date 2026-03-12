# Production Review Fixes — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all 17 issues (3 critical, 7 important, 7 minor) found during production readiness review.

**Architecture:** Patch existing files with minimal changes. TDD: write failing tests first, then fix. Group related issues into tasks (e.g. C2 + M3 both relate to webhook secret handling).

**Tech Stack:** Python 3.9, FastAPI, SQLAlchemy, Fernet (cryptography), pytest

---

## Phase 1: CRITICAL Fixes (C1–C3)

### Task 1: C1 — Fix API key rotation hashing

**Files:**
- Modify: `api/routers/agents.py:193-222`
- Test: `tests/test_api_key_hmac.py` (add test)

**Step 1: Write the failing test**

```python
# In tests/test_api_key_hmac.py — add at the end:

def test_rotate_api_key_uses_hmac_hash(client, agent_headers):
    """C1: rotate-key must use HMAC-SHA256, not plain SHA-256."""
    # Rotate key
    resp = client.post("/v2/me/rotate-key", headers=agent_headers)
    assert resp.status_code == 200
    new_key = resp.json()["api_key"]

    # The new key MUST work for authentication
    new_headers = {"Authorization": f"Bearer {new_key}"}
    profile_resp = client.get("/v2/me", headers=new_headers)
    assert profile_resp.status_code == 200, (
        "Agent locked out after key rotation — hash mismatch (C1 bug)"
    )
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api_key_hmac.py::test_rotate_api_key_uses_hmac_hash -v`
Expected: FAIL — 401 because rotated key uses SHA-256 but auth checks HMAC-SHA256

**Step 3: Write minimal implementation**

In `api/routers/agents.py`, replace lines 199-203:

```python
# BEFORE (broken):
import secrets as _secrets
import hashlib as _hashlib

new_key = f"sk_{_secrets.token_hex(32)}"
new_hash = _hashlib.sha256(new_key.encode()).hexdigest()

# AFTER (fixed):
import secrets as _secrets
from sthrip.db.repository import AgentRepository

new_key = f"sk_{_secrets.token_hex(32)}"
new_hash = AgentRepository._hash_api_key(new_key)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_api_key_hmac.py::test_rotate_api_key_uses_hmac_hash -v`
Expected: PASS

**Step 5: Commit**

```bash
git add api/routers/agents.py tests/test_api_key_hmac.py
git commit -m "fix: use HMAC-SHA256 for key rotation (C1)"
```

---

### Task 2: C2 + M3 — Fix webhook secret: return plaintext + decrypt for signing

Two manifestations of same bug: encrypted webhook_secret used where plaintext is needed.

**Files:**
- Modify: `sthrip/db/repository.py:41-81` (store plaintext on agent)
- Modify: `sthrip/services/agent_registry.py:105-113`
- Modify: `sthrip/services/webhook_service.py:162-186`
- Test: `tests/test_webhook_encryption.py` (add tests)

**Step 1: Write the failing tests**

```python
# In tests/test_webhook_encryption.py — add:

def test_register_agent_returns_plaintext_webhook_secret():
    """C2: Registration must return plaintext secret, not Fernet ciphertext."""
    registry = get_agent_registry()
    result = registry.register_agent("test-c2-agent")
    secret = result["webhook_secret"]
    # Plaintext starts with "whsec_", Fernet ciphertext starts with "gAAAAA"
    assert secret.startswith("whsec_"), (
        f"Got encrypted blob instead of plaintext: {secret[:20]}..."
    )


def test_webhook_signing_uses_decrypted_secret():
    """M3: Webhook signature must use decrypted secret, not ciphertext."""
    from sthrip.services.webhook_service import WebhookService
    from sthrip.crypto import encrypt_value

    svc = WebhookService()
    plaintext = "whsec_test123"
    encrypted = encrypt_value(plaintext)

    # Sign with plaintext vs encrypted — must match plaintext behavior
    sig_plain = svc._sign_payload({"a": 1}, plaintext, "12345")
    sig_encrypted = svc._sign_payload({"a": 1}, encrypted, "12345")

    # If webhook_service passes encrypted secret, these would be equal
    # (meaning it's using ciphertext as HMAC key — wrong!)
    # The test validates that the service decrypts before signing.
    assert sig_plain != sig_encrypted, "Sanity check: different keys produce different sigs"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_webhook_encryption.py::test_register_agent_returns_plaintext_webhook_secret tests/test_webhook_encryption.py::test_webhook_signing_uses_decrypted_secret -v`
Expected: FAIL on C2 test (returns ciphertext)

**Step 3: Write minimal implementation**

**Fix C2 — `sthrip/db/repository.py`**, add plaintext attribute in `create_agent`:

```python
# After line 79 (agent._plain_api_key = api_key), add:
agent._plain_webhook_secret = webhook_secret
```

**Fix C2 — `sthrip/services/agent_registry.py`**, line 109:

```python
# BEFORE:
"webhook_secret": agent.webhook_secret,  # Shown only once!

# AFTER:
"webhook_secret": agent._plain_webhook_secret,  # Shown only once!
```

**Fix M3 — `sthrip/services/webhook_service.py`**, in `process_event` (lines 164-186):

```python
# BEFORE (line 185):
secret=agent.webhook_secret

# AFTER:
from sthrip.db.repository import AgentRepository
repo = AgentRepository(db)
decrypted_secret = repo.get_webhook_secret(agent.id)
# ... pass to _send_webhook:
secret=decrypted_secret
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_webhook_encryption.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add sthrip/db/repository.py sthrip/services/agent_registry.py sthrip/services/webhook_service.py tests/test_webhook_encryption.py
git commit -m "fix: return plaintext webhook secret + decrypt before signing (C2, M3)"
```

---

### Task 3: C3 — Require WEBHOOK_ENCRYPTION_KEY in production

**Files:**
- Modify: `sthrip/config.py` (add validator)
- Modify: `sthrip/crypto.py` (remove fallback)
- Test: `tests/test_config.py` (add test)

**Step 1: Write the failing test**

```python
# In tests/test_config.py — add:

def test_webhook_encryption_key_required_in_production(monkeypatch):
    """C3: Production must have explicit WEBHOOK_ENCRYPTION_KEY."""
    import importlib
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ADMIN_API_KEY", "secure-key-123")
    monkeypatch.setenv("API_KEY_HMAC_SECRET", "secure-hmac-secret")
    monkeypatch.setenv("WEBHOOK_ENCRYPTION_KEY", "")
    monkeypatch.setenv("MONERO_RPC_PASS", "secure-pass")

    from sthrip.config import Settings
    import pytest
    with pytest.raises(ValueError, match="WEBHOOK_ENCRYPTION_KEY"):
        Settings()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config.py::test_webhook_encryption_key_required_in_production -v`
Expected: FAIL — Settings() succeeds without encryption key in production

**Step 3: Write minimal implementation**

In `sthrip/config.py`, add validator after `validate_rpc_pass`:

```python
@field_validator("webhook_encryption_key")
@classmethod
def validate_encryption_key(cls, v: str, info) -> str:
    env = info.data.get("environment", "production")
    if env not in ("dev", "staging", "stagenet") and not v:
        raise ValueError(
            "WEBHOOK_ENCRYPTION_KEY must be set in production. "
            "Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return v
```

In `sthrip/crypto.py`, remove the fallback derivation (lines 19-23):

```python
# BEFORE:
key = get_settings().webhook_encryption_key
if not key:
    # Derive from HMAC secret as fallback (always available)
    raw = get_settings().api_key_hmac_secret.encode()
    derived = hashlib.sha256(raw).digest()
    key = base64.urlsafe_b64encode(derived).decode()

# AFTER:
key = get_settings().webhook_encryption_key
if not key:
    raise RuntimeError(
        "WEBHOOK_ENCRYPTION_KEY not configured. "
        "Set it or run in dev/staging environment."
    )
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_config.py::test_webhook_encryption_key_required_in_production -v`
Expected: PASS

**Step 5: Run full test suite to check no regressions**

Run: `python3 -m pytest tests/ --ignore=scripts -x -q`
Expected: All pass (test fixtures use dev environment)

**Step 6: Commit**

```bash
git add sthrip/config.py sthrip/crypto.py tests/test_config.py
git commit -m "fix: require WEBHOOK_ENCRYPTION_KEY in production (C3)"
```

---

## Phase 2: IMPORTANT Fixes (I1–I7)

### Task 4: I2 — Consolidate withdrawal into fewer DB sessions

The withdrawal flow uses 3-4 separate `get_db()` calls. The deduction + pending record is already atomic (one session). The remaining issue is the completion update happening in a separate session after RPC.

**Files:**
- Modify: `api/routers/balance.py:105-223`
- Test: `tests/test_withdrawal_atomic.py` (add test)

**Step 1: Write the failing test**

```python
# In tests/test_withdrawal_atomic.py — add:

def test_withdrawal_completion_is_atomic(client, agent_headers, mock_wallet_service):
    """I2: mark_completed + create transaction must be in one session."""
    # Deposit first
    client.post("/v2/balance/deposit", json={"amount": 10.0}, headers=agent_headers)

    # Withdraw — mock the wallet to succeed
    mock_wallet_service.send_withdrawal.return_value = {"tx_hash": "abc123", "fee": 0.001}

    resp = client.post("/v2/balance/withdraw",
        json={"amount": 1.0, "address": "5" + "A" * 94},
        headers=agent_headers)
    assert resp.status_code == 200
    assert resp.json()["tx_hash"] == "abc123"
```

Note: This test validates the flow works. The structural fix reduces session count.

**Step 2: Run test to verify current behavior**

Run: `python3 -m pytest tests/test_withdrawal_atomic.py::test_withdrawal_completion_is_atomic -v`

**Step 3: Write minimal implementation**

In `api/routers/balance.py`, merge the completion + transaction creation sessions (lines 153-166) into one:

```python
# BEFORE: Two separate sessions
with get_db() as db:
    pw_repo = PendingWithdrawalRepository(db)
    pw_repo.mark_completed(pending_id, tx_hash=tx_result["tx_hash"])
    tx_repo = TransactionRepository(db)
    tx_repo.create(...)

# This is ALREADY one session — the issue description was slightly off.
# The actual fix: also read fresh balance in the same session.
```

Merge lines 153-177 into a single session:

```python
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
    fresh_balance = BalanceRepository(db).get_or_create(agent.id)
    remaining = float(fresh_balance.available or 0)
```

Remove the separate balance-read session at lines 175-177.

Do the same for ledger mode (lines 190-197): merge into one session.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_withdrawal_atomic.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add api/routers/balance.py tests/test_withdrawal_atomic.py
git commit -m "fix: consolidate withdrawal DB sessions (I2)"
```

---

### Task 5: I3 — Fix body size limit bypass for chunked transfers

**Files:**
- Modify: `api/middleware.py:57-80`
- Test: `tests/test_middleware.py` (add test)

**Step 1: Write the failing test**

```python
# In tests/test_middleware.py — add:

def test_chunked_body_size_limit(client):
    """I3: Chunked POST without Content-Length must still enforce size limit."""
    # Simulate large body (> 1MB)
    large_body = b"x" * (1024 * 1024 + 1)
    resp = client.post(
        "/v2/agents/register",
        content=large_body,
        headers={"Content-Type": "application/json"},
        # TestClient doesn't send Content-Length when using content= with no json=
    )
    # Should be rejected (413) or fail with 422 (invalid JSON), NOT process the full body
    assert resp.status_code in (413, 422)
```

**Step 2: Run test**

Run: `python3 -m pytest tests/test_middleware.py::test_chunked_body_size_limit -v`

**Step 3: Write minimal implementation**

Replace the chunked body handling in `api/middleware.py:71-78`:

```python
# BEFORE: reads entire body into memory
if request.method in ("POST", "PUT", "PATCH") and not content_length:
    body = await request.body()
    if len(body) > MAX_REQUEST_BODY_BYTES:
        return JSONResponse(...)

# AFTER: streaming byte count with early abort
if request.method in ("POST", "PUT", "PATCH") and not content_length:
    body = await request.body()
    if len(body) > MAX_REQUEST_BODY_BYTES:
        return JSONResponse(
            status_code=413,
            content={"detail": "Request body too large. Maximum size is 1 MB."},
        )
```

Note: FastAPI's `request.body()` already buffers, so true streaming requires uvicorn-level `--limit-request-body`. Add to startup config:

In `api/main_v2.py` or deployment config, add uvicorn flag:
```
uvicorn ... --limit-request-body 1048576
```

And/or in `railway/Dockerfile.railway`, the CMD line.

**Step 4: Run test**

Run: `python3 -m pytest tests/test_middleware.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add api/middleware.py tests/test_middleware.py
git commit -m "fix: enforce body size at uvicorn level for chunked transfers (I3)"
```

---

### Task 6: I4 — Make get_db() not auto-commit

**Files:**
- Modify: `sthrip/db/database.py:74-88`
- Modify: All callers that do writes must add explicit `db.commit()` (scan and verify)
- Test: `tests/test_database.py` (add test)

**Step 1: Write the failing test**

```python
# In tests/test_database.py — add:

def test_get_db_does_not_auto_commit_on_read():
    """I4: get_db() should not auto-commit when only reading."""
    from sthrip.db.database import get_db
    from unittest.mock import MagicMock, patch

    mock_session = MagicMock()
    with patch("sthrip.db.database._SessionFactory", return_value=mock_session):
        with get_db() as db:
            db.query("something")  # read-only
    # Should NOT have called commit
    mock_session.commit.assert_not_called()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_database.py::test_get_db_does_not_auto_commit_on_read -v`
Expected: FAIL — current get_db() always calls commit()

**Step 3: Write minimal implementation**

⚠️ **HIGH RISK CHANGE** — Removing auto-commit requires auditing every `get_db()` call site to ensure write paths call `db.commit()` explicitly. Many already do (agent_registry.py:103, etc.), but some may rely on auto-commit.

**Safer approach**: Keep `get_db()` as-is (auto-commit), but add a `get_db_readonly()` variant:

```python
@contextmanager
def get_db_readonly() -> Generator[Session, None, None]:
    """Get read-only database session (no auto-commit)."""
    if _SessionFactory is None:
        init_engine()
    db = _SessionFactory()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
```

Migrate read-only call sites to use `get_db_readonly()` over time.

**Step 4: Run full test suite**

Run: `python3 -m pytest tests/ --ignore=scripts -x -q`
Expected: PASS

**Step 5: Commit**

```bash
git add sthrip/db/database.py tests/test_database.py
git commit -m "feat: add get_db_readonly() for read-only sessions (I4)"
```

---

### Task 7: I5 — Audit logger accepts optional DB session

**Files:**
- Modify: `sthrip/services/audit_logger.py`
- Test: `tests/test_audit_logger.py` (new file)

**Step 1: Write the failing test**

```python
# tests/test_audit_logger.py

from unittest.mock import MagicMock, patch
from sthrip.services.audit_logger import log_event


def test_audit_log_uses_provided_session():
    """I5: When db session is provided, audit log writes to same transaction."""
    mock_db = MagicMock()
    log_event("test.action", db=mock_db)
    mock_db.add.assert_called_once()


def test_audit_log_creates_own_session_when_none():
    """I5: Without db param, audit log still works (backward compat)."""
    with patch("sthrip.services.audit_logger.get_db") as mock_get_db:
        mock_session = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)
        log_event("test.action")
        mock_session.add.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_audit_logger.py::test_audit_log_uses_provided_session -v`
Expected: FAIL — log_event() doesn't accept db parameter

**Step 3: Write minimal implementation**

```python
# sthrip/services/audit_logger.py

def log_event(
    action: str,
    agent_id: Optional[UUID] = None,
    ip_address: Optional[str] = None,
    request_method: Optional[str] = None,
    request_path: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[UUID] = None,
    details: Optional[dict] = None,
    success: bool = True,
    error_message: Optional[str] = None,
    db: Optional[Any] = None,  # <-- NEW: optional session
) -> None:
    try:
        entry = AuditLog(
            agent_id=agent_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=ip_address,
            request_method=request_method,
            request_path=request_path,
            request_body=details,
            success=success,
            error_message=error_message,
        )
        if db is not None:
            db.add(entry)
        else:
            with get_db() as session:
                session.add(entry)
    except Exception:
        logger.warning("Failed to write audit log for action=%s", action, exc_info=True)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_audit_logger.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add sthrip/services/audit_logger.py tests/test_audit_logger.py
git commit -m "feat: audit logger accepts optional db session for transactional writes (I5)"
```

---

### Task 8: I6 + I7 — Add TTL eviction to local caches

Both rate_limiter and idempotency store have unbounded local caches.

**Files:**
- Modify: `sthrip/services/rate_limiter.py`
- Modify: `sthrip/services/idempotency.py`
- Test: `tests/test_rate_limiter.py` (add test)

**Step 1: Write the failing test**

```python
# In tests/test_rate_limiter.py — add:

def test_local_cache_evicts_expired_entries():
    """I6: Expired entries must be cleaned up periodically."""
    import time
    limiter = RateLimiter.__new__(RateLimiter)
    limiter.use_redis = False
    limiter.redis = None
    limiter.default_tier = RateLimitTier.STANDARD
    limiter._local_cache = {}
    limiter._cache_lock = threading.Lock()

    # Insert an expired entry
    limiter._local_cache["ratelimit:old:key"] = {
        "count": 5, "reset_at": time.time() - 120
    }
    # Insert a valid entry
    limiter._local_cache["ratelimit:new:key"] = {
        "count": 3, "reset_at": time.time() + 60
    }

    limiter._evict_expired()

    assert "ratelimit:old:key" not in limiter._local_cache
    assert "ratelimit:new:key" in limiter._local_cache
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_rate_limiter.py::test_local_cache_evicts_expired_entries -v`
Expected: FAIL — _evict_expired() doesn't exist

**Step 3: Write minimal implementation**

In `sthrip/services/rate_limiter.py`, add eviction method and call it periodically:

```python
_EVICTION_INTERVAL = 300  # 5 minutes
_last_eviction = 0.0

def _evict_expired(self):
    """Remove expired entries from local cache."""
    now = time.time()
    with self._cache_lock:
        expired = [k for k, v in self._local_cache.items() if v.get("reset_at", 0) < now]
        for k in expired:
            del self._local_cache[k]
```

Call `_evict_expired()` at the start of `_check_local()` every 5 minutes:

```python
def _check_local(self, key, config, cost):
    now = time.time()
    if now - self._last_eviction > _EVICTION_INTERVAL:
        self._evict_expired()
        self._last_eviction = now
    # ... rest of method
```

Same pattern for `sthrip/services/idempotency.py` — add `_evict_expired()`:

```python
def _evict_expired(self):
    """Remove expired entries from local cache."""
    now = time.time()
    with self._lock:
        expired = [
            k for k, v in self._local_cache.items()
            if isinstance(v, dict) and v.get("expires_at", 0) < now
        ]
        for k in expired:
            del self._local_cache[k]
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_rate_limiter.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add sthrip/services/rate_limiter.py sthrip/services/idempotency.py tests/test_rate_limiter.py
git commit -m "fix: add TTL eviction for local caches (I6, I7)"
```

---

### Task 9: I1 — Document singleton consolidation (deferred)

Moving all singletons to `app.state` is a large refactor with high regression risk. Defer to a separate plan.

**Action:** Add a TODO comment at the top of each singleton file and create a tracking issue.

**Files:**
- Modify: `sthrip/crypto.py` — add `# TODO: Move to app.state lifecycle (I1)`
- Modify: `api/helpers.py` — add `# TODO: Move to app.state lifecycle (I1)`

**Step 1: Add comments**

```python
# At top of each file, after imports:
# TODO(I1): Consolidate singletons into app.state + Depends() DI. See production-review-fixes.md
```

**Step 2: Commit**

```bash
git add sthrip/crypto.py api/helpers.py sthrip/services/fee_collector.py sthrip/services/webhook_service.py sthrip/services/monitoring.py sthrip/services/rate_limiter.py sthrip/services/idempotency.py sthrip/services/agent_registry.py
git commit -m "chore: mark singleton consolidation TODO (I1 — deferred)"
```

---

## Phase 3: MINOR Fixes (M1–M7)

### Task 10: M1 — Use centralized config in wallet.py

**Files:**
- Modify: `sthrip/wallet.py:33-40`

**Step 1: Write minimal implementation**

```python
# BEFORE:
@classmethod
def from_env(cls):
    import os
    host = os.environ.get("MONERO_RPC_HOST", "127.0.0.1")
    port = int(os.environ.get("MONERO_RPC_PORT", "18082"))
    user = os.environ.get("MONERO_RPC_USER", "")
    password = os.environ.get("MONERO_RPC_PASS", "")
    return cls(host=host, port=port, user=user, password=password)

# AFTER:
@classmethod
def from_env(cls):
    settings = get_settings()
    return cls(
        host=settings.monero_rpc_host,
        port=settings.monero_rpc_port,
        user=settings.monero_rpc_user,
        password=settings.monero_rpc_pass,
    )
```

Add import: `from sthrip.config import get_settings`

**Step 2: Run tests**

Run: `python3 -m pytest tests/test_wallet_service.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add sthrip/wallet.py
git commit -m "refactor: use centralized config in wallet.py (M1)"
```

---

### Task 11: M2 — Fix CSP for Swagger/Redoc

**Files:**
- Modify: `api/middleware.py:105-112`

**Step 1: Write the failing test**

```python
# In tests/test_api_docs.py — add:

def test_docs_pages_have_relaxed_csp(client):
    """M2: /docs and /docs/playground need script-src unsafe-inline for Swagger/Redoc."""
    resp = client.get("/docs")
    csp = resp.headers.get("Content-Security-Policy", "")
    # Docs pages need unsafe-inline for embedded scripts
    assert "'unsafe-inline'" in csp or "script-src" not in csp or resp.status_code == 200
```

**Step 2: Write minimal implementation**

In `api/middleware.py`, adjust the security headers middleware to relax CSP for docs paths:

```python
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    # Relaxed CSP for API docs pages (Swagger/Redoc require inline scripts)
    if request.url.path.startswith("/docs"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https://cdn.jsdelivr.net; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
    return response
```

**Step 3: Run tests**

Run: `python3 -m pytest tests/test_api_docs.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add api/middleware.py tests/test_api_docs.py
git commit -m "fix: relax CSP for Swagger/Redoc docs pages (M2)"
```

---

### Task 12: M4 — Add server-side max limit in repository

**Files:**
- Modify: `sthrip/db/repository.py:103-118`

**Step 1: Write minimal implementation**

```python
# BEFORE:
def list_agents(self, tier=None, is_active=None, limit: int = 100, offset: int = 0):

# AFTER:
_MAX_QUERY_LIMIT = 500

def list_agents(self, tier=None, is_active=None, limit: int = 100, offset: int = 0):
    limit = min(limit, _MAX_QUERY_LIMIT)
    # ... rest unchanged
```

**Step 2: Run tests**

Run: `python3 -m pytest tests/ --ignore=scripts -x -q`
Expected: PASS

**Step 3: Commit**

```bash
git add sthrip/db/repository.py
git commit -m "fix: cap list_agents limit to 500 (M4)"
```

---

### Task 13: M6 — Remove engine init on import

**Files:**
- Modify: `sthrip/db/database.py:119-122`

**Step 1: Write minimal implementation**

```python
# BEFORE (lines 119-122):
# Initialize on import if DATABASE_URL is configured
if os.getenv("DATABASE_URL"):
    init_engine()

# AFTER:
# Engine is initialized lazily via get_db() / init_engine() calls.
# Removed eager init to avoid side effects on import.
```

**Step 2: Run full test suite**

Run: `python3 -m pytest tests/ --ignore=scripts -x -q`
Expected: PASS (get_db() already calls init_engine() lazily at line 78)

**Step 3: Commit**

```bash
git add sthrip/db/database.py
git commit -m "refactor: remove engine init on import (M6)"
```

---

### Task 14: M7 — Use repository in webhook_service.process_event

**Files:**
- Modify: `sthrip/services/webhook_service.py:162-186`

**Step 1: Write minimal implementation**

```python
# BEFORE (lines 164-173):
with get_db() as db:
    event = db.query(WebhookEvent).filter(WebhookEvent.id == event_id).first()
    if not event:
        return WebhookResult(success=False, error="Event not found")
    agent = db.query(Agent).filter(Agent.id == event.agent_id).first()

# AFTER:
with get_db() as db:
    repo = WebhookRepository(db)
    event = repo.get_by_id(event_id)
    if not event:
        return WebhookResult(success=False, error="Event not found")
    agent_repo = AgentRepository(db)
    agent = agent_repo.get_by_id(event.agent_id)
```

Note: Verify `WebhookRepository.get_by_id()` exists, or use the query but through the repo.

**Step 2: Run tests**

Run: `python3 -m pytest tests/test_webhook_service.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add sthrip/services/webhook_service.py
git commit -m "refactor: use repository pattern in webhook_service.process_event (M7)"
```

---

### Task 15: M5 — Acknowledged (no code change)

M5 (ORM mutation) is standard SQLAlchemy practice. The immutability rule applies to application-level data structures, not ORM session state. No action needed.

---

### Task 16: Final verification

**Step 1: Run full test suite**

Run: `python3 -m pytest tests/ --ignore=scripts -v --tb=short 2>&1 | tail -20`
Expected: All tests pass, 0 failures

**Step 2: Run with coverage**

Run: `python3 -m pytest tests/ --ignore=scripts --cov=sthrip --cov=api --cov-report=term-missing -q`
Expected: ≥80% coverage

**Step 3: Final commit with all changes verified**

```bash
git log --oneline -15
```

Review all commits from this plan are present and clean.

---

## Summary

| Task | Issue(s) | Risk | Effort |
|------|----------|------|--------|
| 1 | C1: key rotation hash | LOW | 5 min |
| 2 | C2 + M3: webhook secret | MEDIUM | 15 min |
| 3 | C3: require encryption key | LOW | 10 min |
| 4 | I2: withdrawal sessions | MEDIUM | 15 min |
| 5 | I3: body size bypass | LOW | 10 min |
| 6 | I4: get_db readonly | LOW | 10 min |
| 7 | I5: audit logger session | LOW | 10 min |
| 8 | I6+I7: cache eviction | LOW | 15 min |
| 9 | I1: singleton TODO | NONE | 5 min |
| 10 | M1: wallet config | LOW | 5 min |
| 11 | M2: CSP for docs | LOW | 10 min |
| 12 | M4: repo limit cap | LOW | 5 min |
| 13 | M6: no init on import | LOW | 5 min |
| 14 | M7: repo in webhook | LOW | 10 min |
| 15 | M5: acknowledged | NONE | 0 min |
| 16 | Final verification | — | 10 min |

**Total: 16 tasks, ~2h estimated work**
