"""Security tests for Sthrip API"""
import os
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def registered_agent(client):
    r = client.post("/v2/agents/register", json={
        "agent_name": "sec-test-agent",
        "xmr_address": "5" + "a" * 94,
    })
    assert r.status_code == 201
    return r.json()["api_key"], "sec-test-agent"


class TestAuthSecurity:
    """Auth failure should return proper status codes, not 500"""

    def test_missing_auth_returns_401(self, client):
        r = client.get("/v2/me")
        assert r.status_code == 401
        assert r.json()["detail"] == "Missing API key"

    def test_invalid_key_returns_401(self, client):
        r = client.get("/v2/me", headers={"Authorization": "Bearer sk_bogus_key"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid API key"

    def test_empty_bearer_returns_401(self, client):
        r = client.get("/v2/me", headers={"Authorization": "Bearer "})
        assert r.status_code == 401

    def test_malformed_auth_header(self, client):
        r = client.get("/v2/me", headers={"Authorization": "NotBearer token"})
        assert r.status_code in (401, 403)

    def test_admin_endpoint_no_key(self, client):
        r = client.get("/v2/admin/stats")
        assert r.status_code == 401

    def test_admin_endpoint_wrong_key(self, client):
        with patch.dict(os.environ, {"ADMIN_API_KEY": "real-secret-key"}):
            r = client.get("/v2/admin/stats", headers={"admin-key": "wrong-key"})
            assert r.status_code == 401


class TestInputValidation:
    """Validate that bad input is properly rejected"""

    def test_sql_injection_in_agent_name(self, client):
        r = client.post("/v2/agents/register", json={
            "agent_name": "'; DROP TABLE agents; --",
        })
        assert r.status_code == 422  # Pydantic rejects via regex

    def test_xss_in_agent_name(self, client):
        r = client.post("/v2/agents/register", json={
            "agent_name": "<script>alert(1)</script>",
        })
        assert r.status_code == 422

    def test_empty_agent_name(self, client):
        r = client.post("/v2/agents/register", json={
            "agent_name": "",
        })
        assert r.status_code == 422

    def test_agent_name_too_long(self, client):
        r = client.post("/v2/agents/register", json={
            "agent_name": "a" * 300,
        })
        assert r.status_code == 422

    def test_missing_required_fields(self, client):
        r = client.post("/v2/agents/register", json={})
        assert r.status_code == 422

    def test_invalid_privacy_level(self, client):
        r = client.post("/v2/agents/register", json={
            "agent_name": "valid-name",
            "privacy_level": "super_secret",
        })
        assert r.status_code == 422

    def test_negative_payment_amount(self, client, registered_agent):
        key, _ = registered_agent
        r = client.post("/v2/payments/hub-routing",
                        json={"to_agent_name": "someone", "amount": -1.0},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 422

    def test_zero_payment_amount(self, client, registered_agent):
        key, _ = registered_agent
        r = client.post("/v2/payments/hub-routing",
                        json={"to_agent_name": "someone", "amount": 0},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 422

    def test_payment_amount_exceeds_max(self, client, registered_agent):
        key, _ = registered_agent
        r = client.post("/v2/payments/hub-routing",
                        json={"to_agent_name": "someone", "amount": 99999.0},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 422

    def test_hub_payment_invalid_urgency(self, client, registered_agent):
        key, _ = registered_agent
        r = client.post("/v2/payments/hub-routing",
                        json={"to_agent_name": "someone", "amount": 1.0, "urgency": "super_fast"},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 422

    def test_withdraw_address_too_short(self, client, registered_agent):
        key, _ = registered_agent
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 1.0, "address": "short"},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 422

    def test_withdraw_valid_stagenet_address(self, client, registered_agent):
        """Valid 95-char stagenet address (prefix 5) passes validation."""
        key, _ = registered_agent
        addr = "5" + "a" * 94  # valid stagenet format
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 1.0, "address": addr},
                        headers={"Authorization": f"Bearer {key}"})
        # 400 = insufficient balance (validation passed), not 422
        assert r.status_code == 400

    def test_withdraw_wrong_network_prefix(self, client, registered_agent):
        """Mainnet address (prefix 4) rejected on stagenet."""
        key, _ = registered_agent
        addr = "4" + "a" * 94  # mainnet prefix on stagenet
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 1.0, "address": addr},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 422

    def test_withdraw_wrong_length(self, client, registered_agent):
        """Address with wrong length (not 95 or 106) is rejected."""
        key, _ = registered_agent
        addr = "5" + "a" * 80  # 81 chars, wrong length
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 1.0, "address": addr},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 422

    def test_withdraw_invalid_base58_chars(self, client, registered_agent):
        """Address with non-base58 characters (0, O, I, l) is rejected."""
        key, _ = registered_agent
        addr = "5" + "0" * 94  # '0' is not in base58 alphabet
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 1.0, "address": addr},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 422

    def test_withdraw_integrated_address_length(self, client, registered_agent):
        """Valid 106-char integrated address passes validation."""
        key, _ = registered_agent
        addr = "5" + "a" * 105  # 106 chars, integrated address length
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 1.0, "address": addr},
                        headers={"Authorization": f"Bearer {key}"})
        # 400 = insufficient balance (validation passed)
        assert r.status_code == 400


class TestRateLimiting:
    """Verify rate limit responses have proper format"""

    def test_rate_limit_returns_429(self, client):
        """When rate limiter raises, endpoint returns 429"""
        from sthrip.services.rate_limiter import RateLimitExceeded
        import time

        mock_limiter = MagicMock()
        mock_limiter.check_ip_rate_limit.side_effect = RateLimitExceeded(
            limit=5, reset_at=time.time() + 3600
        )

        with patch("api.main_v2.get_rate_limiter", return_value=mock_limiter), \
             patch("api.deps.get_rate_limiter", return_value=mock_limiter), \
             patch("api.routers.agents.get_rate_limiter", return_value=mock_limiter):
            r = client.post("/v2/agents/register", json={
                "agent_name": "rate-test",
                "xmr_address": "5" + "a" * 94,
            })
            assert r.status_code == 429
            assert "Retry-After" in r.headers


class TestSecurityHeaders:
    """All responses must include HTTP security headers."""

    def test_security_headers_present(self, client):
        response = client.get("/health")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "max-age=" in response.headers["Strict-Transport-Security"]

    def test_security_headers_on_error_response(self, client):
        response = client.get("/v2/me")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"


class TestDocsAvailable:
    """Custom branded docs are available in all environments."""

    def test_docs_available(self, client):
        response = client.get("/docs")
        assert response.status_code == 200
        assert "redoc" in response.text.lower()

    def test_default_redoc_disabled(self, client):
        """Default FastAPI /redoc is disabled (custom /docs serves Redoc instead)."""
        response = client.get("/redoc")
        assert response.status_code == 404

    def test_openapi_json_available(self, client):
        response = client.get("/openapi.json")
        assert response.status_code == 200

    def test_swagger_playground_available(self, client):
        response = client.get("/docs/playground")
        assert response.status_code == 200
        assert "swagger" in response.text.lower()


class TestDisabledEndpoints:
    """Verify disabled features return 501, not crash"""

    def test_p2p_send_returns_501(self, client):
        r = client.post("/v2/payments/send", json={})
        assert r.status_code == 501

    def test_escrow_requires_auth(self, client):
        r = client.post("/v2/escrow", json={})
        assert r.status_code == 401
