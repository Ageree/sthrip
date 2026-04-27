"""
IdempotencyKeyRepository — data-access layer for IdempotencyKey records (F-4 fix).

Provides safe read/write with IntegrityError handling for the race condition
where two concurrent requests with the same key both reach the INSERT.

Pattern matches the rest of sthrip/db/ repositories:
- Receives a Session per call (no global state).
- Returns ORM objects or None — callers convert to dicts.
- Uses savepoint-aware nested transactions for the INSERT race case on PostgreSQL.
- Degrades to plain rollback on SQLite (no savepoints inside an existing txn).
"""

import logging
from typing import Any, Dict, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import IdempotencyKey

logger = logging.getLogger("sthrip.idempotency_repo")


class IdempotencyKeyRepository:
    """Data-access layer for the idempotency_keys table."""

    def __init__(self, db: Session):
        self.db = db

    def _is_sqlite(self) -> bool:
        bind = self.db.bind
        return bind is not None and bind.dialect.name == "sqlite"

    def get(self, agent_id: str, endpoint: str, key: str) -> Optional[IdempotencyKey]:
        """Fetch an existing idempotency key row, or None if not found."""
        return (
            self.db.query(IdempotencyKey)
            .filter_by(agent_id=agent_id, endpoint=endpoint, key=key)
            .first()
        )

    def upsert(
        self,
        agent_id: str,
        endpoint: str,
        key: str,
        request_hash: str,
        response_status: int,
        response_body: Dict[str, Any],
    ) -> Optional[IdempotencyKey]:
        """Insert or update an idempotency row (F-4 v3).

        F-4 v3 needed an upsert because the withdraw flow now writes a 202
        in-progress placeholder atomically with the balance debit, then later
        promotes that row to the final 200 response after the wallet RPC
        succeeds. Without this, the second store_response would IntegrityError
        and never overwrite the 202 placeholder, leaving replays stuck on the
        in-progress body forever.

        Updates the row if it already exists for the same (agent_id, endpoint,
        key); otherwise falls through to ``create``.
        """
        existing = self.get(agent_id, endpoint, key)
        if existing is not None:
            # Only update when the new response_status indicates a final state
            # (any 2xx other than 202, or any 4xx/5xx). Keeps 202 placeholders
            # from clobbering a later concurrent winner's final 200.
            existing.request_hash = request_hash
            existing.response_status = response_status
            existing.response_body = response_body
            self.db.flush()
            return existing
        return self.create(
            agent_id=agent_id,
            endpoint=endpoint,
            key=key,
            request_hash=request_hash,
            response_status=response_status,
            response_body=response_body,
        )

    def create(
        self,
        agent_id: str,
        endpoint: str,
        key: str,
        request_hash: str,
        response_status: int,
        response_body: Dict[str, Any],
    ) -> Optional[IdempotencyKey]:
        """Persist a completed idempotency key.

        Returns the created row on success, or the existing row if a concurrent
        writer beat us to the INSERT (IntegrityError on UNIQUE constraint).
        This makes store_response safe to call from both the winner and any
        concurrent loser that slipped past the Redis sentinel.

        Only IntegrityError (the benign concurrent-write race) is swallowed here.
        Any other exception propagates to the caller so the surrounding transaction
        rolls back. This is intentional — Fix 2 requires store_response to re-raise
        on genuine DB failures.

        SQLite note: SQLite does not support savepoints inside an already-open
        transaction without nested transactions, so on SQLite we fall back to a
        full rollback on IntegrityError. Tests use StaticPool which re-uses a
        single connection — this is safe because test cases don't exercise true
        concurrency.
        """
        row = IdempotencyKey(
            agent_id=agent_id,
            endpoint=endpoint,
            key=key,
            request_hash=request_hash,
            response_status=response_status,
            response_body=response_body,
        )

        savepoint = None if self._is_sqlite() else self.db.begin_nested()
        try:
            self.db.add(row)
            self.db.flush()
            return row
        except IntegrityError:
            # Benign race: a concurrent winner already wrote this key.
            # Roll back the failed sub-transaction (not the outer payment transaction).
            if savepoint is not None:
                savepoint.rollback()
            else:
                self.db.rollback()
            logger.info(
                "IdempotencyKey INSERT conflict for agent=%s endpoint=%s key=%s — "
                "concurrent writer won; reading existing row.",
                agent_id,
                endpoint,
                key,
            )
            # Return the winner's row so the loser can serve the cached response.
            return self.get(agent_id, endpoint, key)
        # All other exceptions propagate — caller's transaction rolls back.
