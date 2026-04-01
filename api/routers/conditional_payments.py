"""Conditional payment endpoints: create, list, detail, cancel, trigger."""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.db.conditional_payment_repo import ConditionalPaymentRepository
from sthrip.services.conditional_payment_service import ConditionalPaymentService
from api.deps import get_current_agent
from api.schemas_conditional import ConditionalPaymentCreate

logger = logging.getLogger("sthrip")
router = APIRouter(prefix="/v2/payments/conditional", tags=["conditional-payments"])


def _handle_service_error(exc: Exception):
    """Map service exceptions to HTTP responses."""
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise exc


def _lookup_agent_by_name(db, name: str) -> Agent:
    """Resolve agent by name within the given session."""
    agent = db.query(Agent).filter(Agent.agent_name == name).first()
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    if not agent.is_active:
        raise HTTPException(status_code=400, detail=f"Agent '{name}' is not active")
    return agent


@router.post("", status_code=201)
async def create_conditional_payment(
    req: ConditionalPaymentCreate,
    agent: Agent = Depends(get_current_agent),
):
    """Create a new conditional payment. Funds are locked from the sender."""
    with get_db() as db:
        recipient = _lookup_agent_by_name(db, req.to_agent_name)
        try:
            result = ConditionalPaymentService.create_conditional(
                db=db,
                from_agent_id=agent.id,
                to_agent_id=recipient.id,
                amount=req.amount,
                currency=req.currency,
                condition_type=req.condition_type,
                condition_config=req.condition_config,
                expires_hours=req.expires_hours,
                memo=req.memo,
            )
            return result
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)


@router.get("")
async def list_conditional_payments(
    agent: Agent = Depends(get_current_agent),
    role: Optional[str] = Query(default=None, regex="^(sender|recipient)$"),
    state: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List conditional payments for the authenticated agent."""
    with get_db() as db:
        repo = ConditionalPaymentRepository(db)
        items, total = repo.list_by_agent(
            agent_id=agent.id,
            role=role,
            state=state,
            limit=limit,
            offset=offset,
        )
        from sthrip.services.conditional_payment_service import _payment_to_dict
        return {
            "items": [_payment_to_dict(p) for p in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


@router.get("/{payment_id}")
async def get_conditional_payment(
    payment_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Get details of a specific conditional payment."""
    with get_db() as db:
        repo = ConditionalPaymentRepository(db)
        payment = repo.get_by_id(payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Conditional payment not found")
        # Only sender or recipient can view
        if payment.from_agent_id != agent.id and payment.to_agent_id != agent.id:
            raise HTTPException(status_code=403, detail="Not authorized to view this payment")
        from sthrip.services.conditional_payment_service import _payment_to_dict
        return _payment_to_dict(payment)


@router.delete("/{payment_id}")
async def cancel_conditional_payment(
    payment_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Cancel a PENDING conditional payment and refund the sender."""
    with get_db() as db:
        try:
            result = ConditionalPaymentService.cancel(
                db=db,
                agent_id=agent.id,
                payment_id=payment_id,
            )
            return result
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)


@router.post("/{payment_id}/trigger")
async def trigger_conditional_payment(
    payment_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Trigger a webhook-type conditional payment. Only the sender can trigger."""
    with get_db() as db:
        try:
            result = ConditionalPaymentService.trigger_webhook(
                db=db,
                payment_id=payment_id,
                agent_id=agent.id,
            )
            return result
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)
