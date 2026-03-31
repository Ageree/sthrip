"""E2E encrypted agent messaging service.

The hub NEVER sees plaintext -- it only relays ciphertext between agents.
Messages are ephemeral with a 24-hour TTL and automatic expiry.
"""

import base64
import logging
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import List, Union

from sqlalchemy.orm import Session

from sthrip.db.models import Agent, MessageRelay

logger = logging.getLogger("sthrip")

# Limits
MAX_MESSAGE_SIZE_BYTES = 64 * 1024  # 64 KB
MAX_INBOX_PENDING = 100
MESSAGE_TTL_HOURS = 24


class MessageSizeError(Exception):
    """Raised when ciphertext exceeds the 64 KB limit."""
    pass


class InboxFullError(Exception):
    """Raised when recipient has too many undelivered messages."""
    pass


class RecipientNotFoundError(Exception):
    """Raised when the target agent does not exist or is inactive."""
    pass


def _to_uuid(value: Union[str, _uuid.UUID]) -> _uuid.UUID:
    """Coerce a string to uuid.UUID for SQLite compatibility."""
    if isinstance(value, _uuid.UUID):
        return value
    return _uuid.UUID(str(value))


class MessagingService:
    """Relay encrypted messages between agents.

    The hub stores only ciphertext, nonce, and sender public key.
    It never has access to the shared secret or plaintext.
    """

    def relay_message(
        self,
        db: Session,
        from_agent_id: str,
        to_agent_id: str,
        ciphertext: str,
        nonce: str,
        sender_public_key: str,
        payment_id: str = None,
    ) -> MessageRelay:
        """Accept an encrypted message for relay to another agent.

        Args:
            db: Active database session.
            from_agent_id: UUID of the sending agent.
            to_agent_id: UUID of the receiving agent.
            ciphertext: Base64-encoded NaCl Box ciphertext.
            nonce: Base64-encoded 24-byte nonce.
            sender_public_key: Base64-encoded Curve25519 public key of sender.
            payment_id: Optional payment reference.

        Returns:
            The created MessageRelay record.

        Raises:
            MessageSizeError: If decoded ciphertext exceeds 64 KB.
            RecipientNotFoundError: If recipient agent is missing or inactive.
            InboxFullError: If recipient has >= 100 undelivered messages.
        """
        # Decode and validate ciphertext size
        try:
            raw_ciphertext = base64.b64decode(ciphertext)
        except Exception:
            raise MessageSizeError("Invalid base64 ciphertext")

        size_bytes = len(raw_ciphertext)
        if size_bytes > MAX_MESSAGE_SIZE_BYTES:
            raise MessageSizeError(
                f"Message size {size_bytes} bytes exceeds limit of "
                f"{MAX_MESSAGE_SIZE_BYTES} bytes (64 KB)"
            )

        # Validate nonce is valid base64
        try:
            base64.b64decode(nonce)
        except Exception:
            raise MessageSizeError("Invalid base64 nonce")

        # Validate sender_public_key is valid base64
        try:
            base64.b64decode(sender_public_key)
        except Exception:
            raise MessageSizeError("Invalid base64 sender_public_key")

        # Coerce IDs to UUID for SQLite compatibility
        from_uuid = _to_uuid(from_agent_id)
        to_uuid = _to_uuid(to_agent_id)

        # Check recipient exists and is active
        recipient = (
            db.query(Agent)
            .filter(Agent.id == to_uuid, Agent.is_active.is_(True))
            .first()
        )
        if recipient is None:
            raise RecipientNotFoundError("Recipient agent not found or inactive")

        # Check inbox capacity (pending = undelivered + not expired)
        now = datetime.now(timezone.utc)
        pending_count = (
            db.query(MessageRelay)
            .filter(
                MessageRelay.to_agent_id == to_uuid,
                MessageRelay.delivered_at.is_(None),
                MessageRelay.expires_at > now,
            )
            .count()
        )
        if pending_count >= MAX_INBOX_PENDING:
            raise InboxFullError(
                f"Recipient inbox is full ({MAX_INBOX_PENDING} pending messages)"
            )

        # Create the relay record with 24h TTL
        relay = MessageRelay(
            from_agent_id=from_uuid,
            to_agent_id=to_uuid,
            payment_id=payment_id,
            ciphertext=ciphertext,
            nonce=nonce,
            sender_public_key=sender_public_key,
            size_bytes=size_bytes,
            expires_at=now + timedelta(hours=MESSAGE_TTL_HOURS),
        )
        db.add(relay)
        db.flush()

        logger.info(
            "Message relayed from=%s to=%s size=%d bytes",
            from_agent_id,
            to_agent_id,
            size_bytes,
        )
        return relay

    def get_inbox(
        self,
        db: Session,
        agent_id: str,
    ) -> List[MessageRelay]:
        """Fetch undelivered, non-expired messages for an agent.

        Messages are returned in creation order (oldest first) and
        immediately marked as delivered so they are not returned again.

        Args:
            db: Active database session.
            agent_id: UUID of the agent fetching their inbox.

        Returns:
            List of MessageRelay records ordered by created_at ASC.
        """
        agent_uuid = _to_uuid(agent_id)
        now = datetime.now(timezone.utc)
        messages = (
            db.query(MessageRelay)
            .filter(
                MessageRelay.to_agent_id == agent_uuid,
                MessageRelay.delivered_at.is_(None),
                MessageRelay.expires_at > now,
            )
            .order_by(MessageRelay.created_at.asc())
            .all()
        )

        # Mark all as delivered
        for msg in messages:
            msg.delivered_at = now
        db.flush()

        logger.info(
            "Inbox fetched for agent=%s count=%d",
            agent_id,
            len(messages),
        )
        return messages
