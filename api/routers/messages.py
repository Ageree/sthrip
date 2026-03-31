"""E2E encrypted agent messaging endpoints.

The hub NEVER sees plaintext -- it only stores and relays ciphertext.
Agents exchange Curve25519 public keys and use NaCl Box for encryption.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from api.deps import get_current_agent
from api.schemas import (
    EncryptionKeyRequest,
    MessageSendRequest,
    MessageResponse,
)
from sthrip.services.messaging_service import (
    MessagingService,
    MessageSizeError,
    InboxFullError,
    RecipientNotFoundError,
)

logger = logging.getLogger("sthrip")

router = APIRouter(prefix="/v2", tags=["messages"])

_messaging_service = MessagingService()


@router.put("/me/encryption-key")
async def register_encryption_key(
    body: EncryptionKeyRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Register or update the agent's Curve25519 public key for E2E messaging."""
    with get_db() as db:
        db_agent = db.query(Agent).filter(Agent.id == agent.id).first()
        if db_agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        db_agent.encryption_public_key = body.public_key
        db.flush()
        return {"status": "ok", "public_key": body.public_key}


@router.get("/agents/{agent_id}/public-key")
async def get_public_key(
    agent_id: str,
    _agent: Agent = Depends(get_current_agent),
):
    """Retrieve another agent's Curve25519 public key for E2E encryption."""
    try:
        parsed_id = UUID(agent_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Agent not found")

    with get_db() as db:
        target = (
            db.query(Agent)
            .filter(Agent.id == parsed_id, Agent.is_active.is_(True))
            .first()
        )
        if target is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        if target.encryption_public_key is None:
            raise HTTPException(
                status_code=404,
                detail="Agent has not registered an encryption key",
            )
        return {
            "agent_id": str(target.id),
            "public_key": target.encryption_public_key,
        }


@router.post("/messages/send", status_code=201)
async def send_message(
    body: MessageSendRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Relay an encrypted message to another agent.

    The hub stores only ciphertext -- it never has access to the
    shared secret or plaintext content.
    """
    with get_db() as db:
        try:
            relay = _messaging_service.relay_message(
                db=db,
                from_agent_id=str(agent.id),
                to_agent_id=body.to_agent_id,
                ciphertext=body.ciphertext,
                nonce=body.nonce,
                sender_public_key=body.sender_public_key,
                payment_id=body.payment_id,
            )
            return {
                "status": "sent",
                "message_id": str(relay.id),
                "expires_at": relay.expires_at.isoformat(),
            }
        except MessageSizeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RecipientNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except InboxFullError as e:
            raise HTTPException(status_code=429, detail=str(e))


@router.get("/messages/inbox")
async def get_inbox(
    agent: Agent = Depends(get_current_agent),
):
    """Fetch all undelivered encrypted messages for the authenticated agent.

    Messages are marked as delivered upon retrieval and will not be
    returned again. Messages expire after 24 hours if not fetched.
    """
    with get_db() as db:
        messages = _messaging_service.get_inbox(db, str(agent.id))
        return {
            "messages": [
                MessageResponse(
                    id=str(msg.id),
                    from_agent_id=str(msg.from_agent_id),
                    ciphertext=msg.ciphertext,
                    nonce=msg.nonce,
                    sender_public_key=msg.sender_public_key,
                    payment_id=msg.payment_id,
                    created_at=msg.created_at.isoformat() if msg.created_at else "",
                ).model_dump()
                for msg in messages
            ],
            "count": len(messages),
        }
