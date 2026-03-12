"""Tests for admin session-token auth (CRIT-4).

Covers:
- POST /v2/admin/auth with correct admin key returns bearer token
- GET /v2/admin/stats with bearer token succeeds
- GET /v2/admin/stats with raw admin key in header still works (deprecation)
- Expired token returns 401
- GET /metrics with bearer token succeeds
"""

import os
import time
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from contextlib import ExitStack

ADMIN_KEY = "test-admin-key-for-session-auth"


@pytest.fixture
def api_client():
    """Test client with all dependencies mocked."""
    mock_limiter = MagicMock()
    mock_limiter.check_rate_limit.return_value = None

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy", "timestamp": "2026-03-09T00:00:00", "checks": {}
    }
    mock_monitor.get_alerts.return_value = []
    mock_monitor.start_monitoring.return_value = None
    mock_monitor.stop_monitoring.return_value = None

    mock_webhook = MagicMock()
    mock_webhook.start_worker = MagicMock(return_value=MagicMock())
    mock_webhook.stop_worker.return_value = None
    mock_webhook.close = MagicMock(return_value=MagicMock())
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    mock_registry = MagicMock()
    mock_registry.get_stats.return_value = {
        "total_agents": 3,
        "by_tier": {"free": 1, "verified": 1, "premium": 1},
        "active_24h": 2,
    }

    mock_collector = MagicMock()
    mock_collector.get_revenue_stats.return_value = {
        "total_fees": 0.5, "period_days": 30
    }

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "HUB_MODE": "ledger",
            "DATABASE_URL": "sqlite:///:memory:",
            "ENVIRONMENT": "dev",
            "ADMIN_API_KEY": ADMIN_KEY,
        }))

        stack.enter_context(patch("sthrip.services.monitoring.get_monitor", return_value=mock_monitor))
        stack.enter_context(patch("sthrip.services.monitoring.setup_default_monitoring", return_value=mock_monitor))
        stack.enter_context(patch("sthrip.services.webhook_service.get_webhook_service", return_value=mock_webhook))
        stack.enter_context(patch("sthrip.services.rate_limiter.get_rate_limiter", return_value=mock_limiter))
        stack.enter_context(patch("sthrip.db.database.create_tables"))
        stack.enter_context(patch("sthrip.db.database.get_engine", return_value=MagicMock()))
        stack.enter_context(patch("sthrip.services.agent_registry.get_registry", return_value=mock_registry))
        stack.enter_context(patch("api.routers.admin.get_registry", return_value=mock_registry))
        stack.enter_context(patch("sthrip.services.fee_collector.get_fee_collector", return_value=mock_collector))
        stack.enter_context(patch("api.routers.admin.get_fee_collector", return_value=mock_collector))
        stack.enter_context(patch("sthrip.services.metrics.get_metrics_response", return_value=("metrics_data", "text/plain")))
        stack.enter_context(patch("api.routers.health.get_metrics_response", return_value=("metrics_data", "text/plain")))
        stack.enter_context(patch("api.routers.admin.get_webhook_service", return_value=mock_webhook))
        stack.enter_context(patch("api.routers.admin.get_monitor", return_value=mock_monitor))

        for mod in [
            "api.deps",
            "api.routers.agents",
            "api.routers.payments",
            "api.routers.balance",
            "api.routers.admin",
        ]:
            try:
                stack.enter_context(patch(f"{mod}.audit_log"))
            except AttributeError:
                pass

        import importlib
        import api.main_v2 as main_mod
        importlib.reload(main_mod)

        app = main_mod.create_app()
        client = TestClient(app, raise_server_exceptions=False)
        yield client


def test_admin_auth_returns_bearer_token(api_client):
    """POST /v2/admin/auth with correct admin key returns bearer token."""
    resp = api_client.post("/v2/admin/auth", json={"admin_key": ADMIN_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert "token" in data
    assert "expires_in" in data
    assert data["expires_in"] == 28800
    assert len(data["token"]) > 20


def test_admin_auth_rejects_wrong_key(api_client):
    """POST /v2/admin/auth with wrong key returns 401."""
    resp = api_client.post("/v2/admin/auth", json={"admin_key": "wrong-key"})
    assert resp.status_code == 401


def test_admin_stats_with_bearer_token(api_client):
    """GET /v2/admin/stats with bearer token succeeds."""
    # First get a token
    resp = api_client.post("/v2/admin/auth", json={"admin_key": ADMIN_KEY})
    token = resp.json()["token"]

    # Use bearer token
    resp = api_client.get(
        "/v2/admin/stats",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "agents" in data


def test_admin_stats_with_raw_key_rejected(api_client):
    """GET /v2/admin/stats with raw admin_key header is rejected (removed)."""
    resp = api_client.get(
        "/v2/admin/stats",
        headers={"admin-key": ADMIN_KEY},
    )
    assert resp.status_code == 401


def test_admin_stats_no_auth_returns_401(api_client):
    """GET /v2/admin/stats with no auth returns 401."""
    resp = api_client.get("/v2/admin/stats")
    assert resp.status_code == 401


def test_expired_token_returns_401(api_client):
    """Expired session token returns 401."""
    # Use a fabricated token that is not in any store
    fake_token = "this-token-does-not-exist-in-any-store-1234567890"

    resp = api_client.get(
        "/v2/admin/stats",
        headers={"Authorization": f"Bearer {fake_token}"},
    )
    assert resp.status_code == 401


def test_metrics_with_bearer_token(api_client):
    """GET /metrics with bearer token succeeds."""
    resp = api_client.post("/v2/admin/auth", json={"admin_key": ADMIN_KEY})
    token = resp.json()["token"]

    resp = api_client.get(
        "/metrics",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


def test_metrics_with_raw_key_rejected(api_client):
    """GET /metrics with raw admin_key header is rejected (removed)."""
    resp = api_client.get(
        "/metrics",
        headers={"admin-key": ADMIN_KEY},
    )
    assert resp.status_code == 401


def test_verify_agent_with_bearer_token(api_client):
    """POST /v2/admin/agents/{id}/verify with bearer token works (auth passes)."""
    resp = api_client.post("/v2/admin/auth", json={"admin_key": ADMIN_KEY})
    token = resp.json()["token"]

    # Mock registry returns success, so auth passing means 200
    resp = api_client.post(
        "/v2/admin/agents/00000000-0000-0000-0000-000000000001/verify",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


def test_verify_agent_no_auth_returns_401(api_client):
    """POST /v2/admin/agents/{id}/verify without auth returns 401."""
    resp = api_client.post(
        "/v2/admin/agents/00000000-0000-0000-0000-000000000001/verify",
    )
    assert resp.status_code == 401


def test_admin_ui_login_success_not_blocked_after_failed_attempts(api_client):
    """Successful admin UI login should not be blocked by prior failed attempts.

    Regression test: rate limit counter used to increment BEFORE password check,
    causing valid admins to get locked out after N failed attempts from same IP.
    """
    import re
    from sthrip.services.rate_limiter import RateLimiter

    # Use a real (local-mode) rate limiter to track counters
    real_limiter = RateLimiter.__new__(RateLimiter)
    real_limiter.default_tier = "standard"
    real_limiter._local_cache = {}
    real_limiter._cache_lock = __import__("threading").Lock()
    real_limiter._last_eviction = 0.0
    real_limiter.use_redis = False
    real_limiter.redis = None

    with patch("sthrip.services.rate_limiter.get_rate_limiter", return_value=real_limiter):
        # 4 failed login attempts via admin UI
        for i in range(4):
            # Get CSRF token from login page
            login_page = api_client.get("/admin/login")
            match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
            assert match, "No csrf_token found in login form"
            csrf = match.group(1)

            resp = api_client.post(
                "/admin/login",
                data={"admin_key": f"wrong-key-{i}", "csrf_token": csrf},
            )
            assert resp.status_code == 401, f"Failed attempt {i+1} should return 401"

        # 5th attempt with CORRECT key — should succeed, NOT 429
        login_page = api_client.get("/admin/login")
        match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
        csrf = match.group(1)

        resp = api_client.post(
            "/admin/login",
            data={"admin_key": ADMIN_KEY, "csrf_token": csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303, (
            f"Expected redirect (303) on successful login, got {resp.status_code}. "
            "Successful login should not be counted toward rate limit."
        )

        # Verify the successful attempt did NOT increment the counter
        # (counter should still be at 4, not 5)
        ip_key = "ratelimit:ip:admin_login:testclient"
        entry = real_limiter._local_cache.get(ip_key)
        assert entry is not None, "Rate limit entry should exist for the IP"
        assert entry["count"] == 4, (
            f"Expected counter=4 (only failed attempts), got {entry['count']}. "
            "Successful logins must not increment the rate limit counter."
        )
