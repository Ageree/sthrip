"""
WebhookRepository — data-access layer for WebhookEvent records.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from uuid import UUID

from sqlalchemy.orm import Session

from . import models
from ._repo_base import _MAX_QUERY_LIMIT


class WebhookRepository:
    """Webhook event data access"""

    def __init__(self, db: Session):
        self.db = db

    def create_event(
        self,
        agent_id: UUID,
        event_type: str,
        payload: Dict[str, Any]
    ) -> models.WebhookEvent:
        """Create new webhook event"""
        event = models.WebhookEvent(
            agent_id=agent_id,
            event_type=event_type,
            payload=payload,
            status=models.WebhookStatus.PENDING,
            attempt_count=0,
            max_attempts=5,
            next_attempt_at=datetime.now(timezone.utc)
        )
        self.db.add(event)
        self.db.flush()
        return event

    def get_by_id(self, event_id: UUID) -> Optional[models.WebhookEvent]:
        """Get webhook event by ID (non-locking read)."""
        return self.db.query(models.WebhookEvent).filter(
            models.WebhookEvent.id == event_id
        ).first()

    def get_by_id_for_update(self, event_id: UUID) -> Optional[models.WebhookEvent]:
        """Get webhook event by ID with an exclusive row lock (FOR UPDATE).

        Use this in process_event() Phase 1 to prevent two workers from
        reading the same row as 'pending' and both delivering it (TOCTOU).
        Unlike get_pending_events() which uses skip_locked (worker skips busy
        rows), this blocks until the lock is acquired — appropriate when
        directly targeting a specific event ID.
        """
        return self.db.query(models.WebhookEvent).filter(
            models.WebhookEvent.id == event_id
        ).with_for_update().first()

    def get_pending_events(self, limit: int = 100) -> List[models.WebhookEvent]:
        """Get events pending delivery with row-level lock to prevent duplicate delivery."""
        limit = min(limit, _MAX_QUERY_LIMIT)
        return self.db.query(models.WebhookEvent).filter(
            models.WebhookEvent.status.in_([
                models.WebhookStatus.PENDING,
                models.WebhookStatus.RETRYING
            ]),
            models.WebhookEvent.next_attempt_at <= datetime.now(timezone.utc),
            models.WebhookEvent.attempt_count < models.WebhookEvent.max_attempts
        ).order_by(
            models.WebhookEvent.created_at
        ).with_for_update(skip_locked=True).limit(limit).all()

    def mark_delivered(self, event_id: UUID, response_code: int, response_body: str):
        """Mark event as delivered"""
        self.db.query(models.WebhookEvent).filter(
            models.WebhookEvent.id == event_id
        ).update({
            "status": models.WebhookStatus.DELIVERED,
            "last_response_code": response_code,
            "last_response_body": f"status={response_code}",
            "delivered_at": datetime.now(timezone.utc)
        })

    def mark_failed(self, event_id: UUID, error: str):
        """Mark event as failed (max attempts reached)"""
        self.db.query(models.WebhookEvent).filter(
            models.WebhookEvent.id == event_id
        ).update({
            "status": models.WebhookStatus.FAILED,
            "last_error": error[:1000]
        })

    def schedule_retry(self, event_id: UUID, error: str):
        """Schedule retry with exponential backoff.

        Uses FOR UPDATE to prevent a concurrent worker from reading the
        same row between our SELECT and UPDATE (TOCTOU).
        """
        event = self.db.query(models.WebhookEvent).filter(
            models.WebhookEvent.id == event_id
        ).with_for_update().first()

        if event:
            attempt = event.attempt_count + 1
            delays = [60, 300, 900, 1800, 3600]
            delay = delays[min(attempt - 1, len(delays) - 1)]

            self.db.query(models.WebhookEvent).filter(
                models.WebhookEvent.id == event_id
            ).update({
                "status": models.WebhookStatus.RETRYING,
                "attempt_count": attempt,
                "last_error": error[:1000] if error else None,
                "next_attempt_at": datetime.now(timezone.utc) + timedelta(seconds=delay)
            })
