"""
WebhookEndpointRepository -- data-access layer for self-service webhook endpoints.

Sprint 5 (anonymity-hardening): URLs are encrypted at rest using the
WEBHOOK_ENCRYPTION_KEY Fernet key (same key as ``secret_encrypted``).
Callers pass plaintext URLs to ``create()`` / ``find_by_agent_and_url()``;
the repo handles encryption/decryption internally. The plaintext value is
never stored, never logged.
"""

import logging
from typing import Optional, List
from uuid import UUID

from sqlalchemy.orm import Session

from .models import WebhookEndpoint
from ..crypto import encrypt_value, decrypt_value

logger = logging.getLogger("sthrip.webhook_endpoint_repo")


class WebhookEndpointRepository:
    """CRUD operations for agent webhook endpoints (URL encrypted at rest)."""

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
        """Create a new webhook endpoint for an agent.

        ``url`` is the **plaintext** URL; the repo encrypts it before insert.
        """
        url_encrypted = encrypt_value(url)
        endpoint = WebhookEndpoint(
            agent_id=agent_id,
            url_encrypted=url_encrypted,
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

    # ------------------------------------------------------------------
    # Sprint 5: encrypted-URL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def get_url(endpoint: WebhookEndpoint) -> Optional[str]:
        """Decrypt the endpoint URL.

        Returns ``None`` on any decryption failure rather than raising.
        Callers (e.g. ``WebhookService``) treat ``None`` as a malformed row
        and disable the endpoint -- never deliver to an unknown target.
        """
        if endpoint is None or not endpoint.url_encrypted:
            return None
        try:
            return decrypt_value(endpoint.url_encrypted)
        except Exception:
            logger.warning(
                "Failed to decrypt webhook URL for endpoint %s (agent %s); "
                "endpoint will be treated as unusable.",
                endpoint.id,
                endpoint.agent_id,
            )
            return None

    def find_by_agent_and_url(
        self, agent_id: UUID, url: str
    ) -> Optional[WebhookEndpoint]:
        """Find an existing endpoint for ``(agent_id, plaintext_url)``.

        Implementation: list all endpoints for the agent and decrypt-compare.
        Acceptable because the per-agent list is bounded
        (``_MAX_ENDPOINTS_PER_AGENT = 10`` enforced at the API layer).
        """
        for ep in self.list_by_agent(agent_id):
            if self.get_url(ep) == url:
                return ep
        return None

    def upsert_by_url(
        self,
        agent_id: UUID,
        url: str,
        secret_encrypted: str,
        description: Optional[str] = None,
        event_filters: Optional[List[str]] = None,
    ) -> WebhookEndpoint:
        """Create-or-return-existing endpoint by ``(agent_id, plaintext_url)``.

        If an endpoint with the same plaintext URL already exists for this
        agent, it is returned unchanged (idempotent). Otherwise a fresh
        endpoint is created. Used by the legacy ``PATCH /v2/me/settings``
        shim and the migration backfill.
        """
        existing = self.find_by_agent_and_url(agent_id, url)
        if existing is not None:
            return existing
        return self.create(
            agent_id=agent_id,
            url=url,
            secret_encrypted=secret_encrypted,
            description=description,
            event_filters=event_filters,
        )
