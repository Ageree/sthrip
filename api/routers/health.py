"""Health, readiness, metrics, and root endpoints."""

import os
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from sthrip.db.database import get_db
from sthrip.services.monitoring import get_monitor
from sthrip.services.agent_registry import get_registry
from sthrip.services.metrics import get_metrics_response
from api.deps import verify_admin_key
from api.schemas import HealthResponse

logger = logging.getLogger("sthrip")

router = APIRouter(tags=["health"])


@router.get("/", response_model=dict)
async def root():
    """API info"""
    registry = get_registry()
    stats = registry.get_stats()
    return {
        "name": "Sthrip API",
        "version": "2.0.0",
        "description": "Anonymous payments for AI Agents",
        "agents_registered": stats["total_agents"],
        "endpoints": {
            "docs": "/docs",
            "health": "/health",
            "agents": "/v2/agents",
            "payments": "/v2/payments",
        },
    }


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint"""
    monitor = get_monitor()
    report = monitor.get_health_report()
    return HealthResponse(
        status=report["status"],
        version="2.0.0",
        timestamp=report["timestamp"],
        checks=report["checks"],
    )


@router.get("/ready")
async def readiness():
    """Returns 200 only when DB and critical services are connected."""
    checks = {}
    try:
        with get_db() as db:
            db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "failed"
        return JSONResponse(status_code=503, content={"status": "not_ready", "checks": checks})

    if os.getenv("HUB_MODE", "onchain") == "onchain":
        try:
            from api.helpers import get_wallet_service
            wallet_svc = get_wallet_service()
            wallet_svc.wallet.get_height()
            checks["wallet_rpc"] = "ok"
        except Exception:
            checks["wallet_rpc"] = "failed"
            logger.warning("Readiness check failed: %s", checks)
            return JSONResponse(status_code=503, content={"status": "not_ready", "checks": checks})

    return {"status": "ready", "checks": checks}


@router.get("/metrics")
async def metrics_endpoint(admin_key: str = Header(None)):
    """Prometheus metrics (admin-key protected)"""
    verify_admin_key(admin_key)
    result = get_metrics_response()
    if result is None:
        raise HTTPException(status_code=501, detail="prometheus-client not installed")
    body, content_type = result
    from starlette.responses import Response
    return Response(content=body, media_type=content_type)
