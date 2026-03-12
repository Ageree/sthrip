"""Test RateLimiter.check_failed_auth and record_failed_auth public API."""
import pytest
from sthrip.services.rate_limiter import RateLimiter, RateLimitExceeded


def test_record_and_check_failed_auth():
    limiter = RateLimiter(redis_url=None)

    # Record 4 failures — should not raise
    for _ in range(4):
        limiter.record_failed_auth("1.2.3.4")
        limiter.check_failed_auth("1.2.3.4")  # still under limit

    # 5th failure
    limiter.record_failed_auth("1.2.3.4")
    with pytest.raises(RateLimitExceeded):
        limiter.check_failed_auth("1.2.3.4")


def test_check_failed_auth_no_records():
    """No failures recorded — should not raise."""
    limiter = RateLimiter(redis_url=None)
    limiter.check_failed_auth("9.9.9.9")  # should not raise
