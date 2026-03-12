"""Test that rate limiter blocks at exactly the limit, not limit+1."""
import time
import pytest
from sthrip.services.rate_limiter import RateLimiter, RateLimitExceeded


def test_peek_blocks_at_exact_limit():
    """After N requests (where N == limit), peek must raise."""
    limiter = RateLimiter(redis_url=None)
    ip_key = "ratelimit:ip:test_peek:1.2.3.4"
    global_key = "ratelimit:global:test_peek"
    limit = 5
    now = time.time()

    # Simulate N requests already counted
    with limiter._cache_lock:
        limiter._local_cache[ip_key] = {"count": limit, "reset_at": now + 60}

    # Peek should raise — we're AT the limit
    with pytest.raises(RateLimitExceeded):
        limiter._peek_ip_limit(ip_key, global_key, limit, 1000, now)


def test_peek_allows_below_limit():
    """Below the limit, peek should NOT raise."""
    limiter = RateLimiter(redis_url=None)
    ip_key = "ratelimit:ip:test_peek_ok:1.2.3.4"
    global_key = "ratelimit:global:test_peek_ok"
    limit = 5
    now = time.time()

    with limiter._cache_lock:
        limiter._local_cache[ip_key] = {"count": limit - 1, "reset_at": now + 60}

    result = limiter._peek_ip_limit(ip_key, global_key, limit, 1000, now)
    assert result["allowed"] is True
