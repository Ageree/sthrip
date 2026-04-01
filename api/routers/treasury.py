"""Treasury management endpoints.

Routes
------
PUT    /v2/me/treasury/policy    -- set or update treasury policy
GET    /v2/me/treasury/policy    -- get current policy
DELETE /v2/me/treasury/policy    -- deactivate policy
GET    /v2/me/treasury/status    -- current allocation and balances
POST   /v2/me/treasury/rebalance -- trigger portfolio rebalance
GET    /v2/me/treasury/history   -- rebalance log
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.services.treasury_service import TreasuryService
from api.deps import get_current_agent
from api.schemas_treasury import (
    TreasuryPolicyRequest,
    TreasuryPolicyResponse,
    TreasuryStatusResponse,
    TreasuryRebalanceResponse,
    TreasuryHistoryResponse,
)

logger = logging.getLogger("sthrip")

router = APIRouter(prefix="/v2/me/treasury", tags=["treasury"])

_svc = TreasuryService()


@router.put("/policy", response_model=TreasuryPolicyResponse)
async def set_policy(
    req: TreasuryPolicyRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Set or update the agent's treasury allocation policy."""
    with get_db() as db:
        try:
            result = _svc.set_policy(
                db,
                agent.id,
                allocation=req.allocation,
                rebalance_threshold_pct=req.rebalance_threshold_pct,
                cooldown_minutes=req.cooldown_minutes,
                emergency_reserve_pct=req.emergency_reserve_pct,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return result


@router.get("/policy", response_model=TreasuryPolicyResponse)
async def get_policy(
    agent: Agent = Depends(get_current_agent),
):
    """Get the agent's current treasury policy."""
    with get_db() as db:
        result = _svc.get_policy(db, agent.id)

    if result is None:
        raise HTTPException(status_code=404, detail="No treasury policy found")

    return result


@router.delete("/policy")
async def deactivate_policy(
    agent: Agent = Depends(get_current_agent),
):
    """Deactivate the agent's treasury policy."""
    with get_db() as db:
        _svc.deactivate_policy(db, agent.id)

    return {"detail": "Treasury policy deactivated"}


@router.get("/status", response_model=TreasuryStatusResponse)
async def get_status(
    agent: Agent = Depends(get_current_agent),
):
    """Get current portfolio allocation and balances."""
    with get_db() as db:
        result = _svc.get_status(db, agent.id)

    return result


@router.post("/rebalance", response_model=TreasuryRebalanceResponse)
async def rebalance(
    agent: Agent = Depends(get_current_agent),
):
    """Trigger a manual portfolio rebalance."""
    with get_db() as db:
        try:
            result = _svc.rebalance(db, agent.id, trigger="manual")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return result


@router.get("/history", response_model=TreasuryHistoryResponse)
async def get_history(
    agent: Agent = Depends(get_current_agent),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Get the agent's rebalance history."""
    with get_db() as db:
        items = _svc.get_history(db, agent.id, limit=limit, offset=offset)

    return {"items": items}
