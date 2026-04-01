"""Multi-party payment endpoints: create, status, accept, reject, list."""

import logging
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.services.multi_party_service import MultiPartyService
from api.deps import get_current_agent
from api.schemas_multi_party import MultiPartyCreateRequest

logger = logging.getLogger("sthrip")
router = APIRouter(prefix="/v2/payments/multi", tags=["multi-party"])

_svc = MultiPartyService()


def _handle_service_error(exc: Exception):
    """Map service exceptions to HTTP responses."""
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise exc


@router.post("", status_code=201)
async def create_multi_party(
    req: MultiPartyCreateRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Create a new multi-party payment. The authenticated agent is the sender."""
    with get_db() as db:
        try:
            result = _svc.create_multi_party(
                db=db,
                sender_id=agent.id,
                recipients=[
                    {"agent_name": r.agent_name, "amount": r.amount}
                    for r in req.recipients
                ],
                currency=req.currency,
                require_all_accept=req.require_all_accept,
                accept_hours=req.accept_hours,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

    return result


@router.get("")
async def list_multi_party(
    role: Optional[str] = Query(default=None, pattern=r"^(sender|recipient)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    agent: Agent = Depends(get_current_agent),
):
    """List multi-party payments for the authenticated agent."""
    with get_db() as db:
        result = _svc.list_by_agent(
            db=db,
            agent_id=agent.id,
            role=role,
            limit=limit,
            offset=offset,
        )

    return result


@router.get("/{payment_id}")
async def get_multi_party_status(
    payment_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Get status of a multi-party payment."""
    with get_db() as db:
        try:
            result = _svc.get_status(db=db, payment_id=payment_id, agent_id=agent.id)
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

    return result


@router.post("/{payment_id}/accept")
async def accept_multi_party(
    payment_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Recipient accepts a multi-party payment."""
    with get_db() as db:
        try:
            result = _svc.accept(
                db=db, recipient_agent_id=agent.id, payment_id=payment_id,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

    return result


@router.post("/{payment_id}/reject")
async def reject_multi_party(
    payment_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Recipient rejects a multi-party payment."""
    with get_db() as db:
        try:
            result = _svc.reject(
                db=db, recipient_agent_id=agent.id, payment_id=payment_id,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

    return result
