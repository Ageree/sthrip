"""Webhook event endpoints."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query

from sthrip.db.database import get_db
from sthrip.db.models import Agent, WebhookEvent, WebhookStatus
from api.deps import get_current_agent

router = APIRouter(prefix="/v2/webhooks", tags=["webhooks"])


@router.get("/events")
async def list_webhook_events(
    limit: int = Query(default=50, ge=1, le=500),
    agent: Agent = Depends(get_current_agent),
):
    """List recent webhook events for the current agent"""
    with get_db() as db:
        events = (
            db.query(WebhookEvent)
            .filter(WebhookEvent.agent_id == agent.id)
            .order_by(WebhookEvent.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": str(e.id),
                "event_type": e.event_type,
                "status": e.status.value if hasattr(e.status, "value") else str(e.status),
                "attempt_count": e.attempt_count,
                "last_error": e.last_error,
                "delivered_at": e.delivered_at.isoformat() if e.delivered_at else None,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ]


@router.post("/events/{event_id}/retry")
async def retry_webhook_event(
    event_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Manually retry a failed webhook event"""
    with get_db() as db:
        event = db.query(WebhookEvent).filter(
            WebhookEvent.id == event_id,
            WebhookEvent.agent_id == agent.id,
        ).first()

        if not event:
            raise HTTPException(status_code=404, detail="Webhook event not found")

        if event.status not in (WebhookStatus.FAILED, WebhookStatus.RETRYING):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot retry event with status {event.status.value}",
            )

        event.status = WebhookStatus.PENDING
        event.attempt_count = max((event.attempt_count or 0) - 1, 0)
        event.next_attempt_at = datetime.now(timezone.utc)

    return {"message": "Event queued for retry", "event_id": str(event_id)}
