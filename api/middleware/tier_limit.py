"""Phase 2 Sprint 3 — FREE-tier monthly transaction limit middleware.

Gates payment-creating endpoints when a FREE-tier agent has hit
``FREE_TIER_MONTHLY_LIMIT`` (100) transfers in the current UTC month.
VERIFIED / PREMIUM / ENTERPRISE tiers are unlimited.

Honors ``Agent.tier_grace_until`` — when set and in the future, the
declared tier wins (do not punish an agent because the billing cron just
missed). When set and in the past, the agent is treated as FREE for the
limit check (grace expired).

Order: registered AFTER the existing auth middleware so that
``request.state.agent`` (or fallback Bearer-token DB lookup) is available.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Final, Iterable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sthrip.db.database import get_db
from sthrip.db.enums import AgentTier
from sthrip.db.models import Agent
from sthrip.db.repository import AgentRepository
from sthrip.services.agent_stats_service import get_current_month_count

logger = logging.getLogger("sthrip.tier_limit")

# Hard upper bound for FREE tier per lead-decisions.md.
FREE_TIER_MONTHLY_LIMIT: Final[int] = 100

# Endpoint paths that create user-paid transfers and therefore must respect
# the tier limit. Any future commission-bearing endpoint must be added here.
# GET, balance lookups, marketplace browsing, and admin endpoints are
# intentionally excluded.
GATED_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/v2/payments/hub-routing",
        "/v2/payments/internal-transfer",
    }
)
GATED_METHODS: Final[frozenset[str]] = frozenset({"POST"})


def _is_gated(request: Request) -> bool:
    if request.method not in GATED_METHODS:
        return False
    return request.url.path in GATED_PATHS


def _effective_tier_is_free(agent: Agent, now: datetime) -> bool:
    """Return True iff the agent should be enforced as FREE.

    * If declared ``tier`` is FREE → always enforce (regardless of grace).
    * If declared ``tier`` is paid (VERIFIED/PREMIUM/ENTERPRISE):
        - No ``tier_grace_until`` set → declared tier wins (paid → unlimited).
        - ``tier_grace_until`` in future → declared tier wins.
        - ``tier_grace_until`` in past → grace expired → enforce as FREE.
    """
    declared = agent.tier
    if declared == AgentTier.FREE:
        return True

    grace = agent.tier_grace_until
    if grace is None:
        # Paid tier with no grace marker → unlimited.
        return False

    # Normalize naive datetimes to UTC for comparison.
    if grace.tzinfo is None:
        grace = grace.replace(tzinfo=timezone.utc)

    if grace > now:
        # Grace still active → preserve declared (paid) tier.
        return False
    # Grace expired → enforce as FREE until billing/tier flips.
    return True


def _resolve_agent(request: Request) -> Optional[Agent]:
    """Resolve the calling agent from the request.

    Tries ``request.state.agent`` first (populated by an auth middleware /
    dependency), then falls back to a Bearer-token DB lookup so the
    middleware works even before the FastAPI dep graph runs.
    """
    state_agent = getattr(request.state, "agent", None)
    if state_agent is not None and isinstance(state_agent, Agent):
        return state_agent

    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    api_key = auth[7:].strip()
    if not api_key:
        return None
    try:
        with get_db() as db:
            repo = AgentRepository(db)
            agent = repo.get_by_api_key(api_key)
            if agent is None or not agent.is_active:
                return None
            # Detach from session — only need read-only access here.
            db.expunge(agent)
            return agent
    except Exception as exc:  # noqa: BLE001 — middleware must never crash
        logger.debug("tier_limit: agent resolve failed: %s", exc)
        return None


def _current_count(agent_id) -> int:
    try:
        with get_db() as db:
            return get_current_month_count(agent_id, db)
    except Exception as exc:  # noqa: BLE001 — fail open on infra error
        logger.warning("tier_limit: count query failed: %s", exc)
        return 0


def _too_many_response() -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "error": "tier_limit_reached",
            "current_count": FREE_TIER_MONTHLY_LIMIT,
            "limit": FREE_TIER_MONTHLY_LIMIT,
            "upgrade_url": "/v2/me/upgrade",
            "message": (
                "Free tier limit reached. Upgrade to Pro ($29/month) for "
                "unlimited transactions."
            ),
        },
    )


def configure_tier_limit_middleware(app: FastAPI) -> None:
    """Register the FREE-tier limit middleware on ``app``.

    Must be called AFTER auth middleware so that the calling agent is
    resolvable. The middleware itself short-circuits with 429 on overage.
    """

    @app.middleware("http")
    async def enforce_tier_limit(request: Request, call_next):
        if not _is_gated(request):
            return await call_next(request)

        agent = _resolve_agent(request)
        if agent is None:
            # Auth will reject downstream; let the actual handler return 401.
            return await call_next(request)

        now = datetime.now(timezone.utc)
        if not _effective_tier_is_free(agent, now):
            return await call_next(request)

        count = _current_count(agent.id)
        if count >= FREE_TIER_MONTHLY_LIMIT:
            return _too_many_response()

        return await call_next(request)


__all__ = [
    "FREE_TIER_MONTHLY_LIMIT",
    "GATED_PATHS",
    "GATED_METHODS",
    "configure_tier_limit_middleware",
]
