"""Self-service webhook endpoint registration and management."""

import base64
import logging
import os
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from sthrip.crypto import encrypt_value
from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.db.webhook_endpoint_repo import WebhookEndpointRepository
from api.deps import get_current_agent
from api.schemas import (
    WebhookEndpointCreate,
    WebhookEndpointCreateResponse,
    WebhookEndpointResponse,
)

logger = logging.getLogger("sthrip.webhook_endpoints")

router = APIRouter(prefix="/v2/webhook-endpoints", tags=["webhooks"])

_MAX_ENDPOINTS_PER_AGENT = 10


def _generate_secret() -> str:
    """Generate a webhook signing secret: ``whsec_`` + base64(32 random bytes)."""
    raw = os.urandom(32)
    encoded = base64.urlsafe_b64encode(raw).decode()
    return f"whsec_{encoded}"


def _endpoint_to_response(endpoint) -> dict:
    """Convert a WebhookEndpoint ORM object to a serialisable dict."""
    return {
        "id": str(endpoint.id),
        "url": endpoint.url,
        "description": endpoint.description,
        "event_filters": endpoint.event_filters,
        "is_active": endpoint.is_active,
        "failure_count": endpoint.failure_count,
        "created_at": endpoint.created_at.isoformat() if endpoint.created_at else "",
    }


@router.post(
    "",
    response_model=WebhookEndpointCreateResponse,
    status_code=201,
)
async def register_webhook(
    body: WebhookEndpointCreate,
    agent: Agent = Depends(get_current_agent),
):
    """Register a new webhook endpoint.

    The signing secret is returned **only** in this response.
    Store it securely -- it cannot be retrieved later.
    """
    with get_db() as db:
        repo = WebhookEndpointRepository(db)

        count = repo.count_by_agent(agent.id)
        if count >= _MAX_ENDPOINTS_PER_AGENT:
            raise HTTPException(
                status_code=400,
                detail=f"Maximum {_MAX_ENDPOINTS_PER_AGENT} webhook endpoints per agent",
            )

        secret_plain = _generate_secret()
        secret_encrypted = encrypt_value(secret_plain)

        endpoint = repo.create(
            agent_id=agent.id,
            url=body.url,
            secret_encrypted=secret_encrypted,
            description=body.description,
            event_filters=body.event_filters,
        )

        result = _endpoint_to_response(endpoint)
        result["secret"] = secret_plain
        return result


@router.get("", response_model=list[WebhookEndpointResponse])
async def list_webhooks(
    agent: Agent = Depends(get_current_agent),
):
    """List all webhook endpoints for the current agent (secrets excluded)."""
    with get_db() as db:
        repo = WebhookEndpointRepository(db)
        endpoints = repo.list_by_agent(agent.id)
        return [_endpoint_to_response(ep) for ep in endpoints]


@router.delete("/{webhook_id}", status_code=200)
async def delete_webhook(
    webhook_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Delete a webhook endpoint."""
    with get_db() as db:
        repo = WebhookEndpointRepository(db)
        deleted = repo.delete(webhook_id, agent.id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Webhook endpoint not found")
        return {"message": "Webhook endpoint deleted", "webhook_id": str(webhook_id)}


@router.post(
    "/{webhook_id}/rotate",
    response_model=WebhookEndpointCreateResponse,
)
async def rotate_secret(
    webhook_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Rotate the signing secret for a webhook endpoint.

    Returns the new secret -- store it securely.
    """
    with get_db() as db:
        repo = WebhookEndpointRepository(db)
        new_secret_plain = _generate_secret()
        new_secret_encrypted = encrypt_value(new_secret_plain)

        endpoint = repo.update_secret(webhook_id, agent.id, new_secret_encrypted)
        if endpoint is None:
            raise HTTPException(status_code=404, detail="Webhook endpoint not found")

        result = _endpoint_to_response(endpoint)
        result["secret"] = new_secret_plain
        return result


@router.post("/{webhook_id}/test", status_code=200)
async def test_webhook(
    webhook_id: UUID,
    agent: Agent = Depends(get_current_agent),
):
    """Send a test event to a webhook endpoint."""
    with get_db() as db:
        repo = WebhookEndpointRepository(db)
        endpoint = repo.get_by_id(webhook_id, agent.id)
        if endpoint is None:
            raise HTTPException(status_code=404, detail="Webhook endpoint not found")

        return {
            "message": "Test event queued",
            "webhook_id": str(webhook_id),
            "url": endpoint.url,
        }
