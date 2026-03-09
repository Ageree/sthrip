"""Admin endpoints."""

import logging

from fastapi import APIRouter, HTTPException, Depends, Header, Request, Query

from sthrip.services.fee_collector import get_fee_collector
from sthrip.services.agent_registry import get_registry
from sthrip.services.monitoring import get_monitor
from sthrip.services.webhook_service import get_webhook_service
from sthrip.services.audit_logger import log_event as audit_log
from api.deps import verify_admin_key

logger = logging.getLogger("sthrip")

router = APIRouter(prefix="/v2/admin", tags=["admin"])


@router.get("/stats")
async def get_admin_stats(request: Request, admin_key: str = Header(None)):
    """Get admin statistics"""
    verify_admin_key(admin_key)
    audit_log(
        "admin.stats_viewed",
        ip_address=request.client.host if request.client else None,
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
    admin_key: str = Header(None),
):
    """Verify agent (admin only)"""
    verify_admin_key(admin_key)

    registry = get_registry()
    try:
        result = registry.verify_agent(
            agent_id=agent_id,
            verified_by="admin",
            tier=tier,
        )
        audit_log(
            "agent.verified",
            ip_address=request.client.host if request.client else None,
            request_method="POST",
            request_path=f"/v2/admin/agents/{agent_id}/verify",
            details={"agent_id": agent_id, "tier": tier},
        )
        return result
    except ValueError as e:
        logger.warning("Admin verify failed for agent=%s: %s", agent_id, e)
        raise HTTPException(status_code=404, detail="Agent not found or verification failed.")


