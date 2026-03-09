"""Authentication dependencies and DI providers for the Sthrip API."""

import hmac
import logging
import os
import time as _time
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.db.repository import AgentRepository, BalanceRepository, TransactionRepository
from sthrip.services.rate_limiter import get_rate_limiter, RateLimitExceeded
from sthrip.services.audit_logger import log_event as audit_log

logger = logging.getLogger("sthrip")

security = HTTPBearer(auto_error=False)


# ═══════════════════════════════════════════════════════════════════════════════
# DI PROVIDERS (Task 14)
# ═══════════════════════════════════════════════════════════════════════════════

def get_db_session():
    """Yield a DB session for FastAPI Depends()."""
    with get_db() as db:
        yield db


def get_balance_repo(db: Session = Depends(get_db_session)) -> BalanceRepository:
    """Provide a BalanceRepository bound to the current session."""
    return BalanceRepository(db)


def get_transaction_repo(db: Session = Depends(get_db_session)) -> TransactionRepository:
    """Provide a TransactionRepository bound to the current session."""
    return TransactionRepository(db)


# ═══════════════════════════════════════════════════════════════════════════════
# AUTHENTICATION
# ═══════════════════════════════════════════════════════════════════════════════

_FAILED_AUTH_LIMIT = 20
_FAILED_AUTH_WINDOW = 60  # seconds


async def get_current_agent(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    request: Request = None
) -> Agent:
    """Authenticate agent and check rate limits"""
    client_ip = request.client.host if request and request.client else "unknown"
    limiter = get_rate_limiter()

    # Check failed auth limit (read-only check, no increment)
    _check_failed_auth_limit(limiter, client_ip)

    if not credentials:
        _record_failed_auth(limiter, client_ip)
        audit_log("auth.failed", ip_address=client_ip, details={"reason": "missing_api_key"}, success=False)
        raise HTTPException(status_code=401, detail="Missing API key")

    api_key = credentials.credentials

    with get_db() as db:
        repo = AgentRepository(db)
        agent = repo.get_by_api_key(api_key)

        if not agent:
            _record_failed_auth(limiter, client_ip)
            audit_log("auth.failed", ip_address=client_ip, details={"reason": "invalid_api_key"}, success=False)
            raise HTTPException(status_code=401, detail="Invalid API key")

        if not agent.is_active:
            raise HTTPException(status_code=403, detail="Agent account disabled")

        # Update last seen
        repo.update_last_seen(agent.id)

        # Check per-agent rate limit
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


def _check_failed_auth_limit(limiter, ip: str) -> None:
    """Check if IP has exceeded failed auth limit (read-only, no increment)."""
    key = f"ratelimit:ip:failed_auth:{ip}"
    now = _time.time()

    if limiter.use_redis:
        data = limiter.redis.hmget(key, "count", "reset_at")
        count = int(data[0]) if data[0] else 0
        reset_at = float(data[1]) if data[1] else now + _FAILED_AUTH_WINDOW
        if reset_at < now:
            count = 0
    else:
        with limiter._cache_lock:
            entry = limiter._local_cache.get(key)
        if entry and entry["reset_at"] >= now:
            count = entry["count"]
            reset_at = entry["reset_at"]
        else:
            count = 0
            reset_at = now + _FAILED_AUTH_WINDOW

    if count >= _FAILED_AUTH_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="Too many authentication attempts",
        )


def _record_failed_auth(limiter, ip: str) -> None:
    """Increment failed auth counter for this IP."""
    try:
        limiter.check_ip_rate_limit(
            ip_address=ip,
            action="failed_auth",
            per_ip_limit=_FAILED_AUTH_LIMIT,
            global_limit=10000,
            window_seconds=_FAILED_AUTH_WINDOW,
        )
    except RateLimitExceeded:
        pass  # Already exceeded, will block next request via _check_failed_auth_limit


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN AUTH
# ═══════════════════════════════════════════════════════════════════════════════

def verify_admin_key(admin_key: Optional[str]) -> None:
    """Verify admin key using constant-time comparison."""
    expected_key = os.getenv("ADMIN_API_KEY")
    if not expected_key or not admin_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")
    if not hmac.compare_digest(admin_key.encode(), expected_key.encode()):
        raise HTTPException(status_code=401, detail="Invalid admin key")
