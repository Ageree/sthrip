"""
Rate limiting service using Redis
Supports tiered limits per agent
"""

import os
import time
from enum import Enum
from typing import Optional, Dict
from dataclasses import dataclass

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


class RateLimitExceeded(Exception):
    """Raised when rate limit is exceeded"""
    
    def __init__(self, limit: int, reset_at: float):
        self.limit = limit
        self.reset_at = reset_at
        super().__init__(f"Rate limit exceeded. Limit: {limit}/min. Reset at: {reset_at}")


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
        
        if REDIS_AVAILABLE:
            redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
            try:
                self.redis = redis.from_url(redis_url, decode_responses=True)
                self.redis.ping()
                self.use_redis = True
            except (redis.ConnectionError, redis.ResponseError):
                self.use_redis = False
        else:
            self.use_redis = False
            self.redis = None
    
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
        """Check limit using Redis"""
        now = time.time()
        window = 60  # 1 minute window
        
        pipe = self.redis.pipeline()
        
        # Get current count and reset time
        pipe.hmget(key, "count", "reset_at")
        result = pipe.execute()
        
        count_str, reset_at_str = result[0]
        
        if count_str is None or reset_at_str is None or float(reset_at_str) < now:
            # New window
            count = cost
            reset_at = now + window
            self.redis.hmset(key, {"count": count, "reset_at": reset_at})
            self.redis.expire(key, window + 1)
        else:
            count = int(count_str) + cost
            reset_at = float(reset_at_str)
            
            if count > config.requests_per_minute:
                raise RateLimitExceeded(
                    limit=config.requests_per_minute,
                    reset_at=reset_at
                )
            
            self.redis.hincrby(key, "count", cost)
        
        remaining = max(0, config.requests_per_minute - count)
        
        return {
            "allowed": True,
            "remaining": remaining,
            "reset_at": reset_at,
            "limit": config.requests_per_minute
        }
    
    def _check_local(
        self,
        key: str,
        config: RateLimitConfig,
        cost: int
    ) -> Dict:
        """Check limit using local cache (fallback)"""
        now = time.time()
        window = 60
        
        entry = self._local_cache.get(key)
        
        if entry is None or entry["reset_at"] < now:
            # New window
            entry = {
                "count": cost,
                "reset_at": now + window
            }
            self._local_cache[key] = entry
        else:
            entry["count"] += cost
            
            if entry["count"] > config.requests_per_minute:
                raise RateLimitExceeded(
                    limit=config.requests_per_minute,
                    reset_at=entry["reset_at"]
                )
        
        remaining = max(0, config.requests_per_minute - entry["count"])
        
        return {
            "allowed": True,
            "remaining": remaining,
            "reset_at": entry["reset_at"],
            "limit": config.requests_per_minute
        }
    
    def get_limit_status(self, agent_id: str, tier: str = "standard") -> Dict:
        """Get current rate limit status for agent"""
        config = self._get_limit_config(tier)
        key = self._get_key(agent_id)
        
        if self.use_redis:
            data = self.redis.hmget(key, "count", "reset_at")
            count = int(data[0]) if data[0] else 0
            reset_at = float(data[1]) if data[1] else time.time() + 60
        else:
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
            if key in self._local_cache:
                del self._local_cache[key]


# Global rate limiter instance
_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Get global rate limiter instance"""
    global _limiter
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
