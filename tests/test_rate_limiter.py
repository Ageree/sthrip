"""Tests for sthrip/services/rate_limiter.py — targeting 80%+ coverage."""

import importlib
import importlib.util
import sys
import threading
import time
import types
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import rate_limiter directly, bypassing broken __init__.py in sthrip.services
# (fee_collector.py has a syntax error that prevents normal package import).
# We also wire up sys.modules so that `patch.object` and attribute-based
# patching works correctly.
# ---------------------------------------------------------------------------
_RL_PATH = "/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/services/rate_limiter.py"
_spec = importlib.util.spec_from_file_location(
    "sthrip.services.rate_limiter",
    _RL_PATH,
    submodule_search_locations=[],
)

# Ensure parent packages exist in sys.modules
for _pkg in ("sthrip", "sthrip.services"):
    if _pkg not in sys.modules:
        _m = types.ModuleType(_pkg)
        _m.__path__ = []
        sys.modules[_pkg] = _m

rl_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rl_mod
_spec.loader.exec_module(rl_mod)

# Wire attribute chain so patch("sthrip.services.rate_limiter.X") works
sys.modules["sthrip"].services = sys.modules["sthrip.services"]
sys.modules["sthrip.services"].rate_limiter = rl_mod

RateLimitTier = rl_mod.RateLimitTier
RateLimitConfig = rl_mod.RateLimitConfig
RateLimitExceeded = rl_mod.RateLimitExceeded
RateLimiter = rl_mod.RateLimiter
DEFAULT_LIMITS = rl_mod.DEFAULT_LIMITS

# Module-path string used by all patch() calls
_MOD = "sthrip.services.rate_limiter"


# ---------------------------------------------------------------------------
# RateLimitTier enum
# ---------------------------------------------------------------------------

class TestRateLimitTier:

    def test_tier_values(self):
        assert RateLimitTier.LOW.value == "low"
        assert RateLimitTier.STANDARD.value == "standard"
        assert RateLimitTier.HIGH.value == "high"
        assert RateLimitTier.UNLIMITED.value == "unlimited"

    def test_all_tiers_in_default_limits(self):
        for tier in RateLimitTier:
            assert tier in DEFAULT_LIMITS


# ---------------------------------------------------------------------------
# RateLimitConfig dataclass
# ---------------------------------------------------------------------------

class TestRateLimitConfig:

    def test_fields(self):
        cfg = RateLimitConfig(requests_per_minute=50, burst_size=10)
        assert cfg.requests_per_minute == 50
        assert cfg.burst_size == 10


# ---------------------------------------------------------------------------
# DEFAULT_LIMITS dictionary
# ---------------------------------------------------------------------------

class TestDefaultLimits:

    def test_low_tier(self):
        cfg = DEFAULT_LIMITS[RateLimitTier.LOW]
        assert cfg.requests_per_minute == 10
        assert cfg.burst_size == 5

    def test_standard_tier(self):
        cfg = DEFAULT_LIMITS[RateLimitTier.STANDARD]
        assert cfg.requests_per_minute == 100
        assert cfg.burst_size == 20

    def test_high_tier(self):
        cfg = DEFAULT_LIMITS[RateLimitTier.HIGH]
        assert cfg.requests_per_minute == 1000
        assert cfg.burst_size == 100

    def test_unlimited_tier(self):
        cfg = DEFAULT_LIMITS[RateLimitTier.UNLIMITED]
        assert cfg.requests_per_minute == 1_000_000
        assert cfg.burst_size == 100_000


# ---------------------------------------------------------------------------
# RateLimitExceeded exception
# ---------------------------------------------------------------------------

class TestRateLimitExceeded:

    def test_attributes(self):
        reset = time.time() + 60
        exc = RateLimitExceeded(limit=100, reset_at=reset)
        assert exc.limit == 100
        assert exc.reset_at == reset
        assert "100" in str(exc)
        assert str(reset) in str(exc)


# ---------------------------------------------------------------------------
# Helper: build a limiter with no Redis (local fallback)
# ---------------------------------------------------------------------------

def _make_local_limiter(default_tier=RateLimitTier.STANDARD):
    """Create a RateLimiter that always uses the local-cache path."""
    with patch(f"{_MOD}.REDIS_AVAILABLE", False):
        limiter = RateLimiter(default_tier=default_tier)
    assert limiter.use_redis is False
    return limiter


def _make_redis_limiter():
    """Create a RateLimiter connected to a mock Redis."""
    mock_redis_mod = MagicMock()
    mock_conn = MagicMock()
    mock_redis_mod.from_url.return_value = mock_conn
    mock_conn.ping.return_value = True
    with patch(f"{_MOD}.REDIS_AVAILABLE", True), \
         patch(f"{_MOD}.redis", mock_redis_mod):
        limiter = RateLimiter(redis_url="redis://fake:6379/0")
    return limiter, mock_conn


# ---------------------------------------------------------------------------
# RateLimiter.__init__
# ---------------------------------------------------------------------------

class TestRateLimiterInit:

    def test_init_no_redis_available(self):
        limiter = _make_local_limiter()
        assert limiter.use_redis is False
        assert limiter.redis is None
        assert limiter._local_cache == {}

    def test_init_redis_available_but_connection_fails(self):
        mock_redis_mod = MagicMock()
        # ConnectionError and ResponseError must be real exception classes
        # so that `except (redis.ConnectionError, redis.ResponseError)` works
        mock_redis_mod.ConnectionError = ConnectionError
        mock_redis_mod.ResponseError = Exception
        mock_redis_mod.from_url.return_value.ping.side_effect = ConnectionError("refused")
        with patch(f"{_MOD}.REDIS_AVAILABLE", True), \
             patch(f"{_MOD}.redis", mock_redis_mod):
            limiter = RateLimiter(redis_url="redis://fake:6379/0")
        assert limiter.use_redis is False

    def test_init_redis_available_and_connected(self):
        limiter, mock_conn = _make_redis_limiter()
        assert limiter.use_redis is True
        assert limiter.redis is mock_conn

    def test_default_tier(self):
        limiter = _make_local_limiter(default_tier=RateLimitTier.HIGH)
        assert limiter.default_tier == RateLimitTier.HIGH


# ---------------------------------------------------------------------------
# _get_key
# ---------------------------------------------------------------------------

class TestGetKey:

    def test_without_endpoint(self):
        limiter = _make_local_limiter()
        assert limiter._get_key("agent1") == "ratelimit:agent1"

    def test_with_endpoint(self):
        limiter = _make_local_limiter()
        assert limiter._get_key("agent1", "/pay") == "ratelimit:agent1:/pay"

    def test_empty_agent_id(self):
        limiter = _make_local_limiter()
        assert limiter._get_key("") == "ratelimit:"

    def test_none_endpoint(self):
        limiter = _make_local_limiter()
        assert limiter._get_key("a", None) == "ratelimit:a"


# ---------------------------------------------------------------------------
# _get_limit_config
# ---------------------------------------------------------------------------

class TestGetLimitConfig:

    def test_valid_tiers(self):
        limiter = _make_local_limiter()
        for tier in ("low", "standard", "high", "unlimited"):
            cfg = limiter._get_limit_config(tier)
            assert isinstance(cfg, RateLimitConfig)

    def test_case_insensitive(self):
        limiter = _make_local_limiter()
        cfg = limiter._get_limit_config("HIGH")
        assert cfg.requests_per_minute == 1000

    def test_invalid_tier_falls_back_to_default(self):
        limiter = _make_local_limiter(default_tier=RateLimitTier.LOW)
        cfg = limiter._get_limit_config("nonexistent")
        assert cfg.requests_per_minute == DEFAULT_LIMITS[RateLimitTier.LOW].requests_per_minute


# ---------------------------------------------------------------------------
# check_rate_limit — local fallback path (_check_local)
# ---------------------------------------------------------------------------

class TestCheckRateLimitLocal:

    def test_first_request_allowed(self):
        limiter = _make_local_limiter()
        result = limiter.check_rate_limit("agent1", tier="standard")
        assert result["allowed"] is True
        assert result["remaining"] == 99
        assert result["limit"] == 100

    def test_multiple_requests_decrement_remaining(self):
        limiter = _make_local_limiter()
        for _ in range(5):
            result = limiter.check_rate_limit("agent1", tier="low")
        assert result["remaining"] == 5  # 10 - 5

    def test_exceeds_limit_raises(self):
        limiter = _make_local_limiter()
        for _ in range(10):
            limiter.check_rate_limit("agent1", tier="low")
        with pytest.raises(RateLimitExceeded) as exc_info:
            limiter.check_rate_limit("agent1", tier="low")
        assert exc_info.value.limit == 10

    def test_different_agents_independent(self):
        limiter = _make_local_limiter()
        for _ in range(10):
            limiter.check_rate_limit("agent_a", tier="low")
        result = limiter.check_rate_limit("agent_b", tier="low")
        assert result["allowed"] is True

    def test_endpoint_specific_limits(self):
        limiter = _make_local_limiter()
        for _ in range(10):
            limiter.check_rate_limit("agent1", tier="low", endpoint="/pay")
        result = limiter.check_rate_limit("agent1", tier="low", endpoint="/status")
        assert result["allowed"] is True

    def test_cost_parameter(self):
        limiter = _make_local_limiter()
        limiter.check_rate_limit("agent1", tier="low", cost=5)
        limiter.check_rate_limit("agent1", tier="low", cost=5)
        with pytest.raises(RateLimitExceeded):
            limiter.check_rate_limit("agent1", tier="low", cost=1)

    def test_window_expiry_resets_counter(self):
        limiter = _make_local_limiter()
        for _ in range(10):
            limiter.check_rate_limit("agent1", tier="low")
        key = limiter._get_key("agent1")
        limiter._local_cache[key]["reset_at"] = time.time() - 1
        result = limiter.check_rate_limit("agent1", tier="low")
        assert result["allowed"] is True
        assert result["remaining"] == 9

    def test_high_tier_allows_more(self):
        limiter = _make_local_limiter()
        for _ in range(500):
            result = limiter.check_rate_limit("agent1", tier="high")
        assert result["allowed"] is True
        assert result["remaining"] == 500


# ---------------------------------------------------------------------------
# check_rate_limit — Redis path (_check_redis)
# ---------------------------------------------------------------------------

class TestCheckRateLimitRedis:

    def test_new_window(self):
        limiter, mock_conn = _make_redis_limiter()
        pipe = mock_conn.pipeline.return_value
        pipe.execute.return_value = [(None, None)]

        result = limiter.check_rate_limit("agent1", tier="standard")
        assert result["allowed"] is True
        assert result["remaining"] == 99

    def test_existing_window_under_limit(self):
        limiter, mock_conn = _make_redis_limiter()
        pipe = mock_conn.pipeline.return_value
        future_reset = str(time.time() + 30)
        pipe.execute.return_value = [("5", future_reset)]

        result = limiter.check_rate_limit("agent1", tier="standard")
        assert result["allowed"] is True
        assert result["remaining"] == 94  # 100 - 6

    def test_existing_window_exceeds_limit(self):
        limiter, mock_conn = _make_redis_limiter()
        pipe = mock_conn.pipeline.return_value
        future_reset = str(time.time() + 30)
        pipe.execute.return_value = [("100", future_reset)]

        with pytest.raises(RateLimitExceeded) as exc_info:
            limiter.check_rate_limit("agent1", tier="standard")
        assert exc_info.value.limit == 100

    def test_expired_window_resets(self):
        limiter, mock_conn = _make_redis_limiter()
        pipe = mock_conn.pipeline.return_value
        past_reset = str(time.time() - 10)
        pipe.execute.return_value = [("999", past_reset)]

        result = limiter.check_rate_limit("agent1", tier="standard")
        assert result["allowed"] is True
        assert result["remaining"] == 99


# ---------------------------------------------------------------------------
# check_ip_rate_limit — local fallback (_check_ip_local)
# ---------------------------------------------------------------------------

class TestCheckIpRateLimitLocal:

    def test_first_request_allowed(self):
        limiter = _make_local_limiter()
        result = limiter.check_ip_rate_limit("1.2.3.4")
        assert result["allowed"] is True
        assert result["ip_remaining"] == 4

    def test_per_ip_limit_exceeded(self):
        limiter = _make_local_limiter()
        for _ in range(5):
            limiter.check_ip_rate_limit("1.2.3.4", per_ip_limit=5)
        with pytest.raises(RateLimitExceeded) as exc_info:
            limiter.check_ip_rate_limit("1.2.3.4", per_ip_limit=5)
        assert exc_info.value.limit == 5

    def test_global_limit_exceeded(self):
        limiter = _make_local_limiter()
        for i in range(3):
            limiter.check_ip_rate_limit(
                f"10.0.0.{i}", per_ip_limit=100, global_limit=3
            )
        with pytest.raises(RateLimitExceeded) as exc_info:
            limiter.check_ip_rate_limit(
                "10.0.0.99", per_ip_limit=100, global_limit=3
            )
        assert exc_info.value.limit == 3

    def test_window_expiry_resets(self):
        limiter = _make_local_limiter()
        for _ in range(5):
            limiter.check_ip_rate_limit("1.2.3.4", per_ip_limit=5)
        for key in list(limiter._local_cache):
            limiter._local_cache[key]["reset_at"] = time.time() - 1
        result = limiter.check_ip_rate_limit("1.2.3.4", per_ip_limit=5)
        assert result["allowed"] is True

    def test_different_actions_independent(self):
        limiter = _make_local_limiter()
        for _ in range(5):
            limiter.check_ip_rate_limit("1.2.3.4", action="register", per_ip_limit=5)
        result = limiter.check_ip_rate_limit("1.2.3.4", action="login", per_ip_limit=5)
        assert result["allowed"] is True


# ---------------------------------------------------------------------------
# check_ip_rate_limit — Redis path (_check_ip_redis)
# ---------------------------------------------------------------------------

class TestCheckIpRateLimitRedis:

    def test_new_window_both_keys(self):
        limiter, mock_conn = _make_redis_limiter()
        pipe = mock_conn.pipeline.return_value
        pipe.execute.return_value = [(None, None), (None, None)]

        result = limiter.check_ip_rate_limit("1.2.3.4")
        assert result["allowed"] is True

    def test_existing_window_ip_exceeds(self):
        limiter, mock_conn = _make_redis_limiter()
        pipe = mock_conn.pipeline.return_value
        future = str(time.time() + 60)
        pipe.execute.return_value = [("5", future), ("1", future)]

        with pytest.raises(RateLimitExceeded) as exc_info:
            limiter.check_ip_rate_limit("1.2.3.4", per_ip_limit=5)
        assert exc_info.value.limit == 5

    def test_existing_window_global_exceeds(self):
        limiter, mock_conn = _make_redis_limiter()
        pipe = mock_conn.pipeline.return_value
        future = str(time.time() + 60)
        pipe.execute.return_value = [("1", future), ("100", future)]

        with pytest.raises(RateLimitExceeded) as exc_info:
            limiter.check_ip_rate_limit("1.2.3.4", per_ip_limit=100, global_limit=100)
        assert exc_info.value.limit == 100

    def test_expired_ip_window_resets(self):
        limiter, mock_conn = _make_redis_limiter()
        pipe = mock_conn.pipeline.return_value
        past = str(time.time() - 10)
        future = str(time.time() + 60)
        pipe.execute.return_value = [("999", past), ("1", future)]

        result = limiter.check_ip_rate_limit("1.2.3.4", per_ip_limit=5)
        assert result["allowed"] is True

    def test_expired_global_window_resets(self):
        limiter, mock_conn = _make_redis_limiter()
        pipe = mock_conn.pipeline.return_value
        past = str(time.time() - 10)
        pipe.execute.return_value = [("1", past), ("999", past)]

        result = limiter.check_ip_rate_limit("1.2.3.4", per_ip_limit=5)
        assert result["allowed"] is True


# ---------------------------------------------------------------------------
# get_limit_status
# ---------------------------------------------------------------------------

class TestGetLimitStatus:

    def test_no_prior_requests(self):
        limiter = _make_local_limiter()
        status = limiter.get_limit_status("agent1")
        assert status["used"] == 0
        assert status["remaining"] == 100
        assert status["tier"] == "standard"

    def test_after_some_requests(self):
        limiter = _make_local_limiter()
        for _ in range(3):
            limiter.check_rate_limit("agent1", tier="standard")
        status = limiter.get_limit_status("agent1", tier="standard")
        assert status["used"] == 3
        assert status["remaining"] == 97

    def test_redis_path_with_data(self):
        limiter, mock_conn = _make_redis_limiter()
        future = time.time() + 60
        mock_conn.hmget.return_value = ("5", str(future))

        status = limiter.get_limit_status("agent1")
        assert status["used"] == 5
        assert status["remaining"] == 95

    def test_redis_path_no_data(self):
        limiter, mock_conn = _make_redis_limiter()
        mock_conn.hmget.return_value = (None, None)

        status = limiter.get_limit_status("agent1")
        assert status["used"] == 0
        assert status["remaining"] == 100


# ---------------------------------------------------------------------------
# reset_limit
# ---------------------------------------------------------------------------

class TestResetLimit:

    def test_local_reset(self):
        limiter = _make_local_limiter()
        for _ in range(5):
            limiter.check_rate_limit("agent1", tier="low")
        limiter.reset_limit("agent1")
        result = limiter.check_rate_limit("agent1", tier="low")
        assert result["remaining"] == 9

    def test_local_reset_with_endpoint(self):
        limiter = _make_local_limiter()
        limiter.check_rate_limit("agent1", tier="low", endpoint="/pay")
        limiter.reset_limit("agent1", endpoint="/pay")
        key = limiter._get_key("agent1", "/pay")
        assert key not in limiter._local_cache

    def test_local_reset_nonexistent_key(self):
        limiter = _make_local_limiter()
        # Should not raise
        limiter.reset_limit("nonexistent_agent")

    def test_redis_reset(self):
        limiter, mock_conn = _make_redis_limiter()
        limiter.reset_limit("agent1")
        mock_conn.delete.assert_called_once_with("ratelimit:agent1")


# ---------------------------------------------------------------------------
# Module-level convenience functions: get_rate_limiter, check_rate_limit
# ---------------------------------------------------------------------------

class TestModuleFunctions:

    def test_get_rate_limiter_singleton(self):
        rl_mod._limiter = None
        with patch(f"{_MOD}.REDIS_AVAILABLE", False):
            lim1 = rl_mod.get_rate_limiter()
            lim2 = rl_mod.get_rate_limiter()
        assert lim1 is lim2
        rl_mod._limiter = None

    def test_check_rate_limit_convenience(self):
        rl_mod._limiter = None
        with patch(f"{_MOD}.REDIS_AVAILABLE", False):
            result = rl_mod.check_rate_limit("agent1", tier="standard")
        assert result["allowed"] is True
        rl_mod._limiter = None

    def test_check_rate_limit_with_endpoint(self):
        rl_mod._limiter = None
        with patch(f"{_MOD}.REDIS_AVAILABLE", False):
            result = rl_mod.check_rate_limit("agent1", tier="low", endpoint="/pay")
        assert result["allowed"] is True
        rl_mod._limiter = None


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:

    def test_cache_lock_exists(self):
        limiter = _make_local_limiter()
        assert isinstance(limiter._cache_lock, type(threading.Lock()))

    def test_concurrent_access_does_not_corrupt(self):
        """Multiple threads hitting the same key should not lose counts."""
        limiter = _make_local_limiter()
        errors = []

        def hit():
            try:
                for _ in range(10):
                    limiter.check_rate_limit("shared", tier="high")
            except RateLimitExceeded:
                pass
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=hit) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        key = limiter._get_key("shared")
        assert limiter._local_cache[key]["count"] == 50  # 5 threads * 10
