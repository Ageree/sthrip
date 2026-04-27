"""
Idempotency key service (F-4 fix — retry).

Architecture
------------
PostgreSQL (via IdempotencyKey table) is the *authoritative* store.
Redis is a *write-through hot-path cache* (optional — degrades to DB-only).

Before this fix, keys lived only in Redis with a 24-hour TTL. A client
retaining a long-lived key and replaying >24 h later would find Redis empty,
and the request would be processed as new — double-charging the agent.

After this fix:
  - ``try_reserve`` writes a Redis sentinel (NX) as the in-flight lock.
  - On Redis NX success (cache miss), it checks the DB for a completed row.
    If found: backfill Redis and return the cached response (F-4 fix path).
  - ``store_response`` writes the DB row first (authoritative), then writes
    the Redis cache (write-through). Redis TTL is intentionally short (1 h)
    — it is only a performance cache, not the source of truth.
  - The DB row has no expiry; retention is driven by the
    ``idempotency_db_retention_days`` setting (default 90 d).

Fix 1: single try_reserve call per request
------------------------------------------
Routers MUST call ``try_reserve(..., db=db, request_hash=req_hash)`` exactly
ONCE per request, INSIDE the open ``with get_db() as db:`` block.  The
previous two-call pattern (once outside, once inside) caused the first call
to set a Redis sentinel and the second call to see its own sentinel and raise
409 — blocking all first-time requests with an Idempotency-Key header.

Fix 2: store_response re-raises on DB failure
---------------------------------------------
If the idempotency INSERT fails for any reason other than the benign
UNIQUE-violation race (two concurrent writers — handled inside
IdempotencyKeyRepository.create), the exception propagates.  This causes the
surrounding ``with get_db() as db:`` context to roll back the entire payment
transaction, so the payment and the idempotency row are always committed
atomically.  The client will receive a 500 and can safely retry.

Contract: "If store_response raises, the payment has NOT been committed.
Client retries normally; the next try_reserve will find Redis empty and DB
empty and proceed as a fresh request."

Scoping
-------
Keys are scoped by (agent_id, endpoint, key) matching the existing Redis key
format ``idempotency:{agent_id}:{endpoint}:{sha256(key)}``. This preserves
the three-dimensional scoping: the same key value on /hub-routing and /withdraw
are independent — changing to (agent_id, key) would be a silent behaviour
change.

Session injection
-----------------
``try_reserve`` and ``store_response`` accept an optional ``db: Session``
parameter. Callers that already hold a DB session (routers) SHOULD pass it;
this makes the idempotency INSERT part of the same transaction as the payment,
eliminating the window where the payment commits but the idempotency write
fails.

Callers that do NOT hold a session (legacy ``get_cached_response()`` usage in
background tasks) may omit ``db``; in that case the DB path is skipped and
only Redis is consulted (identical to pre-fix behaviour for those paths).
"""

import hashlib
import json
import threading
import time
import logging
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from sthrip.config import get_settings

logger = logging.getLogger("sthrip.idempotency")

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# Hot-path Redis TTL: 1 h (retry window cache only — DB is authoritative).
_REDIS_TTL_SECONDS = 3600
# Legacy name kept for existing test assertions that check _TTL_SECONDS.
_TTL_SECONDS = _REDIS_TTL_SECONDS

_PROCESSING_SENTINEL = "__processing__"
_EVICTION_INTERVAL = 300  # 5 minutes (local fallback eviction)

# Atomic compare-and-delete: only removes the key if it still holds the sentinel.
# Reuses the same script pattern as sthrip/services/deposit_monitor.py:170.
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


class IdempotencyStore:
    """Atomic idempotency key store with reserve/store pattern (F-4 fix).

    PostgreSQL is the authoritative store; Redis is a write-through cache.
    The store is a process singleton (see ``get_idempotency_store``).
    """

    def __init__(self):
        self._local_cache: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._last_eviction: float = 0.0
        self.use_redis = False

        if REDIS_AVAILABLE:
            redis_url = get_settings().redis_url
            try:
                self.redis = redis.from_url(redis_url, decode_responses=True)
                self.redis.ping()
                self.use_redis = True
            except Exception:
                logger.warning(
                    "Redis unavailable for idempotency store — falling back to local dict. "
                    "Idempotency keys will not be shared across workers.",
                    exc_info=True,
                )
                self.redis = None
        else:
            self.redis = None

    # ── Internal key construction ─────────────────────────────────────────────

    def _key(self, agent_id: str, endpoint: str, idempotency_key: str) -> str:
        hashed = hashlib.sha256(idempotency_key.encode()).hexdigest()
        return f"idempotency:{agent_id}:{endpoint}:{hashed}"

    # ── DB helpers (injected session) ─────────────────────────────────────────

    def _db_get(
        self, db: Session, agent_id: str, endpoint: str, key: str
    ) -> Optional[Any]:
        """Query DB for an existing idempotency key row."""
        from sthrip.db.idempotency_repo import IdempotencyKeyRepository
        repo = IdempotencyKeyRepository(db)
        return repo.get(agent_id, endpoint, key)

    def _db_create(
        self,
        db: Session,
        agent_id: str,
        endpoint: str,
        key: str,
        request_hash: str,
        response_status: int,
        response_body: Dict[str, Any],
    ) -> Optional[Any]:
        """Write a completed idempotency key row to DB.

        Only IntegrityError (concurrent writer race) is handled inside the repo.
        All other exceptions propagate to the caller (Fix 2).
        """
        from sthrip.db.idempotency_repo import IdempotencyKeyRepository
        repo = IdempotencyKeyRepository(db)
        return repo.create(
            agent_id=agent_id,
            endpoint=endpoint,
            key=key,
            request_hash=request_hash,
            response_status=response_status,
            response_body=response_body,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def try_reserve(
        self,
        agent_id: str,
        endpoint: str,
        key: str,
        db: Optional[Session] = None,
        request_hash: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Atomically reserve an idempotency key.

        IMPORTANT — Fix 1 contract:
        This method must be called EXACTLY ONCE per request, with the DB session
        already open (``db`` parameter provided). Calling it twice (once without
        db and once with db) causes the second call to see the sentinel set by
        the first and raise 409 — blocking every first-time request.

        Two-layer flow:
          1. Redis SET NX sentinel (hot path, in-flight lock).
          2. On NX success (Redis forgot or first call):
               a. If ``db`` provided: check DB for completed row.
               b. DB row found + request_hash mismatch → 422.
               c. DB row found + hash match → backfill Redis, return cached.
               d. No DB row → return None (caller proceeds).
          3. On NX failure:
               a. Redis has sentinel → 409 (concurrent request in-flight).
               b. Redis has response → return cached (standard idempotent path).
               c. Redis value disappeared between NX and GET → retry NX.

        Args:
            agent_id:     Authenticated agent identifier.
            endpoint:     Endpoint name used to scope keys (e.g. "hub-routing").
            key:          Client-supplied idempotency key.
            db:           SQLAlchemy session. MUST be provided for mutation
                          endpoints to enable the DB-backed F-4 fix path.
            request_hash: sha256 of the canonical request body. When provided
                          and a DB row exists with a different hash, raises 422.

        Returns:
            Cached response dict if this key was already processed.
            None if successfully reserved — caller should proceed.

        Raises:
            HTTPException(409): Another request is currently processing this key.
            HTTPException(422): Same key reused with a different request body.
        """
        full_key = self._key(agent_id, endpoint, key)

        if self.use_redis:
            return self._try_reserve_redis(full_key, agent_id, endpoint, key, db, request_hash)
        return self._try_reserve_local(full_key, db, agent_id, endpoint, key, request_hash)

    def store_response(
        self,
        agent_id: str,
        endpoint: str,
        key: str,
        response: Dict[str, Any],
        db: Optional[Session] = None,
        request_hash: Optional[str] = None,
        response_status: int = 200,
    ) -> None:
        """Store the final response for a reserved idempotency key.

        Write order: DB first (authoritative), then Redis (cache).

        Fix 2 contract:
        If the DB write fails with any error OTHER than the benign UNIQUE
        constraint race (which is handled inside IdempotencyKeyRepository.create
        and never propagates), the exception re-raises here. This rolls back the
        surrounding payment transaction atomically — payment and idempotency row
        commit together or not at all.

        Only the Redis write is wrapped in try/except — cache failures must not
        break an otherwise-successful payment.

        Args:
            agent_id:        Agent who made the request.
            endpoint:        Endpoint name.
            key:             Client-supplied idempotency key.
            response:        Response dict to cache.
            db:              SQLAlchemy session. Required for DB write (Fix 2).
            request_hash:    sha256 of request body (stored for mismatch detection).
            response_status: HTTP status code to store (default 200).
        """
        full_key = self._key(agent_id, endpoint, key)

        # 1. DB write (authoritative) — let ALL non-IntegrityError exceptions propagate.
        if db is not None:
            effective_hash = request_hash or ""
            # This call raises on any non-race exception (Fix 2).
            self._db_create(
                db=db,
                agent_id=agent_id,
                endpoint=endpoint,
                key=key,
                request_hash=effective_hash,
                response_status=response_status,
                response_body=response,
            )

        # 2. Redis cache write (write-through) — cache failure must not break payment.
        if self.use_redis:
            try:
                self.redis.set(
                    full_key,
                    json.dumps(response, default=str),
                    ex=_REDIS_TTL_SECONDS,
                )
            except Exception:
                logger.critical(
                    "Redis write failed for idempotency store_response — "
                    "sentinel key may be stranded until TTL expiry. "
                    "Agent=%s endpoint=%s",
                    agent_id,
                    endpoint,
                    exc_info=True,
                )
        else:
            with self._lock:
                self._local_cache[full_key] = {
                    "response": response,
                    "expires_at": time.time() + _REDIS_TTL_SECONDS,
                }

    def release(self, agent_id: str, endpoint: str, key: str) -> None:
        """Release a reserved key without storing a response (e.g. on error).

        Note: does NOT delete any DB row — only clears the Redis sentinel.
        DB rows are only written on success (via store_response).
        """
        full_key = self._key(agent_id, endpoint, key)
        if self.use_redis:
            try:
                self.redis.eval(_RELEASE_SCRIPT, 1, full_key, _PROCESSING_SENTINEL)
            except Exception:
                logger.warning("Redis release failed for idempotency key", exc_info=True)
        else:
            with self._lock:
                if self._local_cache.get(full_key) == _PROCESSING_SENTINEL:
                    del self._local_cache[full_key]

    # ── Redis internal ────────────────────────────────────────────────────────

    def _try_reserve_redis(
        self,
        full_key: str,
        agent_id: str,
        endpoint: str,
        key: str,
        db: Optional[Session],
        request_hash: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        acquired = self.redis.set(
            full_key, _PROCESSING_SENTINEL, nx=True, ex=_REDIS_TTL_SECONDS,
        )
        if not acquired:
            # Redis already has something for this key
            cached = self.redis.get(full_key)
            if cached == _PROCESSING_SENTINEL:
                raise HTTPException(
                    status_code=409,
                    detail="Request is being processed, please retry",
                )
            if cached is not None:
                return json.loads(cached)
            # Key disappeared between SET NX and GET — retry NX once
            retry = self.redis.set(
                full_key, _PROCESSING_SENTINEL, nx=True, ex=_REDIS_TTL_SECONDS,
            )
            if not retry:
                raise HTTPException(
                    status_code=409,
                    detail="Request is being processed, please retry",
                )
        # NX acquired (Redis miss) — consult DB (F-4 fix path)
        return self._check_db_after_redis_miss(full_key, agent_id, endpoint, key, db, request_hash)

    def _check_db_after_redis_miss(
        self,
        full_key: str,
        agent_id: str,
        endpoint: str,
        key: str,
        db: Optional[Session],
        request_hash: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Check DB when Redis has no entry (NX was acquired).

        Returns:
            Cached response if DB has a completed row for this key.
            None if genuinely new request — caller proceeds.
        Raises:
            HTTPException(422): DB row exists with a different request_hash.
        """
        if db is None:
            # No session available (legacy path) — skip DB check
            return None

        existing = self._db_get(db, agent_id, endpoint, key)
        if existing is None:
            return None

        # Row exists — this is a replay after Redis TTL expiry (F-4 fix path)
        if request_hash and existing.request_hash and existing.request_hash != request_hash:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Idempotency key reused with a different request body. "
                    "Use a new idempotency key for a different request."
                ),
            )

        cached_response = existing.response_body
        # Backfill Redis cache to short-circuit future replays
        try:
            self.redis.set(
                full_key,
                json.dumps(cached_response, default=str),
                ex=_REDIS_TTL_SECONDS,
            )
        except Exception:
            logger.warning(
                "Redis backfill failed after DB hit for idempotency key. "
                "agent=%s endpoint=%s",
                agent_id,
                endpoint,
                exc_info=True,
            )

        return cached_response

    # ── Local fallback (no Redis) ─────────────────────────────────────────────

    def _try_reserve_local(
        self,
        full_key: str,
        db: Optional[Session],
        agent_id: str,
        endpoint: str,
        key: str,
        request_hash: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        now = time.time()
        if now - self._last_eviction > _EVICTION_INTERVAL:
            self._evict_expired()
            self._last_eviction = now

        with self._lock:
            entry = self._local_cache.get(full_key)
            if entry is not None:
                if entry == _PROCESSING_SENTINEL:
                    raise HTTPException(
                        status_code=409,
                        detail="Request is being processed, please retry",
                    )
                if isinstance(entry, dict) and entry.get("expires_at", 0) > time.time():
                    return entry["response"]
                # Expired — remove and treat as new
                self._local_cache.pop(full_key, None)
            self._local_cache[full_key] = _PROCESSING_SENTINEL

        # Local cache miss — check DB (F-4 fix, even without Redis)
        if db is not None:
            existing = self._db_get(db, agent_id, endpoint, key)
            if existing is not None:
                if request_hash and existing.request_hash and existing.request_hash != request_hash:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "Idempotency key reused with a different request body. "
                            "Use a new idempotency key for a different request."
                        ),
                    )
                # Backfill local cache
                with self._lock:
                    self._local_cache[full_key] = {
                        "response": existing.response_body,
                        "expires_at": time.time() + _REDIS_TTL_SECONDS,
                    }
                return existing.response_body

        return None

    def _evict_expired(self) -> None:
        """Remove expired entries from local cache (prevent unbounded growth)."""
        now = time.time()
        with self._lock:
            expired = [
                k for k, v in self._local_cache.items()
                if isinstance(v, dict) and v.get("expires_at", 0) < now
            ]
            for k in expired:
                del self._local_cache[k]

    # ── Legacy API (backward compat) ──────────────────────────────────────────

    def get_cached_response(
        self,
        idempotency_key: str,
        endpoint: str,
        agent_id: str = "",
        db: Optional[Session] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return cached response if this key was already processed, else None.

        Checks Redis first, then DB (F-4 fix: DB is consulted on cache miss).
        The ``db`` parameter is optional for backward compatibility with callers
        that do not hold a session.
        """
        full_key = self._key(agent_id, endpoint, idempotency_key)

        if self.use_redis:
            try:
                data = self.redis.get(full_key)
                if data and data != _PROCESSING_SENTINEL:
                    return json.loads(data)
                # Cache miss — check DB
                if db is not None:
                    existing = self._db_get(db, agent_id, endpoint, idempotency_key)
                    if existing is not None:
                        return existing.response_body
            except Exception:
                logger.warning("Redis read failed for idempotency key", exc_info=True)
        else:
            with self._lock:
                entry = self._local_cache.get(full_key)
                if isinstance(entry, dict) and entry.get("expires_at", 0) > time.time():
                    return entry["response"]
            # Local cache miss — check DB
            if db is not None:
                existing = self._db_get(db, agent_id, endpoint, idempotency_key)
                if existing is not None:
                    return existing.response_body
        return None


# ── Singleton ─────────────────────────────────────────────────────────────────

_store: Optional[IdempotencyStore] = None
_store_lock = threading.Lock()


def get_idempotency_store() -> IdempotencyStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = IdempotencyStore()
    return _store
