"""Admin endpoints."""

import hmac
import logging
import uuid as _uuid

from fastapi import APIRouter, HTTPException, Depends, Header, Request, Query
from pydantic import BaseModel, Field

from typing import List, Optional

from sthrip.db.database import get_db
from sthrip.db.models import Agent, AgentBalance, PendingWithdrawal
from sthrip.services.fee_collector import get_fee_collector
from sthrip.services.agent_registry import get_registry
from sthrip.services.monitoring import get_monitor
from sthrip.services.rate_limiter import get_rate_limiter, RateLimitExceeded
from sthrip.services.webhook_service import get_webhook_service
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.config import get_settings
from api.deps import get_admin_session, get_admin_session_store, _ADMIN_SESSION_TTL
from api.helpers import get_client_ip

logger = logging.getLogger("sthrip")

router = APIRouter(prefix="/v2/admin", tags=["admin"])


class AdminAuthRequest(BaseModel):
    admin_key: str = Field(..., max_length=256)


@router.post("/auth")
async def admin_auth(body: AdminAuthRequest, request: Request):
    """Authenticate with admin key and receive a bearer token."""
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()

    # Check if already rate-limited before verifying credentials
    try:
        limiter.check_failed_auth(client_ip, limit=5, window=300)
    except RateLimitExceeded:
        raise HTTPException(status_code=429, detail="Too many failed admin auth attempts")

    expected_key = get_settings().admin_api_key
    if not expected_key or not hmac.compare_digest(
        body.admin_key.encode(), expected_key.encode()
    ):
        # Atomically increment counter on failed authentication
        limiter.record_failed_auth(client_ip, window=300)
        raise HTTPException(status_code=401, detail="Invalid admin key")

    store = get_admin_session_store()
    token = store.create_session(_ADMIN_SESSION_TTL)
    return {"token": token, "expires_in": _ADMIN_SESSION_TTL}


@router.get("/stats")
async def get_admin_stats(
    request: Request,
    _auth: bool = Depends(get_admin_session),
):
    """Get admin statistics"""
    audit_log(
        "admin.stats_viewed",
        ip_address=get_client_ip(request),
        request_method="GET",
        request_path="/v2/admin/stats",
    )

    registry = get_registry()
    collector = get_fee_collector()
    webhook_service = get_webhook_service()
    monitor = get_monitor()

    return {
        "agents": registry.get_stats(),
        "revenue": collector.get_revenue_stats(days=30),
        "webhooks": webhook_service.get_delivery_stats(days=7),
        "health": monitor.get_health_report(),
        "alerts": [
            {
                "id": a.id,
                "severity": a.severity.value,
                "title": a.title,
                "timestamp": a.timestamp.isoformat(),
            }
            for a in monitor.get_alerts(unacknowledged_only=True)[:10]
        ],
    }


@router.post("/agents/{agent_id}/verify")
async def verify_agent(
    agent_id: str,
    request: Request,
    tier: str = Query(default="verified", pattern=r"^(free|verified|premium|enterprise)$"),
    _auth: bool = Depends(get_admin_session),
):
    """Verify agent (admin only)"""
    try:
        _uuid.UUID(agent_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Agent not found or verification failed.")

    registry = get_registry()
    try:
        result = registry.verify_agent(
            agent_id=agent_id,
            verified_by="admin",
            tier=tier,
        )
        audit_log(
            "agent.verified",
            ip_address=get_client_ip(request),
            request_method="POST",
            request_path=f"/v2/admin/agents/{agent_id}/verify",
            details={"agent_id": agent_id, "tier": tier},
        )
        return result
    except ValueError as e:
        logger.warning("Admin verify failed for agent=%s: %s", agent_id, e)
        raise HTTPException(status_code=404, detail="Agent not found or verification failed.")




class CleanupRequest(BaseModel):
    """Request body for test agent cleanup."""
    name_pattern: Optional[str] = Field(
        default=None,
        max_length=100,
        description="SQL LIKE pattern for agent names (e.g. 'test-%'). If not set, uses default test patterns.",
    )
    dry_run: bool = Field(
        default=True,
        description="If true, only list agents that would be removed without actually removing them.",
    )


@router.post("/cleanup-test-agents")
async def cleanup_test_agents(
    body: CleanupRequest,
    request: Request,
    _auth: bool = Depends(get_admin_session),
):
    """Deactivate test agents matching a name pattern.

    Default patterns: 'test-%', 'e2e-%', 'demo-%', 'atomic-test-%',
    'withdraw-test-%', 'wd-%'.
    Only deactivates agents with zero balance to prevent fund loss.
    """
    default_patterns = [
        "test-%", "e2e-%", "demo-%", "atomic-test-%",
        "withdraw-test-%", "wd-%",
    ]
    patterns = [body.name_pattern] if body.name_pattern else default_patterns

    matched: List[dict] = []
    deactivated = 0
    skipped_with_balance = 0

    with get_db() as db:
        for pattern in patterns:
            agents = db.query(Agent).filter(
                Agent.agent_name.like(pattern),
                Agent.is_active == True,
            ).all()

            for agent in agents:
                # Check balance — never deactivate agents with funds
                balance = db.query(AgentBalance).filter_by(agent_id=agent.id).first()
                has_funds = balance and (
                    (balance.available or 0) > 0 or (balance.pending or 0) > 0
                )

                entry = {
                    "agent_id": str(agent.id),
                    "agent_name": agent.agent_name,
                    "created_at": agent.created_at.isoformat() if agent.created_at else None,
                    "has_funds": bool(has_funds),
                }

                if has_funds:
                    entry["action"] = "skipped (has funds)"
                    skipped_with_balance += 1
                elif body.dry_run:
                    entry["action"] = "would_deactivate"
                else:
                    agent.is_active = False
                    entry["action"] = "deactivated"
                    deactivated += 1

                matched.append(entry)

        if not body.dry_run:
            db.commit()

    audit_log(
        "admin.cleanup_test_agents",
        ip_address=get_client_ip(request),
        request_method="POST",
        request_path="/v2/admin/cleanup-test-agents",
        details={
            "patterns": patterns,
            "dry_run": body.dry_run,
            "matched": len(matched),
            "deactivated": deactivated,
            "skipped_with_balance": skipped_with_balance,
        },
    )

    return {
        "dry_run": body.dry_run,
        "matched": len(matched),
        "deactivated": deactivated,
        "skipped_with_balance": skipped_with_balance,
        "agents": matched,
    }


# Temporary: fix all PG enums on startup
import logging as _log
_log.getLogger("sthrip").info("Admin module loaded")
