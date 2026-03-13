"""Authentication dependencies and DI providers for the Sthrip API."""

import hmac
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from sthrip.db.database import get_db
from api.helpers import get_client_ip
from sthrip.db.models import Agent
from sthrip.db.repository import AgentRepository, BalanceRepository, TransactionRepository
from sthrip.services.rate_limiter import get_rate_limiter, RateLimitExceeded
from sthrip.config import get_settings
from sthrip.services.audit_logger import log_event as audit_log
from api.session_store import AdminSessionStore

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
# APP.STATE DI PROVIDERS (I1 consolidation)
#
# These providers pull service instances from request.app.state, which is
# populated once during the FastAPI lifespan (see api/main_v2.py).
#
# IMPORTANT: The module-level get_*() functions in each service module are
# kept as-is for backward compatibility with background tasks and CLI code
# that operates outside of a FastAPI request context.
# ═══════════════════════════════════════════════════════════════════════════════

def get_rate_limiter_dep(request: Request):
    """Return the RateLimiter stored on app.state (populated during lifespan)."""
    return request.app.state.rate_limiter


def get_monitor_dep(request: Request):
    """Return the HealthMonitor stored on app.state (populated during lifespan)."""
    return request.app.state.monitor


def get_webhook_service_dep(request: Request):
    """Return the WebhookService stored on app.state (populated during lifespan)."""
    return request.app.state.webhook_service


def get_fee_collector_dep(request: Request):
    """Return the FeeCollector stored on app.state, falling back to module-level singleton."""
    try:
        return request.app.state.fee_collector
    except AttributeError:
        from sthrip.services.fee_collector import get_fee_collector
        return get_fee_collector()


def get_agent_registry_dep(request: Request):
    """Return the AgentRegistry stored on app.state, falling back to module-level singleton."""
    try:
        return request.app.state.agent_registry
    except AttributeError:
        from sthrip.services.agent_registry import get_registry
        return get_registry()


def get_idempotency_store_dep(request: Request):
    """Return the IdempotencyStore stored on app.state, falling back to module-level singleton."""
    try:
        return request.app.state.idempotency_store
    except AttributeError:
        from sthrip.services.idempotency import get_idempotency_store
        return get_idempotency_store()


# ═══════════════════════════════════════════════════════════════════════════════
# AUTHENTICATION
# ═══════════════════════════════════════════════════════════════════════════════



async def get_current_agent(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    request: Request = None,
    db: Session = Depends(get_db_session),
) -> Agent:
    """Authenticate agent using the same DB session as the request handler."""
    client_ip = get_client_ip(request)
    try:
        limiter = request.app.state.rate_limiter
    except AttributeError:
        limiter = get_rate_limiter()

    # Check failed auth limit (read-only check, no increment)
    try:
        limiter.check_failed_auth(client_ip)
    except RateLimitExceeded:
        raise HTTPException(status_code=429, detail="Too many authentication attempts")

    if not credentials:
        limiter.record_failed_auth(client_ip)
        audit_log("auth.failed", ip_address=client_ip, details={"reason": "missing_api_key"}, success=False)
        raise HTTPException(status_code=401, detail="Missing API key")

    api_key = credentials.credentials

    repo = AgentRepository(db)
    agent = repo.get_by_api_key(api_key)

    if not agent:
        limiter.record_failed_auth(client_ip)
        audit_log("auth.failed", ip_address=client_ip, details={"reason": "invalid_api_key"}, success=False)
        raise HTTPException(status_code=401, detail="Invalid API key")

    if not agent.is_active:
        limiter.record_failed_auth(client_ip)
        audit_log("auth.failed", agent_id=str(agent.id), ip_address=client_ip, details={"reason": "agent_disabled"}, success=False)
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




# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN AUTH
# ═══════════════════════════════════════════════════════════════════════════════

def verify_admin_key(admin_key: Optional[str]) -> None:
    """Verify admin key using constant-time comparison."""
    expected_key = get_settings().admin_api_key
    if not expected_key or not admin_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")
    if not hmac.compare_digest(admin_key.encode(), expected_key.encode()):
        raise HTTPException(status_code=401, detail="Invalid admin key")


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN SESSION-TOKEN AUTH (CRIT-4)
# ═══════════════════════════════════════════════════════════════════════════════

_ADMIN_SESSION_TTL = 8 * 3600  # 8 hours

# API admin sessions use a distinct key prefix so they never collide with
# dashboard cookie sessions (which live under "admin_session:").
_admin_session_store = AdminSessionStore(key_prefix="admin_api_session:")


def get_admin_session_store() -> AdminSessionStore:
    """Return the API admin session store singleton."""
    return _admin_session_store


async def get_admin_session(request: Request) -> bool:
    """Authenticate admin via bearer session token only.

    Use POST /v2/admin/auth to obtain a session token, then pass it
    as ``Authorization: Bearer <token>``.
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if _admin_session_store.validate_session(token):
            return True
        raise HTTPException(status_code=401, detail="Invalid or expired admin session token")

    raise HTTPException(status_code=401, detail="Admin authentication required")
