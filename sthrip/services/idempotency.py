"""
Idempotency key service.
Atomic reserve-then-store pattern: Redis SET NX (TTL 24h) with local dict fallback.
Keys are scoped by agent_id to prevent cross-agent collisions.
"""

import hashlib
import json
import threading
import time
import logging
from typing import Optional, Dict, Any

from fastapi import HTTPException

from sthrip.config import get_settings

logger = logging.getLogger("sthrip.idempotency")

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

_TTL_SECONDS = 86400  # 24 hours
_PROCESSING_SENTINEL = "__processing__"
_EVICTION_INTERVAL = 300  # 5 minutes

# Atomic compare-and-delete: only removes the key if it still holds the sentinel
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


class IdempotencyStore:
    """Atomic idempotency key store with reserve/store pattern."""

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
                self.redis = None
        else:
            self.redis = None

    def _key(self, agent_id: str, endpoint: str, idempotency_key: str) -> str:
        hashed = hashlib.sha256(idempotency_key.encode()).hexdigest()
        return f"idempotency:{agent_id}:{endpoint}:{hashed}"

    def try_reserve(
        self, agent_id: str, endpoint: str, key: str,
    ) -> Optional[Dict[str, Any]]:
        """Atomically reserve an idempotency key.

        Returns:
            - Cached response dict if key was already processed.
            - None if successfully reserved (caller should proceed).
        Raises:
            HTTPException(409) if another request is currently processing this key.
        """
        full_key = self._key(agent_id, endpoint, key)

        if self.use_redis:
            return self._try_reserve_redis(full_key)
        return self._try_reserve_local(full_key)

    def _try_reserve_redis(self, full_key: str) -> Optional[Dict[str, Any]]:
        acquired = self.redis.set(
            full_key, _PROCESSING_SENTINEL, nx=True, ex=_TTL_SECONDS,
        )
        if not acquired:
            cached = self.redis.get(full_key)
            if cached == _PROCESSING_SENTINEL:
                raise HTTPException(
                    status_code=409,
                    detail="Request is being processed, please retry",
                )
            if cached is not None:
                return json.loads(cached)
            # Key expired between SET NX and GET — retry reservation
            retry = self.redis.set(
                full_key, _PROCESSING_SENTINEL, nx=True, ex=_TTL_SECONDS,
            )
            if not retry:
                raise HTTPException(
                    status_code=409,
                    detail="Request is being processed, please retry",
                )
        return None

    def _try_reserve_local(self, full_key: str) -> Optional[Dict[str, Any]]:
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
        return None

    def store_response(
        self, agent_id: str, endpoint: str, key: str, response: Dict[str, Any],
    ) -> None:
        """Store the final response for a reserved idempotency key."""
        full_key = self._key(agent_id, endpoint, key)

        if self.use_redis:
            try:
                self.redis.set(
                    full_key, json.dumps(response, default=str), ex=_TTL_SECONDS,
                )
            except Exception:
                logger.warning("Redis write failed for idempotency key", exc_info=True)
        else:
            with self._lock:
                self._local_cache[full_key] = {
                    "response": response,
                    "expires_at": time.time() + _TTL_SECONDS,
                }

    def _evict_expired(self) -> None:
        """Remove expired entries from local cache (I7: prevent unbounded growth)."""
        now = time.time()
        with self._lock:
            expired = [
                k for k, v in self._local_cache.items()
                if isinstance(v, dict) and v.get("expires_at", 0) < now
            ]
            for k in expired:
                del self._local_cache[k]

    def release(self, agent_id: str, endpoint: str, key: str) -> None:
        """Release a reserved key without storing a response (e.g. on error)."""
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

    # ── Legacy API (backward compat) ───────────────────────────────────────
    def get_cached_response(
        self, idempotency_key: str, endpoint: str, agent_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Return cached response if this key was already processed, else None."""
        full_key = self._key(agent_id, endpoint, idempotency_key)

        if self.use_redis:
            try:
                data = self.redis.get(full_key)
                if data and data != _PROCESSING_SENTINEL:
                    return json.loads(data)
            except Exception:
                logger.warning("Redis read failed for idempotency key", exc_info=True)
        else:
            with self._lock:
                entry = self._local_cache.get(full_key)
                if isinstance(entry, dict) and entry.get("expires_at", 0) > time.time():
                    return entry["response"]
        return None


_store: Optional[IdempotencyStore] = None
_store_lock = threading.Lock()


def get_idempotency_store() -> IdempotencyStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = IdempotencyStore()
    return _store
