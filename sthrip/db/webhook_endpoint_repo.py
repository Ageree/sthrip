"""
WebhookEndpointRepository -- data-access layer for self-service webhook endpoints.
"""

from typing import Optional, List
from uuid import UUID

from sqlalchemy.orm import Session

from .models import WebhookEndpoint


class WebhookEndpointRepository:
    """CRUD operations for agent webhook endpoints."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        agent_id: UUID,
        url: str,
        secret_encrypted: str,
        description: Optional[str] = None,
        event_filters: Optional[List[str]] = None,
    ) -> WebhookEndpoint:
        """Create a new webhook endpoint for an agent."""
        endpoint = WebhookEndpoint(
            agent_id=agent_id,
            url=url,
            secret_encrypted=secret_encrypted,
            description=description,
            event_filters=event_filters,
        )
        self.db.add(endpoint)
        self.db.flush()
        return endpoint

    def list_by_agent(self, agent_id: UUID) -> List[WebhookEndpoint]:
        """List all webhook endpoints for an agent."""
        return (
            self.db.query(WebhookEndpoint)
            .filter(WebhookEndpoint.agent_id == agent_id)
            .order_by(WebhookEndpoint.created_at.desc())
            .all()
        )

    def get_by_id(
        self, webhook_id: UUID, agent_id: UUID
    ) -> Optional[WebhookEndpoint]:
        """Get a webhook endpoint by ID, scoped to the owning agent."""
        return (
            self.db.query(WebhookEndpoint)
            .filter(
                WebhookEndpoint.id == webhook_id,
                WebhookEndpoint.agent_id == agent_id,
            )
            .first()
        )

    def delete(self, webhook_id: UUID, agent_id: UUID) -> bool:
        """Delete a webhook endpoint. Returns True if a row was deleted."""
        rows = (
            self.db.query(WebhookEndpoint)
            .filter(
                WebhookEndpoint.id == webhook_id,
                WebhookEndpoint.agent_id == agent_id,
            )
            .delete()
        )
        self.db.flush()
        return rows > 0

    def count_by_agent(self, agent_id: UUID) -> int:
        """Return the number of webhook endpoints owned by an agent."""
        return (
            self.db.query(WebhookEndpoint)
            .filter(WebhookEndpoint.agent_id == agent_id)
            .count()
        )

    def update_secret(
        self,
        webhook_id: UUID,
        agent_id: UUID,
        new_secret_encrypted: str,
    ) -> Optional[WebhookEndpoint]:
        """Rotate the signing secret for a webhook endpoint.

        Returns the updated endpoint or None if not found.
        """
        endpoint = self.get_by_id(webhook_id, agent_id)
        if endpoint is None:
            return None
        endpoint.secret_encrypted = new_secret_encrypted
        self.db.flush()
        return endpoint
