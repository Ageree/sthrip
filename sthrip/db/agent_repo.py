"""
AgentRepository — data-access layer for Agent records.

NOTE on immutability: ORM objects are inherently mutable (SQLAlchemy's
unit-of-work pattern requires in-place mutation for change tracking).
This is an accepted exception to the project's immutability guidelines —
all other layers pass immutable dicts/Pydantic models.
"""

import hashlib
import hmac as _hmac
import secrets
import threading
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Dict
from uuid import UUID

from sqlalchemy.orm import Session, joinedload
from sqlalchemy import desc

from . import models
from ._repo_base import _MAX_QUERY_LIMIT


def _get_hmac_secret() -> str:
    from sthrip.config import get_settings
    return get_settings().api_key_hmac_secret


class AgentRepository:
    """Agent data access"""

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _hash_api_key(api_key: str) -> str:
        """Hash API key using HMAC-SHA256 with server secret."""
        secret = _get_hmac_secret()
        return _hmac.new(secret.encode(), api_key.encode(), hashlib.sha256).hexdigest()

    def create_agent(
        self,
        agent_name: str,
        webhook_url: Optional[str] = None,
        privacy_level: str = "medium",
        tier: str = "free"
    ) -> Tuple[models.Agent, Dict[str, str]]:
        """Create new agent with API key. Returns (agent, credentials_dict)."""
        api_key = f"sk_{secrets.token_hex(32)}"
        api_key_hash = self._hash_api_key(api_key)

        webhook_secret = f"whsec_{secrets.token_hex(24)}"

        from sthrip.crypto import encrypt_value
        encrypted_secret = encrypt_value(webhook_secret)

        agent = models.Agent(
            agent_name=agent_name,
            api_key_hash=api_key_hash,
            webhook_url=webhook_url,
            webhook_secret=encrypted_secret,
            privacy_level=privacy_level,
            tier=tier,
            is_active=True
        )

        self.db.add(agent)
        self.db.flush()

        reputation = models.AgentReputation(agent_id=agent.id)
        self.db.add(reputation)

        credentials = {
            "api_key": api_key,
            "webhook_secret": webhook_secret,
        }

        return agent, credentials

    def get_by_api_key(self, api_key: str) -> Optional[models.Agent]:
        """Get agent by API key"""
        api_key_hash = self._hash_api_key(api_key)
        return self.db.query(models.Agent).filter(
            models.Agent.api_key_hash == api_key_hash,
            models.Agent.is_active == True
        ).first()

    def get_by_id(self, agent_id: UUID) -> Optional[models.Agent]:
        """Get agent by ID"""
        return self.db.query(models.Agent).filter(
            models.Agent.id == agent_id
        ).first()

    def get_by_name(self, agent_name: str) -> Optional[models.Agent]:
        """Get agent by name"""
        return self.db.query(models.Agent).filter(
            models.Agent.agent_name == agent_name
        ).first()

    def list_agents(
        self,
        tier: Optional[str] = None,
        is_active: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[models.Agent]:
        """List agents with optional filters"""
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(models.Agent).options(
            joinedload(models.Agent.reputation)
        )

        if tier:
            query = query.filter(models.Agent.tier == tier)
        if is_active is not None:
            query = query.filter(models.Agent.is_active == is_active)

        return query.order_by(desc(models.Agent.created_at)).offset(offset).limit(limit).all()

    # Throttle update_last_seen: at most once per 60 seconds per agent.
    _last_seen_cache: dict = {}
    _last_seen_lock = threading.Lock()
    _LAST_SEEN_THROTTLE = 60  # seconds
    _LAST_SEEN_MAX_ENTRIES = 5000  # evict oldest when exceeded

    def update_last_seen(self, agent_id: UUID):
        """Update last seen timestamp (throttled to avoid write amplification)."""
        import time as _time

        now = _time.time()
        cache_key = str(agent_id)

        with AgentRepository._last_seen_lock:
            last_update = self._last_seen_cache.get(cache_key, 0)

            if now - last_update < self._LAST_SEEN_THROTTLE:
                return

            if len(AgentRepository._last_seen_cache) >= self._LAST_SEEN_MAX_ENTRIES:
                sorted_keys = sorted(
                    AgentRepository._last_seen_cache,
                    key=AgentRepository._last_seen_cache.get,
                )
                for k in sorted_keys[: len(sorted_keys) // 2]:
                    AgentRepository._last_seen_cache.pop(k, None)

            AgentRepository._last_seen_cache[cache_key] = now

        self.db.query(models.Agent).filter(
            models.Agent.id == agent_id
        ).update(
            {"last_seen_at": datetime.now(timezone.utc)},
            synchronize_session="evaluate",
        )

    def get_webhook_secret(self, agent_id: UUID) -> Optional[str]:
        """Get decrypted webhook secret for agent."""
        from sthrip.crypto import decrypt_value
        agent = self.get_by_id(agent_id)
        if not agent or not agent.webhook_secret:
            return None
        try:
            return decrypt_value(agent.webhook_secret)
        except Exception as e:
            import logging
            logging.getLogger("sthrip").critical(
                "Failed to decrypt webhook secret for agent %s: %s. "
                "This may indicate key rotation without data migration.",
                agent_id, e,
            )
            raise ValueError(
                f"Cannot decrypt webhook secret for agent {agent_id}. "
                "Contact system administrator."
            ) from e

    def update_wallet_addresses(
        self,
        agent_id: UUID,
        xmr_address: Optional[str] = None,
        base_address: Optional[str] = None,
        solana_address: Optional[str] = None
    ):
        """Update agent wallet addresses"""
        updates = {}
        if xmr_address:
            updates["xmr_address"] = xmr_address
        if base_address:
            updates["base_address"] = base_address
        if solana_address:
            updates["solana_address"] = solana_address

        if updates:
            self.db.query(models.Agent).filter(
                models.Agent.id == agent_id
            ).update(updates, synchronize_session="evaluate")
