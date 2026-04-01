"""Recurring subscriptions endpoints.

Routes (prefix: /v2/subscriptions):
  POST   /           — create subscription (authenticated as from_agent)
  GET    /           — list subscriptions for the current agent
  GET    /{id}       — get subscription detail
  PATCH  /{id}       — update amount / interval (sender only)
  DELETE /{id}       — cancel subscription (either participant)
"""

import logging
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from sthrip.db.database import get_db
from sthrip.db.models import Agent, RecurringInterval
from sthrip.db.recurring_repo import RecurringPaymentRepository
from sthrip.services.recurring_service import RecurringService
from api.deps import get_current_agent
from api.schemas_subscriptions import (
    SubscriptionCreateRequest,
    SubscriptionUpdateRequest,
    SubscriptionResponse,
)

logger = logging.getLogger("sthrip.subscriptions")

router = APIRouter(prefix="/v2/subscriptions", tags=["subscriptions"])

_svc = RecurringService()


def _handle_service_error(exc: Exception):
    """Map RecurringService exceptions to HTTP responses."""
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc))
    raise exc


@router.post("", status_code=201)
async def create_subscription(
    req: SubscriptionCreateRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Create a recurring payment subscription.

    The authenticated agent becomes the sender (from_agent).
    """
    with get_db() as db:
        # Resolve receiver by name
        receiver = db.query(Agent).filter(Agent.agent_name == req.to_agent_name).first()
        if receiver is None:
            raise HTTPException(status_code=404, detail="Recipient agent not found")
        if not receiver.is_active:
            raise HTTPException(status_code=400, detail="Recipient agent is not active")

        interval = RecurringInterval(req.interval)
        try:
            result = _svc.create_subscription(
                db=db,
                from_agent_id=agent.id,
                to_agent_id=receiver.id,
                amount=req.amount,
                interval=interval,
                max_payments=req.max_payments,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

        return result


@router.get("")
async def list_subscriptions(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    agent: Agent = Depends(get_current_agent),
):
    """List all subscriptions (as sender or receiver) for the current agent."""
    with get_db() as db:
        repo = RecurringPaymentRepository(db)
        items, total = repo.list_by_agent(agent.id, limit=limit, offset=offset)
        return {
            "items": [_payment_to_response(p) for p in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


@router.get("/{subscription_id}")
async def get_subscription(
    subscription_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Get details of a specific subscription.

    The current agent must be a participant (sender or receiver).
    """
    with get_db() as db:
        repo = RecurringPaymentRepository(db)
        payment = repo.get_by_id(subscription_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if agent.id not in (payment.from_agent_id, payment.to_agent_id):
            raise HTTPException(status_code=403, detail="Access denied")
        return _payment_to_response(payment)


@router.patch("/{subscription_id}")
async def update_subscription(
    subscription_id: UUID,
    req: SubscriptionUpdateRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Update amount and/or interval. Only the sender may update."""
    with get_db() as db:
        interval = RecurringInterval(req.interval) if req.interval else None
        try:
            result = _svc.update_subscription(
                db=db,
                payment_id=subscription_id,
                agent_id=agent.id,
                amount=req.amount,
                interval=interval,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

        return result


@router.delete("/{subscription_id}")
async def cancel_subscription(
    subscription_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Cancel a recurring subscription. Either participant may cancel."""
    with get_db() as db:
        try:
            result = _svc.cancel_subscription(
                db=db,
                payment_id=subscription_id,
                agent_id=agent.id,
            )
        except (LookupError, PermissionError, ValueError) as exc:
            _handle_service_error(exc)

        return result


def _payment_to_response(payment) -> dict:
    """Convert a RecurringPayment ORM object to a response dict."""
    interval_val = (
        payment.interval.value
        if hasattr(payment.interval, "value")
        else payment.interval
    )
    return {
        "id": str(payment.id),
        "from_agent_id": str(payment.from_agent_id),
        "to_agent_id": str(payment.to_agent_id),
        "amount": str(payment.amount),
        "interval": interval_val,
        "next_payment_at": payment.next_payment_at.isoformat() if payment.next_payment_at else None,
        "last_payment_at": payment.last_payment_at.isoformat() if payment.last_payment_at else None,
        "total_paid": str(payment.total_paid or Decimal("0")),
        "max_payments": payment.max_payments,
        "payments_made": payment.payments_made or 0,
        "is_active": payment.is_active,
        "created_at": payment.created_at.isoformat() if payment.created_at else None,
        "cancelled_at": payment.cancelled_at.isoformat() if payment.cancelled_at else None,
    }
