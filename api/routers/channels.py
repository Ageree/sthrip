"""Payment channel endpoints."""

import logging
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.services.channel_service import ChannelService
from api.deps import get_current_agent
from api.schemas_channels import (
    ChannelOpenRequest,
    ChannelStateUpdateRequest,
    ChannelSettleRequest,
    ChannelDisputeRequest,
)

logger = logging.getLogger("sthrip")
router = APIRouter(prefix="/v2/channels", tags=["channels"])

_svc = ChannelService()


def _handle_service_error(exc: Exception):
    """Map ChannelService exceptions to HTTP responses."""
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise exc


@router.post("", status_code=201)
async def open_channel(
    req: ChannelOpenRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Open a new payment channel. The authenticated agent becomes agent A."""
    try:
        agent_b_id = UUID(req.agent_b_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid agent_b_id format")

    with get_db() as db:
        try:
            result = _svc.open_channel(
                db=db,
                agent_a_id=agent.id,
                agent_b_id=agent_b_id,
                deposit_a=req.deposit_a,
                deposit_b=req.deposit_b,
                settlement_period=req.settlement_period,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

    return result


@router.get("")
async def list_channels(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    agent: Agent = Depends(get_current_agent),
):
    """List channels where the authenticated agent is a participant."""
    with get_db() as db:
        result = _svc.list_channels(db=db, agent_id=agent.id, limit=limit, offset=offset)
    return result


@router.get("/{channel_id}")
async def get_channel(
    channel_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Get channel details. Only participants may view."""
    with get_db() as db:
        try:
            result = _svc.get_channel(db=db, channel_id=channel_id, agent_id=agent.id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
    return result


@router.post("/{channel_id}/update")
async def submit_update(
    channel_id: UUID,
    req: ChannelStateUpdateRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Submit an off-chain state update for a channel."""
    with get_db() as db:
        try:
            result = _svc.submit_update(
                db=db,
                channel_id=channel_id,
                agent_id=agent.id,
                nonce=req.nonce,
                balance_a=req.balance_a,
                balance_b=req.balance_b,
                signature_a=req.signature_a,
                signature_b=req.signature_b,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
    return result


@router.post("/{channel_id}/settle")
async def settle_channel(
    channel_id: UUID,
    req: ChannelSettleRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Initiate channel settlement (OPEN -> CLOSING)."""
    with get_db() as db:
        try:
            result = _svc.settle(
                db=db,
                channel_id=channel_id,
                agent_id=agent.id,
                nonce=req.nonce,
                balance_a=req.balance_a,
                balance_b=req.balance_b,
                sig_a=req.sig_a,
                sig_b=req.sig_b,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
    return result


@router.post("/{channel_id}/close")
async def close_channel(
    channel_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Close a settled channel. Credits balances back to participants."""
    with get_db() as db:
        try:
            result = _svc.close(db=db, channel_id=channel_id, agent_id=agent.id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
    return result


@router.post("/{channel_id}/dispute")
async def dispute_channel(
    channel_id: UUID,
    req: ChannelDisputeRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Submit a dispute with a higher-nonce state during the CLOSING window."""
    with get_db() as db:
        try:
            result = _svc.dispute(
                db=db,
                channel_id=channel_id,
                agent_id=agent.id,
                nonce=req.nonce,
                balance_a=req.balance_a,
                balance_b=req.balance_b,
                sig_a=req.sig_a,
                sig_b=req.sig_b,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
    return result
