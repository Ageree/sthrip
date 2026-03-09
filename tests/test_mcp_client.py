"""Tests for sthrip_mcp.client — async HTTP client."""

import pytest
import httpx
from unittest.mock import AsyncMock, patch

from integrations.sthrip_mcp.client import (
    SthripClient,
    SthripApiError,
    _build_headers,
)


@pytest.fixture
def client():
    return SthripClient(
        base_url="https://api.test.com",
        api_key="sk_test_key",
        timeout=5.0,
    )


@pytest.fixture
def unauthenticated_client():
    return SthripClient(base_url="https://api.test.com")


def _mock_response(status_code=200, json_data=None):
    """Create a mock httpx.Response."""
    resp = httpx.Response(
        status_code=status_code,
        json=json_data or {},
        request=httpx.Request("GET", "https://api.test.com"),
    )
    return resp


class TestBuildHeaders:
    def test_with_api_key(self):
        headers = _build_headers("sk_test")
        assert headers["Authorization"] == "Bearer sk_test"
        assert headers["Content-Type"] == "application/json"

    def test_without_api_key(self):
        headers = _build_headers(None)
        assert "Authorization" not in headers


class TestImmutability:
    def test_with_api_key_returns_new_instance(self, client):
        new_client = client.with_api_key("sk_new")
        assert new_client is not client
        assert new_client.api_key == "sk_new"
        assert client.api_key == "sk_test_key"


class TestDiscoveryEndpoints:
    @pytest.mark.asyncio
    async def test_search_agents(self, client):
        mock_data = [{"agent_name": "alice", "tier": "free"}]
        mock_resp = _mock_response(200, mock_data)

        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.search_agents(query="alice", limit=10)
        assert result == mock_data

    @pytest.mark.asyncio
    async def test_get_agent_profile(self, client):
        mock_data = {"agent_name": "bob", "trust_score": 0.95}
        mock_resp = _mock_response(200, mock_data)

        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.get_agent_profile("bob")
        assert result["agent_name"] == "bob"

    @pytest.mark.asyncio
    async def test_get_leaderboard(self, client):
        mock_data = [{"agent_name": "top1", "trust_score": 1.0}]
        mock_resp = _mock_response(200, mock_data)

        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.get_leaderboard(limit=5)
        assert result == mock_data


class TestRegistrationEndpoints:
    @pytest.mark.asyncio
    async def test_register_agent(self, unauthenticated_client):
        mock_data = {"agent_id": "uuid-1", "agent_name": "new_agent", "api_key": "sk_new"}
        mock_resp = _mock_response(201, mock_data)

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            result = await unauthenticated_client.register_agent("new_agent")
        assert result["api_key"] == "sk_new"

    @pytest.mark.asyncio
    async def test_get_me(self, client):
        mock_data = {"agent_name": "me", "tier": "verified"}
        mock_resp = _mock_response(200, mock_data)

        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.get_me()
        assert result["agent_name"] == "me"

    @pytest.mark.asyncio
    async def test_update_settings(self, client):
        mock_data = {"privacy_level": "high"}
        mock_resp = _mock_response(200, mock_data)

        with patch.object(httpx.AsyncClient, "patch", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.update_settings(privacy_level="high")
        assert result["privacy_level"] == "high"


class TestPaymentEndpoints:
    @pytest.mark.asyncio
    async def test_send_payment(self, client):
        mock_data = {"payment_id": "pay-1", "status": "confirmed", "amount": 0.5}
        mock_resp = _mock_response(200, mock_data)

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.send_payment("recipient", 0.5, memo="test")
        assert result["payment_id"] == "pay-1"

    @pytest.mark.asyncio
    async def test_get_payment_status(self, client):
        mock_data = {"payment_id": "pay-1", "status": "confirmed"}
        mock_resp = _mock_response(200, mock_data)

        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.get_payment_status("pay-1")
        assert result["status"] == "confirmed"

    @pytest.mark.asyncio
    async def test_get_payment_history(self, client):
        mock_data = [{"payment_id": "pay-1"}, {"payment_id": "pay-2"}]
        mock_resp = _mock_response(200, mock_data)

        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.get_payment_history(direction="out", limit=10)
        assert len(result) == 2


class TestBalanceEndpoints:
    @pytest.mark.asyncio
    async def test_get_balance(self, client):
        mock_data = {"available": 1.5, "pending": 0.1}
        mock_resp = _mock_response(200, mock_data)

        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.get_balance()
        assert result["available"] == 1.5

    @pytest.mark.asyncio
    async def test_deposit(self, client):
        mock_data = {"deposit_address": "5xxx..."}
        mock_resp = _mock_response(200, mock_data)

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.deposit()
        assert "deposit_address" in result

    @pytest.mark.asyncio
    async def test_withdraw(self, client):
        mock_data = {"status": "pending", "tx_hash": "abc123"}
        mock_resp = _mock_response(200, mock_data)

        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.withdraw(0.5, "5xMoneroAddress")
        assert result["status"] == "pending"


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_401_unauthorized(self, client):
        mock_resp = _mock_response(401, {"detail": "Invalid API key"})

        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(SthripApiError) as exc_info:
                await client.get_me()
        assert "Authentication required" in str(exc_info.value)
        assert exc_info.value.error.status_code == 401

    @pytest.mark.asyncio
    async def test_404_not_found(self, client):
        mock_resp = _mock_response(404, {"detail": "Agent not found"})

        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(SthripApiError) as exc_info:
                await client.get_agent_profile("nonexistent")
        assert exc_info.value.error.status_code == 404

    @pytest.mark.asyncio
    async def test_429_rate_limit(self, client):
        mock_resp = _mock_response(429, {"detail": "Too many requests"})

        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(SthripApiError) as exc_info:
                await client.get_balance()
        assert "Rate limit" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_500_server_error(self, client):
        mock_resp = _mock_response(500, {"detail": "Internal error"})

        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(SthripApiError) as exc_info:
                await client.get_balance()
        assert exc_info.value.error.status_code == 500
