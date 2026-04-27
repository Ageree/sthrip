"""
Distributed lease for background cron loops.

Provides a Redis SETNX-based distributed lease that ensures only one replica
executes a background loop body at a time.

Pattern mirrors deposit_monitor.py:170 _UNLOCK_SCRIPT (atomic compare-and-delete).

Usage:
    redis_client = redis.from_url(settings.redis_url, decode_responses=True)

    with with_redis_lease(redis_client, "recurring_payment_loop", ttl=360) as acquired:
        if not acquired:
            return  # another replica is running — skip
        do_work()

When redis_client is None:
    - fail_open=True  → lease is granted (single-replica / test environments)
    - fail_open=False → lease is denied (strict production mode)
"""

import logging
import uuid as _uuid
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger("sthrip.distributed_lease")

# Lua script: atomic compare-and-delete (safe Redlock release).
# Returns 1 if deleted, 0 if key not held by us.
_UNLOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""

_KEY_PREFIX = "lease:"


class RedisLease:
    """Low-level Redis SETNX lease primitive.

    Attributes:
        _redis: Redis client instance (sync, decode_responses=True).
        _name:  Logical name of the lease (used as Redis key suffix).
        _ttl:   TTL in seconds — lease auto-expires if holder crashes.
        _token: Random token that identifies this holder (prevents foreign releases).
    """

    def __init__(self, redis_client, name: str, ttl: int) -> None:
        self._redis = redis_client
        self._name = name
        self._ttl = ttl
        self._token: Optional[str] = None

    @property
    def _key(self) -> str:
        return f"{_KEY_PREFIX}{self._name}"

    def acquire(self) -> bool:
        """Try to acquire the lease.

        Returns True on success, False if already held.
        """
        token = str(_uuid.uuid4())
        acquired = self._redis.set(self._key, token, nx=True, ex=self._ttl)
        if acquired:
            self._token = token
            logger.debug("Lease '%s' acquired (token=%s)", self._name, token[:8])
            return True
        logger.debug("Lease '%s' not acquired — held by another instance", self._name)
        return False

    def release(self) -> None:
        """Release the lease if we hold it.

        Uses atomic compare-and-delete so we never accidentally release a lease
        that was re-acquired by a different instance after our TTL expired.
        """
        if self._token is None:
            return
        deleted = self._redis.eval(_UNLOCK_SCRIPT, 1, self._key, self._token)
        if deleted:
            logger.debug("Lease '%s' released", self._name)
        else:
            logger.debug(
                "Lease '%s' release skipped — token mismatch (expired and re-acquired?)",
                self._name,
            )
        self._token = None


@contextmanager
def with_redis_lease(
    redis_client,
    name: str,
    ttl: int,
    fail_open: bool = False,
):
    """Context manager that acquires a distributed Redis lease.

    Yields:
        True  if the lease was acquired (caller should proceed with work).
        False if the lease is held by another replica (caller should skip).

    Args:
        redis_client:  Sync Redis client (decode_responses=True), or None.
        name:          Logical name — used as the Redis key suffix.
        ttl:           Lease TTL in seconds. Should be >= cron interval.
        fail_open:     When redis_client is None:
                         True  → grant the lease (safe for dev/test with one instance).
                         False → deny the lease (strict; default for production).
    """
    if redis_client is None:
        if fail_open:
            logger.info(
                "Lease '%s': Redis unavailable, fail_open=True → proceeding", name
            )
            yield True
        else:
            logger.warning(
                "Lease '%s': Redis unavailable, fail_open=False → skipping", name
            )
            yield False
        return

    lease = RedisLease(redis_client, name, ttl)
    acquired = lease.acquire()
    try:
        yield acquired
    finally:
        if acquired:
            lease.release()
