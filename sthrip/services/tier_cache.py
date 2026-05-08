"""Phase 2 Sprint 2 — in-request tier memoization.

Prevents tier-bypass within a single request: an attacker who races a
``PATCH /v2/agents/me`` (downgrade to Pro mid-request) cannot pay Pro
rates on a transfer that started before the change. Cache is keyed by
``(request_id, agent_id)`` and lives only for the duration of one
request via ``contextvars``.

Outside a request context (background tasks, tests, cron jobs) the cache
is bypassed and a direct DB read is performed — slightly slower but
correctness-preserving.
"""

from __future__ import annotations

import contextvars
from typing import Dict, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.enums import AgentTier
from sthrip.db.models import Agent

# Per-request, per-agent tier cache. Set by ``begin_request``, cleared by
# ``end_request``. Default factory returns None to mean "no active request".
_request_tier_cache: contextvars.ContextVar[Optional[Dict[UUID, AgentTier]]] = (
    contextvars.ContextVar("sthrip_request_tier_cache", default=None)
)


def begin_request() -> contextvars.Token:
    """Open a fresh cache scope. Returns the contextvar token for ``end_request``."""
    return _request_tier_cache.set({})


def end_request(token: contextvars.Token) -> None:
    """Close the cache scope previously opened by ``begin_request``."""
    _request_tier_cache.reset(token)


def get_tier(agent_id: UUID, db: Session) -> AgentTier:
    """Return ``Agent.tier`` for ``agent_id``, memoized per request.

    * If a request scope is active and the agent_id is cached, returns the
      cached value (no DB read).
    * Otherwise reads ``Agent.tier`` from DB. If a request scope is active,
      memoizes the result for subsequent calls in the same request.
    * Falls back to ``AgentTier.FREE`` if the agent row is missing — keeps
      callers safe; missing-agent error will surface elsewhere.
    """
    cache = _request_tier_cache.get()
    if cache is not None and agent_id in cache:
        return cache[agent_id]

    row = db.query(Agent.tier).filter(Agent.id == agent_id).first()
    tier = row[0] if row is not None else AgentTier.FREE
    if not isinstance(tier, AgentTier):
        # SQLAlchemy may return raw string for enum-as-VARCHAR rows — coerce.
        try:
            tier = AgentTier(tier)
        except ValueError:
            tier = AgentTier.FREE

    if cache is not None:
        cache[agent_id] = tier
    return tier


def clear_for_agent(agent_id: UUID) -> None:
    """Drop a single agent's cached tier (e.g. after a tier upgrade in same request)."""
    cache = _request_tier_cache.get()
    if cache is not None:
        cache.pop(agent_id, None)


__all__ = ["begin_request", "end_request", "get_tier", "clear_for_agent"]
