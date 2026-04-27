"""Unified admin session store — Redis-backed with in-memory fallback.

This module is the single implementation shared by:
- api/deps.py         (API Bearer-token admin auth, prefix ``admin_api_session:``)
- api/admin_ui/views.py  (dashboard cookie auth, prefix ``admin_session:``)

Both consumers import :func:`get_session_store` and pass the desired key prefix
when constructing their own :class:`AdminSessionStore` instance, or they share
the module-level singleton for the dashboard prefix.

Interface
---------
The class exposes two overlapping pairs of methods so that both callers can
migrate without changing their call sites:

API-style (``api/deps.py``):
    token = store.create_session(ttl)     # generates + stores token
    ok    = store.validate_session(token) # True/False

Dashboard-style (``api/admin_ui/views.py``):
    store.set_session(token, ttl)   # caller supplies the token
    ok = store.get_session(token)   # True/False
    store.delete_session(token)

CSRF (dashboard only):
    token = store.create_csrf_token()    # single-use, 10-min TTL
    ok    = store.verify_csrf_token(token)
"""

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time as _time
from typing import Optional

from sthrip.config import get_settings

logger = logging.getLogger("sthrip.session_store")

_CSRF_TTL = 600  # 10 minutes


class AdminSessionStore:
    """Redis-backed session and CSRF token store with in-memory fallback.

    Redis initialisation is lazy to avoid calling get_settings() at import time.

    Args:
        key_prefix: Redis key prefix for session entries.
            Defaults to ``"admin_session:"``.
    """

    _MAX_LOCAL_ENTRIES = 1000

    def __init__(self, key_prefix: str = "admin_session:") -> None:
        self._key_prefix = key_prefix
        self._local: dict = {}
        self._redis = None
        self._redis_checked = False
        self._init_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_redis(self) -> None:
        """Lazily attempt Redis connection on first use (thread-safe)."""
        if self._redis_checked:
            return
        with self._init_lock:
            if self._redis_checked:
                return
            try:
                import redis
                redis_url = get_settings().redis_url
                if redis_url:
                    client = redis.from_url(redis_url, decode_responses=True)
                    client.ping()
                    self._redis = client
                    logger.info(
                        "AdminSessionStore using Redis (prefix=%r)", self._key_prefix
                    )
            except Exception as exc:
                logger.warning(
                    "AdminSessionStore using in-memory fallback (prefix=%r): %s",
                    self._key_prefix,
                    exc,
                )
            finally:
                self._redis_checked = True

    def _evict_expired(self) -> None:
        """Remove expired entries from the in-memory fallback dict."""
        now = _time.time()
        expired_keys = [
            k for k, v in self._local.items()
            if isinstance(v, dict) and v.get("expires", float("inf")) < now
        ]
        for k in expired_keys:
            self._local.pop(k, None)

    @staticmethod
    def _hash(token: str) -> str:
        """Return SHA-256 hex digest of *token*."""
        return hashlib.sha256(token.encode()).hexdigest()

    def _session_key(self, token_hash: str) -> str:
        return f"{self._key_prefix}{token_hash}"

    def _store_token(self, redis_key: str, local_key: str, ttl: int, value: str = "1") -> None:
        """Write *redis_key* to Redis or *local_key* to the local dict."""
        if self._redis:
            self._redis.setex(redis_key, ttl, value)
        else:
            if len(self._local) >= self._MAX_LOCAL_ENTRIES:
                self._evict_expired()
            self._local[local_key] = {"expires": _time.time() + ttl, "value": value}

    def _read_token(self, redis_key: str, local_key: str) -> Optional[str]:
        """Return stored token value (string) if present and not expired, else None."""
        if self._redis:
            raw = self._redis.get(redis_key)
            return raw if raw else None
        entry = self._local.get(local_key)
        if not entry:
            return None
        if entry["expires"] < _time.time():
            self._local.pop(local_key, None)
            return None
        return entry.get("value", "1")

    def _delete_token(self, redis_key: str, local_key: str) -> None:
        """Delete the token entry from Redis or the local dict (no-op if absent)."""
        if self._redis:
            self._redis.delete(redis_key)
        else:
            self._local.pop(local_key, None)

    # ------------------------------------------------------------------
    # API-style interface  (used by api/deps.py)
    # ------------------------------------------------------------------

    def create_session(
        self,
        ttl: int,
        *,
        client_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> str:
        """Generate a new session token; optionally bind to client IP and UA hash.

        When ``client_ip`` and/or ``user_agent`` are provided, the session is
        bound — :meth:`validate_session` will reject mismatching values.  Token
        rotation on every privilege use is still recommended.

        Args:
            ttl: Lifetime in seconds.
            client_ip: Optional IP literal to pin the session to.
            user_agent: Optional User-Agent string; only its hash is stored.

        Returns:
            URL-safe random token string (plaintext).
        """
        self._ensure_redis()
        token = secrets.token_urlsafe(32)
        token_hash = self._hash(token)
        payload = json.dumps({
            "ip": client_ip or "",
            "ua_hash": self._hash(user_agent) if user_agent else "",
        }, sort_keys=True, separators=(",", ":"))
        self._store_token(self._session_key(token_hash), token_hash, ttl, payload)
        return token

    def validate_session(
        self,
        token: str,
        *,
        client_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> bool:
        """Return True if *token* is a live session token whose binding matches.

        If the session was created with ``client_ip``/``user_agent`` binding,
        the same values must be supplied on validate or the call returns False.
        Sessions created without binding accept any caller (legacy behaviour).
        """
        if not token:
            return False
        self._ensure_redis()
        token_hash = self._hash(token)
        raw = self._read_token(self._session_key(token_hash), token_hash)
        if raw is None:
            return False
        # Legacy plain-"1" entries: accept (no binding stored).
        if raw == "1":
            return True
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return False
        # Reject payloads that are valid JSON but not an object (e.g. a stray
        # number, string, or array). Without this guard a bare ``payload.get``
        # would raise AttributeError and surface as a 500.
        if not isinstance(payload, dict):
            return False
        bound_ip = payload.get("ip", "")
        bound_ua_hash = payload.get("ua_hash", "")
        # Always pass strings to compare_digest so the constant-time guarantee
        # holds; short-circuiting on a falsy ``client_ip`` would leak the
        # presence of a bound IP via response timing.
        got_ip = client_ip or ""
        if bound_ip and not hmac.compare_digest(bound_ip, got_ip):
            # Don't log the bound IP — log aggregators (Railway/Logtail) may be
            # readable to roles who shouldn't see where an admin token was issued.
            logger.warning("Admin session IP binding mismatch")
            return False
        if bound_ua_hash:
            got_hash = self._hash(user_agent) if user_agent else ""
            if not hmac.compare_digest(bound_ua_hash, got_hash):
                logger.warning("Admin session User-Agent fingerprint mismatch")
                return False
        return True

    # ------------------------------------------------------------------
    # Dashboard-style interface  (used by api/admin_ui/views.py)
    # ------------------------------------------------------------------

    def set_session(self, token: str, ttl: int) -> None:
        """Store a caller-supplied *token* with the given *ttl*.

        Args:
            token: Plaintext token (caller is responsible for generation).
            ttl:   Lifetime in seconds.
        """
        self._ensure_redis()
        token_hash = self._hash(token)
        self._store_token(self._session_key(token_hash), token_hash, ttl)

    def get_session(self, token: str) -> bool:
        """Return True if *token* is a live session set via :meth:`set_session`."""
        if not token:
            return False
        self._ensure_redis()
        token_hash = self._hash(token)
        return self._read_token(self._session_key(token_hash), token_hash) is not None

    def delete_session(self, token: str) -> None:
        """Remove *token* from the store (no-op if absent).

        Args:
            token: Plaintext token to remove.
        """
        self._ensure_redis()
        token_hash = self._hash(token)
        self._delete_token(self._session_key(token_hash), token_hash)

    # ------------------------------------------------------------------
    # CSRF  (used by api/admin_ui/views.py)
    # ------------------------------------------------------------------

    def create_csrf_token(self) -> str:
        """Create a single-use CSRF token with a 10-minute TTL.

        Returns:
            Plaintext CSRF token.
        """
        self._ensure_redis()
        token = secrets.token_urlsafe(32)
        token_hash = self._hash(token)
        csrf_redis_key = f"csrf:{token_hash}"
        csrf_local_key = f"csrf:{token_hash}"
        self._store_token(csrf_redis_key, csrf_local_key, _CSRF_TTL)
        return token

    def verify_csrf_token(self, token: str) -> bool:
        """Verify *and consume* a single-use CSRF token.

        Returns True if the token was valid (and deletes it).
        Returns False for empty, whitespace-only, invalid, or already-used tokens.
        """
        if not token or not token.strip():
            return False
        self._ensure_redis()
        token_hash = self._hash(token)
        csrf_redis_key = f"csrf:{token_hash}"
        csrf_local_key = f"csrf:{token_hash}"

        if self._redis:
            pipe = self._redis.pipeline(True)
            pipe.get(csrf_redis_key)
            pipe.delete(csrf_redis_key)
            results = pipe.execute()
            return bool(results[0])

        entry = self._local.pop(csrf_local_key, None)
        return entry is not None and entry["expires"] > _time.time()


# ---------------------------------------------------------------------------
# Module-level singleton (dashboard default — prefix ``admin_session:``)
# ---------------------------------------------------------------------------

_session_store = AdminSessionStore(key_prefix="admin_session:")


def get_session_store() -> AdminSessionStore:
    """Return the module-level AdminSessionStore singleton.

    This singleton uses the ``admin_session:`` Redis key prefix and is the
    shared instance for the admin dashboard.  The API admin auth in
    ``api/deps.py`` constructs its own instance with the
    ``admin_api_session:`` prefix.
    """
    return _session_store
