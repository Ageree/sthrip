"""Spending policy endpoints — per-agent spending controls.

Mounted under ``/v2/me`` so that the policy is always scoped to the
authenticated agent (no agent-id in the URL).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.db.spending_policy_repo import SpendingPolicyRepository
from api.deps import get_current_agent
from api.schemas import SpendingPolicyRequest, SpendingPolicyResponse

logger = logging.getLogger("sthrip")

router = APIRouter(prefix="/v2/me", tags=["spending-policy"])


@router.put("/spending-policy", response_model=SpendingPolicyResponse)
async def upsert_spending_policy(
    body: SpendingPolicyRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Create or replace the spending policy for the authenticated agent."""
    with get_db() as db:
        repo = SpendingPolicyRepository(db)
        policy = repo.upsert(
            agent.id,
            max_per_tx=body.max_per_tx,
            max_per_session=body.max_per_session,
            daily_limit=body.daily_limit,
            allowed_agents=body.allowed_agents,
            blocked_agents=body.blocked_agents,
            require_escrow_above=body.require_escrow_above,
        )
        return SpendingPolicyResponse(
            max_per_tx=str(policy.max_per_tx) if policy.max_per_tx is not None else None,
            max_per_session=str(policy.max_per_session) if policy.max_per_session is not None else None,
            daily_limit=str(policy.daily_limit) if policy.daily_limit is not None else None,
            allowed_agents=policy.allowed_agents,
            blocked_agents=policy.blocked_agents,
            require_escrow_above=str(policy.require_escrow_above) if policy.require_escrow_above is not None else None,
            is_active=policy.is_active,
        )


@router.get("/spending-policy", response_model=SpendingPolicyResponse)
async def get_spending_policy(
    agent: Agent = Depends(get_current_agent),
):
    """Return the spending policy for the authenticated agent (or 404)."""
    with get_db() as db:
        repo = SpendingPolicyRepository(db)
        policy = repo.get_by_agent_id(agent.id)
        if policy is None:
            raise HTTPException(status_code=404, detail="No spending policy configured")
        return SpendingPolicyResponse(
            max_per_tx=str(policy.max_per_tx) if policy.max_per_tx is not None else None,
            max_per_session=str(policy.max_per_session) if policy.max_per_session is not None else None,
            daily_limit=str(policy.daily_limit) if policy.daily_limit is not None else None,
            allowed_agents=policy.allowed_agents,
            blocked_agents=policy.blocked_agents,
            require_escrow_above=str(policy.require_escrow_above) if policy.require_escrow_above is not None else None,
            is_active=policy.is_active,
        )
