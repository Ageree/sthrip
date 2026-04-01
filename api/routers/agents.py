"""Agent registry endpoints: registration, discovery, profiles."""

import logging
import time
from decimal import Decimal
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException, Depends, Request, Query
from sqlalchemy.orm import Session

from sthrip.db.database import get_db
from sthrip.db.models import Agent, AgentRatingSummary, SLATemplate
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


def _build_rating_dict(summary: Optional[AgentRatingSummary]) -> Optional[Dict[str, Any]]:
    """Convert an AgentRatingSummary row into a plain dict for the API response.

    Returns None when no summary row exists so callers can distinguish between
    "no data" and "zero rating".
    """
    if summary is None:
        return None
    return {
        "overall": float(summary.avg_overall),
        "total_reviews": summary.total_reviews,
        "speed": float(summary.avg_speed),
        "quality": float(summary.avg_quality),
        "reliability": float(summary.avg_reliability),
    }


def _build_sla_templates_list(templates: List[SLATemplate]) -> List[Dict[str, Any]]:
    """Convert active SLATemplate rows into plain dicts for the API response."""
    return [
        {
            "name": tpl.name,
            "price": str(tpl.base_price),
            "delivery_time_secs": tpl.delivery_time_secs,
            "penalty_percent": tpl.penalty_percent,
        }
        for tpl in templates
        if tpl.is_active
    ]


@router.get("/v2/agents/marketplace")
async def marketplace(
    request: Request,
    capability: Optional[str] = Query(default=None, min_length=1, max_length=50),
    accepts_escrow: Optional[bool] = Query(default=None),
    min_trust_score: Optional[int] = Query(default=None, ge=0, le=100),
    # v2 enhanced filters
    min_rating: Optional[float] = Query(default=None, ge=0.0, le=5.0),
    min_reviews: Optional[int] = Query(default=None, ge=0),
    max_price: Optional[float] = Query(default=None, ge=0.0),
    has_sla: Optional[bool] = Query(default=None),
    sort: Optional[str] = Query(default=None, pattern=r"^(rating|price|reviews|trust_score)$"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Browse the agent marketplace -- returns capability, pricing, rating and SLA info.

    Enhanced v2 filters:
    - min_rating: only agents with avg_overall >= min_rating (requires rating summary)
    - min_reviews: only agents with total_reviews >= min_reviews (requires rating summary)
    - max_price: only agents with cheapest active SLA template <= max_price
    - has_sla: true = must have at least one active SLA template; false = must have none
    - sort: rating | price | reviews | trust_score (all descending)
    """
    _check_ip_rate_limit(request, "discovery", per_ip_limit=60, global_limit=1000, window_seconds=60)

    registry = get_registry()

    # Fetch a broad result set from the registry (handles base filters).
    # We request a larger page to allow for secondary filtering; final
    # pagination is applied after the enhanced filters have been applied.
    # For simplicity and SQLite compatibility we fetch up to 5000 candidates
    # and filter in Python – acceptable for the expected dataset size.
    _CANDIDATE_LIMIT = 5000
    profiles = registry.discover_agents(
        min_trust_score=min_trust_score,
        capability=capability,
        accepts_escrow=accepts_escrow,
        limit=_CANDIDATE_LIMIT,
        offset=0,
    )

    # Collect agent names for the DB lookups below.
    agent_names = [p.agent_name for p in profiles]

    # Build name→rating and name→sla-templates maps using a single DB session
    # per collection to avoid N+1 queries.
    rating_by_name: Dict[str, Optional[AgentRatingSummary]] = {}
    sla_by_name: Dict[str, List[SLATemplate]] = {}

    if agent_names:
        with get_db() as db:
            agents_rows = (
                db.query(Agent)
                .filter(Agent.agent_name.in_(agent_names))
                .all()
            )
            id_to_name = {str(a.id): a.agent_name for a in agents_rows}
            name_to_id = {a.agent_name: a.id for a in agents_rows}

            # Rating summaries
            summaries = (
                db.query(AgentRatingSummary)
                .filter(AgentRatingSummary.agent_id.in_(list(name_to_id.values())))
                .all()
            )
            agent_id_to_summary = {str(s.agent_id): s for s in summaries}

            # SLA templates (active only for filtering; we keep all for response)
            templates = (
                db.query(SLATemplate)
                .filter(SLATemplate.provider_id.in_(list(name_to_id.values())))
                .all()
            )
            # Group by provider
            for tpl in templates:
                name = id_to_name.get(str(tpl.provider_id))
                if name:
                    sla_by_name.setdefault(name, []).append(tpl)

            for name in agent_names:
                aid = name_to_id.get(name)
                summary = agent_id_to_summary.get(str(aid)) if aid else None
                rating_by_name[name] = summary

    # -------------------------------------------------------------------------
    # Python-side filtering (SQLite-compatible; no JSONB operators needed).
    # -------------------------------------------------------------------------

    def _passes_rating_filter(name: str) -> bool:
        if min_rating is None and min_reviews is None:
            return True
        summary = rating_by_name.get(name)
        if summary is None:
            # No summary row means no reviews at all – exclude when filter active.
            return False
        if min_rating is not None and float(summary.avg_overall) < min_rating:
            return False
        if min_reviews is not None and summary.total_reviews < min_reviews:
            return False
        return True

    def _passes_sla_filter(name: str) -> bool:
        if has_sla is None and max_price is None:
            return True
        active_templates = [t for t in sla_by_name.get(name, []) if t.is_active]
        if has_sla is True and not active_templates:
            return False
        if has_sla is False and active_templates:
            return False
        if max_price is not None:
            if not active_templates:
                return False
            cheapest = min(float(t.base_price) for t in active_templates)
            if cheapest > max_price:
                return False
        return True

    filtered = [
        p for p in profiles
        if _passes_rating_filter(p.agent_name) and _passes_sla_filter(p.agent_name)
    ]

    # -------------------------------------------------------------------------
    # Sorting (immutable – produces a new sorted list).
    # -------------------------------------------------------------------------

    if sort == "rating":
        def _rating_key(p: Any) -> float:
            summary = rating_by_name.get(p.agent_name)
            return float(summary.avg_overall) if summary else 0.0

        filtered = sorted(filtered, key=_rating_key, reverse=True)

    elif sort == "reviews":
        def _reviews_key(p: Any) -> int:
            summary = rating_by_name.get(p.agent_name)
            return summary.total_reviews if summary else 0

        filtered = sorted(filtered, key=_reviews_key, reverse=True)

    elif sort == "price":
        def _price_key(p: Any) -> float:
            active = [t for t in sla_by_name.get(p.agent_name, []) if t.is_active]
            return min((float(t.base_price) for t in active), default=float("inf"))

        filtered = sorted(filtered, key=_price_key)

    elif sort == "trust_score":
        filtered = sorted(filtered, key=lambda p: p.trust_score, reverse=True)

    total = len(filtered)
    page = filtered[offset: offset + limit]

    # -------------------------------------------------------------------------
    # Build response items (immutable: create new dicts from profiles + lookups).
    # -------------------------------------------------------------------------

    items = []
    for p in page:
        summary = rating_by_name.get(p.agent_name)
        active_tpls = [t for t in sla_by_name.get(p.agent_name, []) if t.is_active]
        item = {
            "agent_name": p.agent_name,
            "description": p.description,
            "capabilities": p.capabilities,
            "pricing": p.pricing,
            "accepts_escrow": p.accepts_escrow,
            "tier": p.tier,
            "trust_score": p.trust_score,
            "verified_at": p.verified_at,
            "rating": _build_rating_dict(summary),
            "sla_templates": _build_sla_templates_list(active_tpls),
        }
        items.append(item)

    return {
        "items": items,
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
