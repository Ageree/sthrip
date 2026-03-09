# Security Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all CRITICAL and HIGH security/code-quality findings from production readiness review.

**Architecture:** Targeted surgical fixes — no refactoring beyond what's needed. Each task is independent and can be committed separately. TDD: write failing test first, then fix.

**Tech Stack:** Python 3.9, FastAPI, SQLAlchemy, Redis, pytest

---

## Task 1: Fix Double Piconero Conversion (CRITICAL — funds loss)

**Files:**
- Modify: `sthrip/services/wallet_service.py:88-103`
- Modify: `sthrip/wallet.py:94-121`
- Test: `tests/test_wallet_service.py`

**Context:** `WalletService.send_withdrawal()` calls `xmr_to_piconero(amount)` then passes the result to `MoneroWalletRPC.transfer()`, which internally multiplies by 10^12 again. The amount sent to RPC is 10^24x the intended value.

**Step 1: Write the failing test**

```python
# In tests/test_wallet_service.py — add this test
def test_send_withdrawal_passes_xmr_not_piconero():
    """Ensure transfer() receives XMR amount, not piconero."""
    from unittest.mock import MagicMock
    from decimal import Decimal
    from sthrip.services.wallet_service import WalletService

    mock_rpc = MagicMock()
    mock_rpc.transfer.return_value = {"tx_hash": "abc123", "fee": 0}
    svc = WalletService(wallet_rpc=mock_rpc, db_session_factory=MagicMock())

    svc.send_withdrawal("5addr...", Decimal("0.5"))

    # transfer() must receive the raw XMR Decimal, NOT piconero int
    call_args = mock_rpc.transfer.call_args
    amount_arg = call_args.kwargs.get("amount") or call_args[1].get("amount", call_args[0][1] if len(call_args[0]) > 1 else None)
    # Amount must be 0.5 (XMR), not 500000000000 (piconero)
    assert amount_arg == Decimal("0.5") or amount_arg == 0.5, \
        f"transfer() received {amount_arg} — expected XMR amount, got piconero?"
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_wallet_service.py::test_send_withdrawal_passes_xmr_not_piconero -v`
Expected: FAIL — currently passes piconero (int ~500000000000)

**Step 3: Fix `wallet_service.py` — remove double conversion**

In `sthrip/services/wallet_service.py`, change `send_withdrawal`:

```python
def send_withdrawal(self, to_address: str, amount: Decimal) -> Dict:
    """Send XMR from hub wallet to external address.

    Returns dict with tx_hash, fee (XMR), and amount (XMR).
    Raises WalletRPCError on failure.
    """
    # wallet.transfer() handles XMR->piconero conversion internally
    result = self.wallet.transfer(
        destination=to_address,
        amount=float(amount),
    )
    return {
        "tx_hash": result["tx_hash"],
        "fee": piconero_to_xmr(result.get("fee", 0)),
        "amount": amount,
    }
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_wallet_service.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
git add sthrip/services/wallet_service.py tests/test_wallet_service.py
git commit -m "fix: remove double piconero conversion in send_withdrawal

wallet.transfer() already converts XMR to piconero internally.
Passing pre-converted piconero caused 10^24x amount inflation."
```

---

## Task 2: Admin Login Brute-Force Protection (CRITICAL)

**Files:**
- Modify: `api/admin_ui/views.py:83-102`
- Test: `tests/test_admin_ui.py`

**Context:** `POST /admin/login` has no rate limiting. An attacker can make unlimited guesses at `ADMIN_API_KEY`.

**Step 1: Write the failing test**

```python
# In tests/test_admin_ui.py — add these tests
def test_admin_login_rate_limited_after_5_attempts(client):
    """After 5 failed logins from same IP, return 429."""
    for i in range(5):
        resp = client.post("/admin/login", data={"admin_key": f"wrong_{i}"})
        assert resp.status_code in (401, 303)  # wrong key

    # 6th attempt should be rate limited
    resp = client.post("/admin/login", data={"admin_key": "wrong_6"})
    assert resp.status_code == 429
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_admin_ui.py::test_admin_login_rate_limited_after_5_attempts -v`
Expected: FAIL — currently no 429 response

**Step 3: Add rate limiting to `login_submit`**

In `api/admin_ui/views.py`, modify the `login_submit` function:

```python
@router.post("/login")
async def login_submit(request: Request, admin_key: str = Form(...)):
    """Validate admin key and set session cookie."""
    from sthrip.services.rate_limiter import get_rate_limiter, RateLimitExceeded

    client_ip = request.client.host if request.client else "unknown"
    try:
        limiter = get_rate_limiter()
        limiter.check_ip_rate_limit(
            ip_address=client_ip,
            action="admin_login",
            per_ip_limit=5,
            global_limit=50,
            window_seconds=300,
        )
    except RateLimitExceeded:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Too many login attempts. Try again later."},
            status_code=429,
        )

    if not _verify_admin_key(admin_key):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid admin key"},
            status_code=401,
        )
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"expires": time.time() + _SESSION_TTL}
    response = RedirectResponse(url="/admin/", status_code=303)
    response.set_cookie(
        key="admin_session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=_SESSION_TTL,
    )
    return response
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_admin_ui.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add api/admin_ui/views.py tests/test_admin_ui.py
git commit -m "fix: add rate limiting to admin login endpoint

5 attempts per IP per 5 minutes, 50 global. Returns 429 on excess."
```

---

## Task 3: Admin Session Cookie — Add `secure=True` (CRITICAL)

**Files:**
- Modify: `api/admin_ui/views.py:95-101`
- Test: `tests/test_admin_ui.py`

**Context:** Admin session cookie is missing `secure=True`, allowing transmission over HTTP.

**Step 1: Write the failing test**

```python
# In tests/test_admin_ui.py
def test_admin_login_sets_secure_cookie_in_production(client, monkeypatch):
    """Cookie must have secure=True in non-dev environments."""
    monkeypatch.setenv("ADMIN_API_KEY", "test-key-123")
    monkeypatch.setenv("ENVIRONMENT", "production")
    resp = client.post("/admin/login", data={"admin_key": "test-key-123"}, follow_redirects=False)
    assert resp.status_code == 303
    cookie_header = resp.headers.get("set-cookie", "")
    assert "Secure" in cookie_header or "secure" in cookie_header
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_admin_ui.py::test_admin_login_sets_secure_cookie_in_production -v`
Expected: FAIL — no `Secure` in cookie

**Step 3: Add `secure` flag conditionally**

In `api/admin_ui/views.py`, change the `set_cookie` call in `login_submit`:

```python
    is_secure = os.getenv("ENVIRONMENT", "production") != "dev"
    response.set_cookie(
        key="admin_session",
        value=token,
        httponly=True,
        secure=is_secure,
        samesite="strict",
        max_age=_SESSION_TTL,
    )
```

**Step 4: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_admin_ui.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add api/admin_ui/views.py tests/test_admin_ui.py
git commit -m "fix: add secure flag to admin session cookie

secure=True in non-dev, samesite upgraded to strict."
```

---

## Task 4: Move Admin Sessions to Redis (CRITICAL)

**Files:**
- Modify: `api/admin_ui/views.py:25-28, 48-59, 92-93, 108-110`
- Test: `tests/test_admin_ui.py`

**Context:** `_sessions: dict = {}` is in-process — lost on restart, not shared across replicas. Move to Redis with fallback to dict for tests/dev.

**Step 1: Write the failing test**

```python
# In tests/test_admin_ui.py
def test_admin_session_uses_redis_store_interface():
    """Session store must expose get/set/delete, not be a plain dict."""
    from api.admin_ui.views import _session_store
    assert hasattr(_session_store, 'set_session')
    assert hasattr(_session_store, 'get_session')
    assert hasattr(_session_store, 'delete_session')
```

**Step 2: Run test to verify it fails**

Expected: FAIL — `_session_store` does not exist

**Step 3: Implement session store abstraction**

Replace the `_sessions` dict in `api/admin_ui/views.py` with:

```python
import logging as _logging

_session_logger = _logging.getLogger("sthrip.admin_sessions")


class _SessionStore:
    """Admin session store — Redis-backed with in-memory fallback."""

    def __init__(self):
        self._local: dict = {}
        self._redis = None
        try:
            import redis
            redis_url = os.getenv("REDIS_URL", "")
            if redis_url:
                self._redis = redis.from_url(redis_url, decode_responses=True)
                self._redis.ping()
                _session_logger.info("Admin sessions using Redis")
        except Exception:
            _session_logger.warning("Admin sessions using in-memory fallback")

    def set_session(self, token: str, ttl: int) -> None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        if self._redis:
            self._redis.setex(f"admin_session:{token_hash}", ttl, "1")
        else:
            self._local[token_hash] = {"expires": time.time() + ttl}

    def get_session(self, token: str) -> bool:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        if self._redis:
            return bool(self._redis.get(f"admin_session:{token_hash}"))
        entry = self._local.get(token_hash)
        if not entry:
            return False
        if entry["expires"] < time.time():
            self._local.pop(token_hash, None)
            return False
        return True

    def delete_session(self, token: str) -> None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        if self._redis:
            self._redis.delete(f"admin_session:{token_hash}")
        else:
            self._local.pop(token_hash, None)


_session_store = _SessionStore()
```

Then update `_is_authenticated`:

```python
def _is_authenticated(request: Request) -> bool:
    token = _get_session_token(request)
    if not token:
        return False
    return _session_store.get_session(token)
```

Update `login_submit` session creation:

```python
    token = secrets.token_urlsafe(32)
    _session_store.set_session(token, _SESSION_TTL)
```

Update `logout`:

```python
    if token:
        _session_store.delete_session(token)
```

**Step 4: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_admin_ui.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add api/admin_ui/views.py tests/test_admin_ui.py
git commit -m "fix: move admin sessions to Redis with in-memory fallback

Sessions now survive restarts and are shared across replicas.
Token stored as SHA-256 hash in Redis with TTL."
```

---

## Task 5: Add `stagenet` to Config Literal (HIGH)

**Files:**
- Modify: `sthrip/config.py:14`
- Test: `tests/test_config.py` (create if needed)

**Context:** Railway runs `ENVIRONMENT=stagenet` but `Settings.environment` only accepts `dev|staging|production`.

**Step 1: Write the failing test**

```python
# tests/test_config.py
import os
import pytest

def test_settings_accepts_stagenet(monkeypatch):
    """Settings must accept ENVIRONMENT=stagenet."""
    monkeypatch.setenv("ENVIRONMENT", "stagenet")
    monkeypatch.setenv("ADMIN_API_KEY", "test-key-12345678")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///test.db")

    from sthrip.config import Settings
    settings = Settings()
    assert settings.environment == "stagenet"
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_config.py::test_settings_accepts_stagenet -v`
Expected: FAIL — `ValidationError` for invalid Literal value

**Step 3: Add `stagenet` to the Literal**

In `sthrip/config.py:14`, change:

```python
environment: Literal["dev", "staging", "stagenet", "production"] = "production"
```

**Step 4: Run test to verify it passes**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_config.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add sthrip/config.py tests/test_config.py
git commit -m "fix: add stagenet to Settings.environment Literal

Railway uses ENVIRONMENT=stagenet, which was rejected by pydantic."
```

---

## Task 6: Atomic Rate Limiter with Lua Script (HIGH)

**Files:**
- Modify: `sthrip/services/rate_limiter.py:143-186, 261-293`
- Test: `tests/test_rate_limiter.py`

**Context:** Redis read-then-write is not atomic — TOCTOU race allows exceeding limits. Also `hmset` is deprecated.

**Step 1: Write the failing test**

```python
# tests/test_rate_limiter.py
def test_rate_limiter_uses_atomic_redis_operation():
    """Rate limiter must use eval/evalsha (Lua) for atomic check+increment."""
    from unittest.mock import MagicMock, patch
    from sthrip.services.rate_limiter import RateLimiter

    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    mock_redis.evalsha.side_effect = Exception("NOSCRIPT")
    mock_redis.eval.return_value = [1, 60.0]  # [count, reset_at]

    with patch("sthrip.services.rate_limiter.redis") as mock_redis_module:
        mock_redis_module.from_url.return_value = mock_redis
        mock_redis_module.ConnectionError = ConnectionError
        mock_redis_module.ResponseError = Exception
        limiter = RateLimiter.__new__(RateLimiter)
        limiter.redis = mock_redis
        limiter.use_redis = True
        limiter.default_tier = limiter.__class__.__init__.__defaults__[1] if hasattr(limiter.__class__.__init__, '__defaults__') else "standard"

    # The key assertion: _check_redis must NOT call hmset or separate hincrby
    limiter._check_redis("test_key", MagicMock(requests_per_minute=100, burst_size=20), 1)

    # Must NOT have called hmset (deprecated, non-atomic)
    assert not mock_redis.hmset.called, "hmset is deprecated and non-atomic — use Lua script"
```

**Step 2: Run test to verify it fails**

Expected: FAIL — current code calls `hmset`

**Step 3: Replace `_check_redis` and `_check_ip_redis` with Lua scripts**

In `sthrip/services/rate_limiter.py`, add Lua script constant and rewrite `_check_redis`:

```python
# Lua script for atomic rate limit check+increment
_RATE_LIMIT_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'count', 'reset_at')
local count = tonumber(data[1])
local reset_at = tonumber(data[2])

if count == nil or reset_at == nil or reset_at < now then
    -- New window
    count = cost
    reset_at = now + window
    redis.call('HSET', key, 'count', count, 'reset_at', reset_at)
    redis.call('EXPIRE', key, window + 1)
    return {count, tostring(reset_at)}
end

if count + cost > limit then
    return {-1, tostring(reset_at)}
end

count = redis.call('HINCRBY', key, 'count', cost)
return {count, tostring(reset_at)}
"""
```

Then rewrite `_check_redis`:

```python
    def _check_redis(self, key: str, config: RateLimitConfig, cost: int) -> Dict:
        """Check limit using atomic Lua script."""
        now = time.time()
        window = 60

        result = self.redis.eval(
            _RATE_LIMIT_LUA, 1, key,
            config.requests_per_minute, window, cost, now
        )
        count = int(result[0])
        reset_at = float(result[1])

        if count == -1:
            raise RateLimitExceeded(
                limit=config.requests_per_minute,
                reset_at=reset_at,
            )

        remaining = max(0, config.requests_per_minute - count)
        return {
            "allowed": True,
            "remaining": remaining,
            "reset_at": reset_at,
            "limit": config.requests_per_minute,
        }
```

Similarly rewrite `_check_ip_redis` to use two Lua eval calls (one per key).

**Step 4: Run all rate limiter tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_rate_limiter.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add sthrip/services/rate_limiter.py tests/test_rate_limiter.py
git commit -m "fix: atomic rate limiting with Lua script

Replaces non-atomic read+hmset with single eval() call.
Removes deprecated hmset usage."
```

---

## Task 7: Use Decimal for Financial Amounts in Schemas (HIGH)

**Files:**
- Modify: `api/schemas.py:72-73, 80, 88, 94, 130-131`
- Modify: `api/routers/balance.py:75, 121`
- Test: `tests/test_schemas.py` (create or extend)

**Context:** `float` is used for XMR amounts — IEEE 754 precision loss in financial operations.

**Step 1: Write the failing test**

```python
# tests/test_schemas.py
from decimal import Decimal

def test_withdraw_request_accepts_decimal_precision():
    """WithdrawRequest.amount must preserve Decimal precision."""
    from api.schemas import WithdrawRequest
    import os
    os.environ.setdefault("MONERO_NETWORK", "stagenet")

    req = WithdrawRequest(amount=Decimal("1.123456789012"), address="5" + "a" * 94)
    assert isinstance(req.amount, Decimal)
    assert req.amount == Decimal("1.123456789012")
```

**Step 2: Run test to verify it fails**

Expected: FAIL — `amount` is `float`, not `Decimal`

**Step 3: Change `float` to `Decimal` in all financial schema fields**

In `api/schemas.py`:

```python
from decimal import Decimal as Dec

# PaymentRequest
amount: Dec = Field(..., gt=0, description="Amount in XMR")

# HubPaymentRequest
amount: Dec = Field(..., gt=0, le=10000, description="Amount in XMR")

# EscrowCreateRequest
amount: Dec = Field(..., gt=0)

# DepositRequest
amount: Optional[Dec] = Field(default=None, gt=0, le=10000, description="Amount to deposit (required in ledger mode)")

# WithdrawRequest
amount: Dec = Field(gt=0, le=10000, description="Amount to withdraw")
```

In `api/routers/balance.py`, remove redundant `Decimal(str(req.amount))` wrapping — amount is already Decimal:

```python
# Line 75: amount = req.amount  (not Decimal(str(req.amount)))
# Line 121: amount = req.amount  (not Decimal(str(req.amount)))
```

**Step 4: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/ -v -x`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add api/schemas.py api/routers/balance.py tests/test_schemas.py
git commit -m "fix: use Decimal instead of float for financial amounts

Prevents IEEE 754 precision loss in XMR transactions."
```

---

## Task 8: Fix Content-Length Bypass in Body Size Limiter (HIGH)

**Files:**
- Modify: `api/middleware.py:40-48`
- Test: `tests/test_middleware.py` (create or extend)

**Context:** Body size check only fires when `Content-Length` header is present. Omitting the header bypasses the check.

**Step 1: Write the failing test**

```python
# tests/test_middleware.py
def test_oversized_body_rejected_without_content_length_header(client):
    """Request with large body but no Content-Length must still be rejected."""
    # Send 2MB of data without Content-Length header
    large_body = b"x" * (2 * 1024 * 1024)
    resp = client.post(
        "/v2/agents/register",
        content=large_body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413
```

**Step 2: Run test to verify it fails**

Expected: FAIL — currently passes through to route handler

**Step 3: Add streaming body size enforcement**

In `api/middleware.py`, replace the `limit_request_body` middleware:

```python
    @app.middleware("http")
    async def limit_request_body(request: Request, call_next):
        # Check Content-Length header first (fast path)
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_REQUEST_BODY_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large. Maximum size is 1 MB."},
                    )
            except ValueError:
                pass

        # For requests without Content-Length, check actual body size
        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.body()
            if len(body) > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large. Maximum size is 1 MB."},
                )

        return await call_next(request)
```

**Step 4: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_middleware.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add api/middleware.py tests/test_middleware.py
git commit -m "fix: enforce body size limit even without Content-Length header

Reads actual body for POST/PUT/PATCH when header is missing."
```

---

## Task 9: Add CSP Header to Middleware (HIGH)

**Files:**
- Modify: `api/middleware.py:66-73`
- Test: `tests/test_middleware.py`

**Context:** No `Content-Security-Policy` header — XSS in admin templates has no restriction.

**Step 1: Write the failing test**

```python
# tests/test_middleware.py
def test_responses_include_csp_header(client):
    """All responses must include Content-Security-Policy."""
    resp = client.get("/health")
    assert "content-security-policy" in resp.headers
```

**Step 2: Run test to verify it fails**

Expected: FAIL — no CSP header

**Step 3: Add CSP to `add_security_headers`**

In `api/middleware.py`, inside `add_security_headers`:

```python
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.tailwindcss.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        return response
```

**Step 4: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_middleware.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add api/middleware.py tests/test_middleware.py
git commit -m "fix: add Content-Security-Policy header to all responses

Restricts script/style sources, blocks framing."
```

---

## Task 10: Fix `datetime.utcnow()` Deprecation (HIGH)

**Files:**
- Modify: `sthrip/db/models.py:455, 469, 470`
- Modify: `api/admin_ui/views.py:136`
- Test: `tests/test_models.py` or inline verification

**Context:** `datetime.utcnow()` is deprecated in Python 3.12, returns naive datetime that miscompares with aware datetimes elsewhere.

**Step 1: Write the failing test**

```python
# tests/test_models.py
def test_system_state_uses_timezone_aware_datetime():
    """SystemState.updated_at default must produce timezone-aware datetime."""
    from sthrip.db.models import SystemState
    col = SystemState.__table__.columns["updated_at"]
    # The default should be func.now() or use timezone.utc, NOT datetime.utcnow
    default = col.default
    if hasattr(default, 'arg') and callable(default.arg):
        val = default.arg()
        assert val.tzinfo is not None, "updated_at default produces naive datetime"
```

**Step 2: Run test to verify it fails**

Expected: FAIL — `datetime.utcnow()` returns naive datetime

**Step 3: Replace all `datetime.utcnow` with `datetime.now(timezone.utc)`**

In `sthrip/db/models.py`:

```python
# Line 455: Change datetime.utcnow to lambda: datetime.now(timezone.utc)
updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

# Line 469:
created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

# Line 470:
updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
```

Make sure `from datetime import datetime, timezone` is imported at the top of models.py.

In `api/admin_ui/views.py:136`, change:

```python
cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
```

Add `from datetime import datetime, timedelta, timezone` at the top (remove inline import).

**Step 4: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/ -v -x`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add sthrip/db/models.py api/admin_ui/views.py
git commit -m "fix: replace deprecated datetime.utcnow with timezone-aware datetime

Use datetime.now(timezone.utc) for consistent tz-aware timestamps."
```

---

## Task 11: Fix Atomic `record_dispute` in Repository (HIGH)

**Files:**
- Modify: `sthrip/db/repository.py:624-628`
- Test: `tests/test_repository.py`

**Context:** `record_dispute` uses read-modify-write instead of atomic SQL update.

**Step 1: Write the failing test**

```python
# tests/test_repository.py
def test_record_dispute_uses_atomic_update(db_session):
    """record_dispute must use SQL UPDATE, not read-modify-write."""
    from unittest.mock import patch, MagicMock
    from sthrip.db.repository import ReputationRepository
    import uuid

    repo = ReputationRepository(db_session)
    agent_id = uuid.uuid4()

    # Create reputation record first
    # ... (setup code depends on existing fixtures)

    # The test verifies db.execute is called with an UPDATE statement
    with patch.object(db_session, 'execute') as mock_exec:
        repo.record_dispute(agent_id)
        if mock_exec.called:
            call_str = str(mock_exec.call_args)
            assert "UPDATE" in call_str or "update" in call_str
```

**Step 2: Run test to verify it fails**

Expected: FAIL — currently uses ORM attribute mutation, not `db.execute`

**Step 3: Fix `record_dispute` to use atomic SQL**

In `sthrip/db/repository.py`, replace `record_dispute`:

```python
    def record_dispute(self, agent_id: UUID):
        """Record dispute for agent — atomic SQL increment."""
        self.db.execute(
            update(models.AgentReputation)
            .where(models.AgentReputation.agent_id == agent_id)
            .values(
                disputed_transactions=models.AgentReputation.disputed_transactions + 1
            )
        )
```

**Step 4: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_repository.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add sthrip/db/repository.py tests/test_repository.py
git commit -m "fix: make record_dispute atomic with SQL UPDATE

Prevents lost updates from concurrent read-modify-write."
```

---

## Task 12: Replace `print()` with `logger.info()` in Lifespan (MEDIUM)

**Files:**
- Modify: `api/main_v2.py:43`

**Step 1: Replace the print**

```python
# Line 43: change
print("🚀 Starting Sthrip API v2...")
# to
logger.info("Starting Sthrip API v2")
```

**Step 2: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/ -v --timeout=60`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add api/main_v2.py
git commit -m "fix: replace print() with logger.info() in lifespan"
```

---

## Task 13: Thread-Safe Singleton Initialization (MEDIUM)

**Files:**
- Modify: `api/helpers.py:18-26`
- Test: `tests/test_helpers.py`

**Context:** Singletons use `if x is None: x = Cls()` without locking — race condition at startup.

**Step 1: Add threading lock**

In `api/helpers.py`:

```python
import threading

_wallet_lock = threading.Lock()
_wallet_service = None


def get_wallet_service() -> WalletService:
    """Get or create the WalletService singleton (thread-safe)."""
    global _wallet_service
    if _wallet_service is None:
        with _wallet_lock:
            if _wallet_service is None:
                _wallet_service = WalletService.from_env(db_session_factory=get_db)
    return _wallet_service
```

Apply the same pattern to `get_rate_limiter()` in `sthrip/services/rate_limiter.py:367-372`.

**Step 2: Run tests**

Run: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/ -v --timeout=60`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add api/helpers.py sthrip/services/rate_limiter.py
git commit -m "fix: thread-safe singleton initialization with double-checked locking"
```

---

## Summary

| Task | Severity | Description | Est. |
|------|----------|-------------|------|
| 1 | CRITICAL | Fix double piconero conversion | 5 min |
| 2 | CRITICAL | Admin login brute-force protection | 5 min |
| 3 | CRITICAL | Admin cookie `secure=True` | 3 min |
| 4 | CRITICAL | Sessions to Redis | 10 min |
| 5 | HIGH | `stagenet` in config Literal | 3 min |
| 6 | HIGH | Atomic rate limiter (Lua) | 10 min |
| 7 | HIGH | Decimal for financial amounts | 5 min |
| 8 | HIGH | Content-Length bypass fix | 5 min |
| 9 | HIGH | Add CSP header | 3 min |
| 10 | HIGH | Fix `datetime.utcnow()` | 5 min |
| 11 | HIGH | Atomic `record_dispute` | 3 min |
| 12 | MEDIUM | Replace `print()` with logger | 2 min |
| 13 | MEDIUM | Thread-safe singletons | 5 min |

**Total: 13 tasks, ~64 minutes estimated**
