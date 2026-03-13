"""
Rate limiting service using Redis
Supports tiered limits per agent
"""

import logging
import threading
import time
from enum import Enum
from typing import Optional, Dict
from dataclasses import dataclass

from fastapi import HTTPException
from sthrip.config import get_settings

logger = logging.getLogger("sthrip.rate_limiter")

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

class RateLimitTier(Enum):
    """Rate limit tiers"""
    LOW = "low"           # 10 req/min
    STANDARD = "standard" # 100 req/min
    HIGH = "high"         # 1000 req/min
    UNLIMITED = "unlimited"  # No limit


@dataclass
class RateLimitConfig:
    """Rate limit configuration"""
    requests_per_minute: int
    burst_size: int
    

# Default limits per tier
DEFAULT_LIMITS: Dict[RateLimitTier, RateLimitConfig] = {
    RateLimitTier.LOW: RateLimitConfig(requests_per_minute=10, burst_size=5),
    RateLimitTier.STANDARD: RateLimitConfig(requests_per_minute=100, burst_size=20),
    RateLimitTier.HIGH: RateLimitConfig(requests_per_minute=1000, burst_size=100),
    RateLimitTier.UNLIMITED: RateLimitConfig(requests_per_minute=1000000, burst_size=100000),
}


# Lua script for atomic rate limit check+increment
_RATE_LIMIT_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'count', 'reset_at')
local count = tonumber(data[1])
local reset_at = tonumber(data[2])

if count == nil or reset_at == nil or reset_at < now then
    count = cost
    reset_at = now + window
    redis.call('HSET', key, 'count', count, 'reset_at', reset_at)
    redis.call('EXPIRE', key, window + 1)
    return {count, tostring(reset_at)}
end

if count + cost > limit then
    return {-1, tostring(reset_at)}
end

count = redis.call('HINCRBY', key, 'count', cost)
return {count, tostring(reset_at)}
"""


class RateLimitExceeded(Exception):
    """Raised when rate limit is exceeded"""
    
    def __init__(self, limit: int, reset_at: float):
        self.limit = limit
        self.reset_at = reset_at
        super().__init__(f"Rate limit exceeded. Limit: {limit}/min. Reset at: {reset_at}")


_EVICTION_INTERVAL = 300  # seconds between local-cache sweeps (5 minutes)


class RateLimiter:
    """
    Token bucket rate limiter using Redis

    Supports:
    - Per-agent rate limiting by tier
    - Per-endpoint limiting
    - Sliding window for IP-based limits
    """

    def __init__(
        self,
        redis_url: Optional[str] = None,
        default_tier: RateLimitTier = RateLimitTier.STANDARD
    ):
        self.default_tier = default_tier
        self._local_cache: Dict[str, Dict] = {}  # Fallback if no Redis
        self._cache_lock = threading.Lock()
        self._last_eviction: float = 0.0
        self._reject_requests = False

        settings = get_settings()
        fail_open = settings.rate_limit_fail_open

        if REDIS_AVAILABLE:
            redis_url = redis_url or settings.redis_url
            if not redis_url:
                self.redis = None
                self._handle_redis_unavailable(fail_open)
            else:
                try:
                    self.redis = redis.from_url(redis_url, decode_responses=True)
                    self.redis.ping()
                    self.use_redis = True
                except (redis.ConnectionError, redis.ResponseError, ValueError):
                    self._handle_redis_unavailable(fail_open)
        else:
            self.redis = None
            self._handle_redis_unavailable(fail_open)

    def _handle_redis_unavailable(self, fail_open: bool) -> None:
        """Handle Redis being unavailable based on fail_open setting."""
        self.use_redis = False
        self._reject_requests = not fail_open
        if fail_open:
            logger.critical(
                "Redis unavailable — rate limiter falling back to in-process dict"
                " (RATE_LIMIT_FAIL_OPEN=true). Multi-replica rate limiting is DISABLED."
            )
        else:
            logger.critical(
                "Redis unavailable — rate limiting will reject authenticated requests"
                " (RATE_LIMIT_FAIL_OPEN=false). Health checks remain available."
            )

    def _get_key(self, agent_id: str, endpoint: Optional[str] = None) -> str:
        """Generate Redis key for agent"""
        if endpoint:
            return f"ratelimit:{agent_id}:{endpoint}"
        return f"ratelimit:{agent_id}"
    
    def _get_limit_config(self, tier: str) -> RateLimitConfig:
        """Get limit config for tier"""
        try:
            tier_enum = RateLimitTier(tier.lower())
        except ValueError:
            tier_enum = self.default_tier
        return DEFAULT_LIMITS.get(tier_enum, DEFAULT_LIMITS[RateLimitTier.STANDARD])
    
    def check_rate_limit(
        self,
        agent_id: str,
        tier: str = "standard",
        endpoint: Optional[str] = None,
        cost: int = 1
    ) -> Dict:
        """
        Check if request is within rate limit
        
        Args:
            agent_id: Agent identifier
            tier: Rate limit tier
            endpoint: Optional endpoint-specific limiting
            cost: Request cost (default 1)
        
        Returns:
            Dict with limit info: {
                "allowed": bool,
                "remaining": int,
                "reset_at": float,
                "limit": int
            }
        
        Raises:
            RateLimitExceeded: If limit exceeded
        """
        config = self._get_limit_config(tier)
        key = self._get_key(agent_id, endpoint)

        if getattr(self, "_reject_requests", False):
            raise HTTPException(
                status_code=503,
                detail="Rate limiting service unavailable: Redis is not reachable"
            )

        if self.use_redis:
            return self._check_redis(key, config, cost)
        else:
            return self._check_local(key, config, cost)
    
    def _check_redis(
        self,
        key: str,
        config: RateLimitConfig,
        cost: int
    ) -> Dict:
        """Check limit using atomic Lua script."""
        now = time.time()
        window = 60  # 1 minute window

        try:
            result = self.redis.eval(
                _RATE_LIMIT_LUA, 1, key,
                config.requests_per_minute, window, cost, now
            )
        except (redis.ConnectionError, redis.RedisError) as exc:
            logger.warning("Redis failed mid-request in _check_redis, falling back to local: %s", exc)
            return self._check_local(key, config, cost)

        count = int(result[0])
        reset_at = float(result[1])

        if count == -1:
            raise RateLimitExceeded(
                limit=config.requests_per_minute,
                reset_at=reset_at,
            )

        remaining = max(0, config.requests_per_minute - count)
        return {
            "allowed": True,
            "remaining": remaining,
            "reset_at": reset_at,
            "limit": config.requests_per_minute,
        }
    
    def _evict_expired(self) -> None:
        """Remove expired entries from local cache (I6: prevent unbounded growth)."""
        now = time.time()
        with self._cache_lock:
            expired = [
                k for k, v in self._local_cache.items()
                if v.get("reset_at", 0) < now
            ]
            for k in expired:
                del self._local_cache[k]

    def _check_local(
        self,
        key: str,
        config: RateLimitConfig,
        cost: int
    ) -> Dict:
        """Check limit using local cache (fallback)"""
        now = time.time()
        window = 60

        if now - self._last_eviction > _EVICTION_INTERVAL:
            self._evict_expired()
            self._last_eviction = now

        with self._cache_lock:
            entry = self._local_cache.get(key)

            if entry is None or entry["reset_at"] < now:
                # New window
                entry = {
                    "count": cost,
                    "reset_at": now + window
                }
                self._local_cache[key] = entry
            else:
                # Check BEFORE incrementing (consistent with Redis Lua script)
                if entry["count"] + cost > config.requests_per_minute:
                    raise RateLimitExceeded(
                        limit=config.requests_per_minute,
                        reset_at=entry["reset_at"]
                    )
                entry = {
                    "count": entry["count"] + cost,
                    "reset_at": entry["reset_at"]
                }
                self._local_cache[key] = entry

            remaining = max(0, config.requests_per_minute - entry["count"])

        return {
            "allowed": True,
            "remaining": remaining,
            "reset_at": entry["reset_at"],
            "limit": config.requests_per_minute
        }
    
    def check_ip_rate_limit(
        self,
        ip_address: str,
        action: str = "register",
        per_ip_limit: int = 5,
        global_limit: int = 100,
        window_seconds: int = 3600,
        check_only: bool = False,
    ) -> Dict:
        """
        Check IP-based rate limit for unauthenticated endpoints.

        Args:
            ip_address: Client IP address
            action: Action name (used in key)
            per_ip_limit: Max requests per IP per window
            global_limit: Max total requests per window
            window_seconds: Window duration in seconds
            check_only: If True, only check whether the limit is exceeded
                        without incrementing the counter. Useful for separating
                        the check from the increment (e.g. increment only on
                        authentication failure).

        Raises:
            RateLimitExceeded: If either limit exceeded
        """
        ip_key = f"ratelimit:ip:{action}:{ip_address}"
        global_key = f"ratelimit:global:{action}"
        now = time.time()

        if check_only:
            return self._peek_ip_limit(ip_key, global_key, per_ip_limit, global_limit, now)

        if self.use_redis:
            return self._check_ip_redis(ip_key, global_key, per_ip_limit, global_limit, window_seconds, now)
        else:
            return self._check_ip_local(ip_key, global_key, per_ip_limit, global_limit, window_seconds, now)

    def _peek_ip_limit(self, ip_key, global_key, per_ip_limit, global_limit, now):
        """Check if IP or global limit is already exceeded without incrementing.

        Uses strict > comparison. After N failed attempts (counter=N), peek allows
        one more attempt. If that attempt also fails, the increment path pushes
        counter to N+1 which exceeds the limit on the next peek.
        """
        if self.use_redis:
            ip_data = self.redis.hmget(ip_key, "count", "reset_at")
            ip_count = int(ip_data[0]) if ip_data[0] else 0
            ip_reset = float(ip_data[1]) if ip_data[1] else now

            if ip_reset < now:
                ip_count = 0

            if ip_count >= per_ip_limit:
                raise RateLimitExceeded(limit=per_ip_limit, reset_at=ip_reset)

            g_data = self.redis.hmget(global_key, "count", "reset_at")
            g_count = int(g_data[0]) if g_data[0] else 0
            g_reset = float(g_data[1]) if g_data[1] else now

            if g_reset < now:
                g_count = 0

            if g_count >= global_limit:
                raise RateLimitExceeded(limit=global_limit, reset_at=g_reset)

            return {"allowed": True, "ip_remaining": per_ip_limit - ip_count, "global_remaining": global_limit - g_count}
        else:
            with self._cache_lock:
                ip_entry = self._local_cache.get(ip_key)
                ip_count = 0
                ip_reset = now
                if ip_entry and ip_entry["reset_at"] >= now:
                    ip_count = ip_entry["count"]
                    ip_reset = ip_entry["reset_at"]

                if ip_count >= per_ip_limit:
                    raise RateLimitExceeded(limit=per_ip_limit, reset_at=ip_reset)

                g_entry = self._local_cache.get(global_key)
                g_count = 0
                if g_entry and g_entry["reset_at"] >= now:
                    g_count = g_entry["count"]
                    g_reset = g_entry["reset_at"]
                    if g_count >= global_limit:
                        raise RateLimitExceeded(limit=global_limit, reset_at=g_reset)

            return {"allowed": True, "ip_remaining": per_ip_limit - ip_count, "global_remaining": global_limit - g_count}

    def _check_ip_redis(self, ip_key, global_key, per_ip_limit, global_limit, window, now):
        """Check IP + global limits using atomic Lua scripts.

        Global limit is checked first (peek) so that the IP counter is not
        charged when the global limit is already exceeded.
        """
        # Peek at global_key first — do NOT increment yet
        g_data = self.redis.hmget(global_key, "count", "reset_at")
        g_count_peek = int(g_data[0]) if g_data[0] else 0
        g_reset_peek = float(g_data[1]) if g_data[1] else now
        if g_reset_peek >= now and g_count_peek + 1 > global_limit:
            raise RateLimitExceeded(limit=global_limit, reset_at=g_reset_peek)

        # Per-IP check+increment
        ip_result = self.redis.eval(
            _RATE_LIMIT_LUA, 1, ip_key,
            per_ip_limit, window, 1, now
        )
        ip_count = int(ip_result[0])
        ip_reset = float(ip_result[1])

        if ip_count == -1:
            raise RateLimitExceeded(limit=per_ip_limit, reset_at=ip_reset)

        # Global check+increment
        g_result = self.redis.eval(
            _RATE_LIMIT_LUA, 1, global_key,
            global_limit, window, 1, now
        )
        g_count = int(g_result[0])
        g_reset = float(g_result[1])

        if g_count == -1:
            raise RateLimitExceeded(limit=global_limit, reset_at=g_reset)

        return {"allowed": True, "ip_remaining": per_ip_limit - ip_count, "global_remaining": global_limit - g_count}

    def _check_ip_local(self, ip_key, global_key, per_ip_limit, global_limit, window, now):
        with self._cache_lock:
            # Read current counts (without incrementing yet)
            ip_entry = self._local_cache.get(ip_key)
            if ip_entry is None or ip_entry["reset_at"] < now:
                ip_count = 0
                ip_reset = now + window
            else:
                ip_count = ip_entry["count"]
                ip_reset = ip_entry["reset_at"]

            g_entry = self._local_cache.get(global_key)
            if g_entry is None or g_entry["reset_at"] < now:
                g_count = 0
                g_reset = now + window
            else:
                g_count = g_entry["count"]
                g_reset = g_entry["reset_at"]

            # Check both limits BEFORE incrementing
            if ip_count + 1 > per_ip_limit:
                raise RateLimitExceeded(limit=per_ip_limit, reset_at=ip_reset)
            if g_count + 1 > global_limit:
                raise RateLimitExceeded(limit=global_limit, reset_at=g_reset)

            # Both limits OK — now increment atomically
            self._local_cache[ip_key] = {"count": ip_count + 1, "reset_at": ip_reset}
            self._local_cache[global_key] = {"count": g_count + 1, "reset_at": g_reset}

        return {"allowed": True, "ip_remaining": per_ip_limit - (ip_count + 1), "global_remaining": global_limit - (g_count + 1)}

    def get_limit_status(self, agent_id: str, tier: str = "standard") -> Dict:
        """Get current rate limit status for agent"""
        config = self._get_limit_config(tier)
        key = self._get_key(agent_id)
        
        if self.use_redis:
            data = self.redis.hmget(key, "count", "reset_at")
            count = int(data[0]) if data[0] else 0
            reset_at = float(data[1]) if data[1] else time.time() + 60
        else:
            with self._cache_lock:
                entry = self._local_cache.get(key)
            if entry:
                count = entry["count"]
                reset_at = entry["reset_at"]
            else:
                count = 0
                reset_at = time.time() + 60
        
        remaining = max(0, config.requests_per_minute - count)
        
        return {
            "limit": config.requests_per_minute,
            "used": count,
            "remaining": remaining,
            "reset_at": reset_at,
            "tier": tier
        }
    
    def reset_limit(self, agent_id: str, endpoint: Optional[str] = None):
        """Reset rate limit for agent (admin only)"""
        key = self._get_key(agent_id, endpoint)

        if self.use_redis:
            self.redis.delete(key)
        else:
            with self._cache_lock:
                self._local_cache.pop(key, None)

    # ─── Failed auth rate limiting ────────────────────────────────────────

    def check_failed_auth(self, ip: str, limit: int = 5, window: int = 60) -> None:
        """Raise RateLimitExceeded if IP has exceeded failed auth limit."""
        key = f"ratelimit:ip:failed_auth:{ip}"
        now = time.time()

        if self.use_redis:
            data = self.redis.hmget(key, "count", "reset_at")
            count = int(data[0]) if data[0] else 0
            reset_at = float(data[1]) if data[1] else now + window
            if reset_at < now:
                count = 0
            if count >= limit:
                raise RateLimitExceeded(limit=limit, reset_at=reset_at)
        else:
            with self._cache_lock:
                entry = self._local_cache.get(key)
            if entry and entry.get("reset_at", 0) >= now:
                if entry.get("count", 0) >= limit:
                    raise RateLimitExceeded(limit=limit, reset_at=entry["reset_at"])

    def record_failed_auth(self, ip: str, window: int = 60) -> int:
        """Increment failed auth counter for IP. Returns current count."""
        key = f"ratelimit:ip:failed_auth:{ip}"
        now = time.time()

        if self.use_redis:
            pipe = self.redis.pipeline()
            pipe.hincrby(key, "count", 1)
            pipe.hsetnx(key, "reset_at", str(now + window))
            pipe.expire(key, window + 60)
            results = pipe.execute()
            count = int(results[0])
            return count
        else:
            with self._cache_lock:
                entry = self._local_cache.get(key)
                if entry and entry.get("reset_at", 0) >= now:
                    count = entry.get("count", 0) + 1
                    self._local_cache[key] = {
                        "count": count,
                        "reset_at": entry["reset_at"],
                    }
                else:
                    count = 1
                    self._local_cache[key] = {
                        "count": count,
                        "reset_at": now + window,
                    }
                return count


# Global rate limiter instance
_limiter: Optional[RateLimiter] = None
_limiter_lock = threading.Lock()


def get_rate_limiter() -> RateLimiter:
    """Get global rate limiter instance (thread-safe)."""
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = RateLimiter()
    return _limiter


def check_rate_limit(
    agent_id: str,
    tier: str = "standard",
    endpoint: Optional[str] = None
) -> Dict:
    """Convenience function to check rate limit"""
    limiter = get_rate_limiter()
    return limiter.check_rate_limit(agent_id, tier, endpoint)
