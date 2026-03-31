"""Agent registry endpoints: registration, discovery, profiles."""

import logging
import time
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends, Request, Query
from sqlalchemy.orm import Session

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.services.rate_limiter import get_rate_limiter, RateLimitExceeded
from sthrip.services.agent_registry import get_registry
from sthrip.services.audit_logger import log_event as audit_log
from api.deps import get_current_agent, get_db_session
from api.helpers import get_client_ip
from api.schemas import (
    AgentRegistration, AgentResponse, AgentProfileResponse, AgentSettingsUpdate,
    AgentMarketplaceResponse, POWChallengeResponse,
)
from sthrip.services.pow_service import get_pow_service
from sthrip.db.enums import PrivacyLevel
from sthrip.db.repository import AgentRepository

logger = logging.getLogger("sthrip")

_ADDR_FIELDS = {"xmr_address", "base_address", "solana_address"}


def _redact_addresses(d: dict) -> dict:
    """Truncate wallet addresses in audit log details to avoid leaking full addresses."""
    return {
        k: (v[:8] + "..." if k in _ADDR_FIELDS and v else v)
        for k, v in d.items()
    }


def _check_ip_rate_limit(
    request: Request,
    action: str,
    per_ip_limit: int,
    global_limit: int,
    window_seconds: int,
    detail: str = "Rate limit exceeded",
) -> None:
    """Check IP-based rate limit, raising 429 if exceeded."""
    try:
        limiter = get_rate_limiter()
        client_ip = get_client_ip(request)
        limiter.check_ip_rate_limit(
            ip_address=client_ip,
            action=action,
            per_ip_limit=per_ip_limit,
            global_limit=global_limit,
            window_seconds=window_seconds,
        )
    except RateLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail=detail,
            headers={"Retry-After": str(int(e.reset_at - time.time()))},
        )


router = APIRouter(tags=["agents"])


@router.post(
    "/v2/agents/register/challenge",
    response_model=POWChallengeResponse,
)
async def get_registration_challenge(request: Request):
    """Return a proof-of-work challenge that must be solved before registration.

    The challenge contains a random nonce and a difficulty target.  The
    client must find a counter ``c`` such that
    ``sha256(nonce + ":" + c)`` has at least ``difficulty_bits`` leading
    zero bits.  Include the solved challenge as ``pow_challenge`` in the
    registration request body.
    """
    _check_ip_rate_limit(
        request, "pow_challenge", per_ip_limit=30, global_limit=500,
        window_seconds=60, detail="Challenge rate limit exceeded",
    )

    pow_svc = get_pow_service()
    challenge = pow_svc.create_challenge()
    return POWChallengeResponse(**challenge)


@router.post("/v2/agents/register", response_model=AgentResponse, status_code=201)
async def register_agent(reg: AgentRegistration, request: Request):
    """Register new agent"""
    _check_ip_rate_limit(
        request, "register", per_ip_limit=5, global_limit=100,
        window_seconds=3600, detail="Registration rate limit exceeded",
    )

    # Verify proof-of-work if provided
    if reg.pow_challenge is not None:
        pow_svc = get_pow_service()
        challenge_dict = {
            "nonce": reg.pow_challenge.nonce,
            "difficulty_bits": reg.pow_challenge.difficulty_bits,
            "expires_at": reg.pow_challenge.expires_at,
        }
        if not pow_svc.verify(challenge_dict, reg.pow_challenge.solution):
            raise HTTPException(
                status_code=400,
                detail="Invalid or expired proof-of-work solution",
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
            capabilities=reg.capabilities,
            pricing=reg.pricing,
            description=reg.description,
            accepts_escrow=reg.accepts_escrow,
        )

        client_ip = get_client_ip(request)
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
            webhook_secret=result["webhook_secret"],
            created_at=result["created_at"],
        )
    except ValueError as e:
        logger.warning("Registration failed: %s", e)
        raise HTTPException(status_code=400, detail="Registration failed. Check your input and try again.")


@router.get("/v2/agents/marketplace")
async def marketplace(
    request: Request,
    capability: Optional[str] = Query(default=None, min_length=1, max_length=50),
    accepts_escrow: Optional[bool] = Query(default=None),
    min_trust_score: Optional[int] = Query(default=None, ge=0, le=100),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Browse the agent marketplace -- returns capability and pricing info."""
    _check_ip_rate_limit(request, "discovery", per_ip_limit=60, global_limit=1000, window_seconds=60)

    registry = get_registry()
    profiles = registry.discover_agents(
        min_trust_score=min_trust_score,
        capability=capability,
        accepts_escrow=accepts_escrow,
        limit=limit,
        offset=offset,
    )
    total = registry.count_agents(
        min_trust_score=min_trust_score,
        capability=capability,
        accepts_escrow=accepts_escrow,
    )
    return {
        "items": [
            AgentMarketplaceResponse(
                agent_name=p.agent_name,
                description=p.description,
                capabilities=p.capabilities,
                pricing=p.pricing,
                accepts_escrow=p.accepts_escrow,
                tier=p.tier,
                trust_score=p.trust_score,
                verified_at=p.verified_at,
            )
            for p in profiles
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/v2/agents/{agent_name}", response_model=AgentProfileResponse)
async def get_agent_profile(agent_name: str, request: Request):
    """Get public agent profile"""
    _check_ip_rate_limit(request, "discovery", per_ip_limit=60, global_limit=1000, window_seconds=60)

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
        capabilities=profile.capabilities,
        pricing=profile.pricing,
        description=profile.description,
        accepts_escrow=profile.accepts_escrow,
    )


@router.get("/v2/agents")
async def discover_agents(
    request: Request,
    min_trust_score: Optional[int] = Query(default=None, ge=0, le=100),
    tier: Optional[str] = Query(default=None, pattern=r"^(free|verified|premium|enterprise)$"),
    verified_only: bool = False,
    capability: Optional[str] = Query(default=None, min_length=1, max_length=50),
    accepts_escrow: Optional[bool] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Discover agents with filters"""
    _check_ip_rate_limit(request, "discovery", per_ip_limit=60, global_limit=1000, window_seconds=60)

    registry = get_registry()
    profiles = registry.discover_agents(
        min_trust_score=min_trust_score,
        tier=tier,
        verified_only=verified_only,
        capability=capability,
        accepts_escrow=accepts_escrow,
        limit=limit,
        offset=offset,
    )
    total = registry.count_agents(
        min_trust_score=min_trust_score,
        tier=tier,
        verified_only=verified_only,
        capability=capability,
        accepts_escrow=accepts_escrow,
    )
    return {
        "items": [
            AgentProfileResponse(
                agent_name=p.agent_name,
                did=p.did,
                tier=p.tier,
                trust_score=p.trust_score,
                total_transactions=p.total_transactions,
                xmr_address=p.xmr_address,
                base_address=p.base_address,
                verified_at=p.verified_at,
                capabilities=p.capabilities,
                pricing=p.pricing,
                description=p.description,
                accepts_escrow=p.accepts_escrow,
            )
            for p in profiles
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/v2/leaderboard")
async def get_leaderboard(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
):
    """Get top agents by trust score"""
    _check_ip_rate_limit(request, "discovery", per_ip_limit=60, global_limit=1000, window_seconds=60)

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
        "capabilities": agent.capabilities if agent.capabilities else [],
        "pricing": agent.pricing if agent.pricing else {},
        "description": agent.description,
        "accepts_escrow": agent.accepts_escrow if agent.accepts_escrow is not None else True,
    }


@router.patch("/v2/me/settings")
async def update_agent_settings(
    settings: AgentSettingsUpdate,
    request: Request,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db_session),
):
    """Update agent settings (webhook_url, privacy_level, wallet addresses)"""
    db_agent = db.query(Agent).filter(
        Agent.id == agent.id, Agent.is_active == True
    ).first()
    if not db_agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    old_values = {}
    new_values = {}

    # Scalar fields (coerce privacy_level to enum)
    scalar_fields = {
        "webhook_url": settings.webhook_url,
        "privacy_level": settings.privacy_level,
        "xmr_address": settings.xmr_address,
        "base_address": settings.base_address,
        "solana_address": settings.solana_address,
        "description": settings.description,
        "accepts_escrow": settings.accepts_escrow,
    }
    for field, value in scalar_fields.items():
        if value is not None:
            old_val = getattr(db_agent, field)
            old_values[field] = str(old_val) if old_val else None
            new_values[field] = value
            coerced = PrivacyLevel(value) if field == "privacy_level" else value
            setattr(db_agent, field, coerced)

    # JSON fields (list/dict -- compare against None, not truthiness)
    json_fields = {
        "capabilities": settings.capabilities,
        "pricing": settings.pricing,
    }
    for field, value in json_fields.items():
        if value is not None:
            old_val = getattr(db_agent, field)
            old_values[field] = old_val
            new_values[field] = value
            setattr(db_agent, field, value)

    if not new_values:
        raise HTTPException(status_code=400, detail="No fields to update")

    audit_log(
        "agent.settings_updated",
        agent_id=str(agent.id),
        ip_address=get_client_ip(request),
        request_method="PATCH",
        request_path="/v2/me/settings",
        details={"old": _redact_addresses(old_values), "new": _redact_addresses(new_values)},
        db=db,
    )

    return {"updated": list(new_values.keys()), "message": "Settings updated"}


@router.post("/v2/me/rotate-key")
async def rotate_api_key(
    request: Request,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db_session),
):
    """Rotate API key. Returns the new key once — store it securely."""
    import secrets as _secrets

    new_key = f"sk_{_secrets.token_hex(32)}"
    new_hash = AgentRepository._hash_api_key(new_key)

    db_agent = db.query(Agent).filter(Agent.id == agent.id).first()
    if not db_agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    db_agent.api_key_hash = new_hash

    audit_log(
        "agent.key_rotated",
        agent_id=str(agent.id),
        ip_address=get_client_ip(request),
        request_method="POST",
        request_path="/v2/me/rotate-key",
        db=db,
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
