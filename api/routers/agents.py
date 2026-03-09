"""Agent registry endpoints: registration, discovery, profiles."""

import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends, Request, Query

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.services.rate_limiter import get_rate_limiter, RateLimitExceeded
from sthrip.services.agent_registry import get_registry
from sthrip.services.audit_logger import log_event as audit_log
from api.deps import get_current_agent
from api.schemas import (
    AgentRegistration, AgentResponse, AgentProfileResponse, AgentSettingsUpdate,
)

logger = logging.getLogger("sthrip")

router = APIRouter(tags=["agents"])


@router.post("/v2/agents/register", response_model=AgentResponse, status_code=201)
async def register_agent(reg: AgentRegistration, request: Request):
    """Register new agent"""
    try:
        limiter = get_rate_limiter()
        client_ip = request.client.host if request.client else "unknown"
        limiter.check_ip_rate_limit(
            ip_address=client_ip,
            action="register",
            per_ip_limit=5,
            global_limit=100,
            window_seconds=3600,
        )
    except RateLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail="Registration rate limit exceeded",
            headers={"Retry-After": str(int(e.reset_at - __import__("time").time()))},
        )

    registry = get_registry()
    try:
        result = registry.register_agent(
            agent_name=reg.agent_name,
            webhook_url=reg.webhook_url,
            privacy_level=reg.privacy_level,
            xmr_address=reg.xmr_address,
            base_address=reg.base_address,
            solana_address=reg.solana_address,
        )

        client_ip = request.client.host if request.client else None
        audit_log(
            "agent.registered",
            ip_address=client_ip,
            request_method="POST",
            request_path="/v2/agents/register",
            details={"agent_name": reg.agent_name},
        )

        return AgentResponse(
            agent_id=result["agent_id"],
            agent_name=result["agent_name"],
            tier=result["tier"],
            api_key=result["api_key"],
            created_at=result["created_at"],
        )
    except ValueError as e:
        logger.warning("Registration failed: %s", e)
        raise HTTPException(status_code=400, detail="Registration failed. Check your input and try again.")


@router.get("/v2/agents/{agent_name}", response_model=AgentProfileResponse)
async def get_agent_profile(agent_name: str):
    """Get public agent profile"""
    registry = get_registry()
    profile = registry.get_profile(agent_name)
    if not profile:
        raise HTTPException(status_code=404, detail="Agent not found")
    return AgentProfileResponse(
        agent_name=profile.agent_name,
        did=profile.did,
        tier=profile.tier,
        trust_score=profile.trust_score,
        total_transactions=profile.total_transactions,
        xmr_address=profile.xmr_address,
        base_address=profile.base_address,
        verified_at=profile.verified_at,
    )


@router.get("/v2/agents", response_model=List[AgentProfileResponse])
async def discover_agents(
    min_trust_score: Optional[int] = None,
    tier: Optional[str] = None,
    verified_only: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Discover agents with filters"""
    registry = get_registry()
    profiles = registry.discover_agents(
        min_trust_score=min_trust_score,
        tier=tier,
        verified_only=verified_only,
        limit=limit,
        offset=offset,
    )
    return [
        AgentProfileResponse(
            agent_name=p.agent_name,
            did=p.did,
            tier=p.tier,
            trust_score=p.trust_score,
            total_transactions=p.total_transactions,
            xmr_address=p.xmr_address,
            base_address=p.base_address,
            verified_at=p.verified_at,
        )
        for p in profiles
    ]


@router.get("/v2/leaderboard")
async def get_leaderboard(limit: int = Query(default=100, ge=1, le=500)):
    """Get top agents by trust score"""
    registry = get_registry()
    return registry.get_leaderboard(limit=limit)


# ═══════════════════════════════════════════════════════════════════════════════
# AUTHENTICATED AGENT ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/v2/me")
async def get_current_agent_info(agent: Agent = Depends(get_current_agent)):
    """Get current agent info"""
    return {
        "agent_id": str(agent.id),
        "agent_name": agent.agent_name,
        "tier": agent.tier.value,
        "privacy_level": agent.privacy_level.value,
        "xmr_address": agent.xmr_address,
        "created_at": agent.created_at.isoformat(),
    }


@router.patch("/v2/me/settings")
async def update_agent_settings(
    settings: AgentSettingsUpdate,
    request: Request,
    agent: Agent = Depends(get_current_agent),
):
    """Update agent settings (webhook_url, privacy_level, wallet addresses)"""
    with get_db() as db:
        db_agent = db.query(Agent).filter(Agent.id == agent.id).first()
        if not db_agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        old_values = {}
        new_values = {}
        fields = {
            "webhook_url": settings.webhook_url,
            "privacy_level": settings.privacy_level,
            "xmr_address": settings.xmr_address,
            "base_address": settings.base_address,
            "solana_address": settings.solana_address,
        }
        for field, value in fields.items():
            if value is not None:
                old_val = getattr(db_agent, field)
                old_values[field] = str(old_val) if old_val else None
                new_values[field] = value
                setattr(db_agent, field, value)

        if not new_values:
            raise HTTPException(status_code=400, detail="No fields to update")

        audit_log(
            "agent.settings_updated",
            agent_id=str(agent.id),
            ip_address=request.client.host if request.client else None,
            request_method="PATCH",
            request_path="/v2/me/settings",
            details={"old": old_values, "new": new_values},
        )

        return {"updated": list(new_values.keys()), "message": "Settings updated"}


@router.post("/v2/me/rotate-key")
async def rotate_api_key(
    request: Request,
    agent: Agent = Depends(get_current_agent),
):
    """Rotate API key. Returns the new key once — store it securely."""
    import secrets as _secrets
    import hashlib as _hashlib

    new_key = f"sk_{_secrets.token_hex(32)}"
    new_hash = _hashlib.sha256(new_key.encode()).hexdigest()

    with get_db() as db:
        db_agent = db.query(Agent).filter(Agent.id == agent.id).first()
        if not db_agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        db_agent.api_key_hash = new_hash

    audit_log(
        "agent.key_rotated",
        agent_id=str(agent.id),
        ip_address=request.client.host if request.client else None,
        request_method="POST",
        request_path="/v2/me/rotate-key",
    )

    return {
        "api_key": new_key,
        "message": "Store this API key securely — it cannot be retrieved again. Old key is now invalid.",
    }


@router.get("/v2/me/rate-limit")
async def get_rate_limit_status(agent: Agent = Depends(get_current_agent)):
    """Get current rate limit status"""
    limiter = get_rate_limiter()
    return limiter.get_limit_status(
        agent_id=str(agent.id),
        tier=agent.rate_limit_tier.value,
    )
