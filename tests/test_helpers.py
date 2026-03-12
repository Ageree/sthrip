"""Tests for api/helpers.py — thread-safe singletons."""

import threading
from unittest.mock import patch, MagicMock


def test_get_wallet_service_uses_double_checked_locking():
    """get_wallet_service must use a threading lock for initialization."""
    import api.helpers as helpers_module

    assert hasattr(helpers_module, "_wallet_lock")
    assert isinstance(helpers_module._wallet_lock, type(threading.Lock()))


def test_get_rate_limiter_uses_double_checked_locking():
    """get_rate_limiter must use a threading lock for initialization."""
    import sthrip.services.rate_limiter as rl_module

    assert hasattr(rl_module, "_limiter_lock")
    assert isinstance(rl_module._limiter_lock, type(threading.Lock()))


def test_get_wallet_service_creates_single_instance_under_concurrency():
    """Concurrent calls to get_wallet_service must produce exactly one instance."""
    import api.helpers as helpers_module

    # Reset singleton
    helpers_module._wallet_service = None

    mock_service = MagicMock()
    call_count = 0
    original_lock = helpers_module._wallet_lock

    def mock_from_env(**kwargs):
        nonlocal call_count
        call_count += 1
        return mock_service

    results = []
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        svc = helpers_module.get_wallet_service()
        results.append(svc)

    with patch("api.helpers.WalletService") as mock_cls:
        mock_cls.from_env = mock_from_env
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # All threads must get the same instance
    assert all(r is results[0] for r in results), "Not all threads got the same instance"
    # Factory must be called exactly once
    assert call_count == 1, f"from_env called {call_count} times, expected 1"

    # Cleanup
    helpers_module._wallet_service = None


def test_get_rate_limiter_creates_single_instance_under_concurrency():
    """Concurrent calls to get_rate_limiter must produce exactly one instance."""
    import sthrip.services.rate_limiter as rl_module

    # Reset singleton
    rl_module._limiter = None

    call_count = 0
    mock_instance = MagicMock()

    original_init = rl_module.RateLimiter.__init__

    def counting_init(self):
        nonlocal call_count
        call_count += 1

    results = []
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        limiter = rl_module.get_rate_limiter()
        results.append(limiter)

    with patch.object(rl_module.RateLimiter, "__init__", counting_init):
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # All threads must get the same instance
    assert all(r is results[0] for r in results), "Not all threads got the same instance"
    # Constructor must be called exactly once
    assert call_count == 1, f"RateLimiter() called {call_count} times, expected 1"

    # Cleanup
    rl_module._limiter = None
