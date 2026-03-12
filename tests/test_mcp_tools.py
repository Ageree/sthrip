"""Tests for sthrip_mcp tools — all 12 tools via mocked client."""

import json
from unittest.mock import AsyncMock, patch

import pytest

mcp = pytest.importorskip("mcp", reason="MCP SDK requires Python 3.10+")
from mcp.server.fastmcp import FastMCP

from integrations.sthrip_mcp.auth import AuthError
from integrations.sthrip_mcp.client import SthripClient
from integrations.sthrip_mcp.tools.balance import register_balance_tools
from integrations.sthrip_mcp.tools.discovery import register_discovery_tools
from integrations.sthrip_mcp.tools.payments import register_payment_tools
from integrations.sthrip_mcp.tools.registration import register_registration_tools


@pytest.fixture
def mock_client():
    """Create a SthripClient with all methods mocked."""
    client = SthripClient(base_url="https://test.com", api_key="sk_test")
    # Discovery
    client.search_agents = AsyncMock(return_value=[{"agent_name": "alice"}])
    client.get_agent_profile = AsyncMock(return_value={"agent_name": "bob", "tier": "free"})
    client.get_leaderboard = AsyncMock(return_value=[{"agent_name": "top", "trust_score": 1.0}])
    # Registration
    client.register_agent = AsyncMock(return_value={
        "agent_id": "uuid-1", "agent_name": "new", "api_key": "sk_new_key", "tier": "free",
    })
    client.get_me = AsyncMock(return_value={"agent_name": "me", "tier": "verified"})
    client.update_settings = AsyncMock(return_value={"privacy_level": "high"})
    # Payments
    client.send_payment = AsyncMock(return_value={
        "payment_id": "pay-1", "status": "confirmed", "amount": 0.5, "fee": 0.005,
    })
    client.get_payment_status = AsyncMock(return_value={"payment_id": "pay-1", "status": "confirmed"})
    client.get_payment_history = AsyncMock(return_value=[{"payment_id": "pay-1"}])
    # Balance
    client.get_balance = AsyncMock(return_value={"available": 1.0, "pending": 0.0})
    client.deposit = AsyncMock(return_value={"deposit_address": "5xxx"})
    client.withdraw = AsyncMock(return_value={"status": "pending", "tx_hash": "abc"})
    return client


@pytest.fixture
def unauthenticated_client():
    """Client without API key."""
    client = SthripClient(base_url="https://test.com", api_key=None)
    client.search_agents = AsyncMock(return_value=[])
    client.get_agent_profile = AsyncMock(return_value={})
    client.get_leaderboard = AsyncMock(return_value=[])
    client.register_agent = AsyncMock(return_value={
        "agent_id": "uuid-1", "agent_name": "new", "api_key": "sk_new", "tier": "free",
    })
    return client


def _build_mcp_with_tools(client):
    """Build a FastMCP server with all tools registered."""
    mcp = FastMCP("test")
    register_discovery_tools(mcp, client)
    register_registration_tools(mcp, client)
    register_payment_tools(mcp, client)
    register_balance_tools(mcp, client)
    return mcp


class TestDiscoveryTools:
    @pytest.mark.asyncio
    async def test_search_agents(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "search_agents":
                tool_fn = tool.fn
                break
        result = await tool_fn(query="alice", limit=10, offset=0)
        data = json.loads(result)
        assert data[0]["agent_name"] == "alice"

    @pytest.mark.asyncio
    async def test_get_agent_profile(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "get_agent_profile":
                tool_fn = tool.fn
                break
        result = await tool_fn(agent_name="bob")
        data = json.loads(result)
        assert data["agent_name"] == "bob"

    @pytest.mark.asyncio
    async def test_get_leaderboard(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "get_leaderboard":
                tool_fn = tool.fn
                break
        result = await tool_fn(limit=5)
        data = json.loads(result)
        assert len(data) == 1


class TestRegistrationTools:
    @pytest.mark.asyncio
    async def test_register_agent_saves_key(self, unauthenticated_client, tmp_path):
        mcp = _build_mcp_with_tools(unauthenticated_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "register_agent":
                tool_fn = tool.fn
                break

        creds_file = tmp_path / "credentials.json"
        with patch("integrations.sthrip_mcp.tools.registration.save_api_key", return_value=creds_file) as mock_save:
            result = await tool_fn(agent_name="new_agent", privacy_level="medium")

        mock_save.assert_called_once_with("sk_new")
        data = json.loads(result)
        assert "api_key" not in data  # Key must NOT be in output
        assert data["credentials_saved_to"] == str(creds_file)

    @pytest.mark.asyncio
    async def test_get_my_profile_requires_auth(self, unauthenticated_client):
        mcp = _build_mcp_with_tools(unauthenticated_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "get_my_profile":
                tool_fn = tool.fn
                break

        with pytest.raises(AuthError):
            await tool_fn()

    @pytest.mark.asyncio
    async def test_get_my_profile_with_auth(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "get_my_profile":
                tool_fn = tool.fn
                break
        result = await tool_fn()
        data = json.loads(result)
        assert data["agent_name"] == "me"

    @pytest.mark.asyncio
    async def test_update_settings_requires_auth(self, unauthenticated_client):
        mcp = _build_mcp_with_tools(unauthenticated_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "update_settings":
                tool_fn = tool.fn
                break
        with pytest.raises(AuthError):
            await tool_fn(privacy_level="high")


class TestPaymentTools:
    @pytest.mark.asyncio
    async def test_send_payment(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "send_payment":
                tool_fn = tool.fn
                break
        result = await tool_fn(to_agent_name="alice", amount=0.5, urgency="normal")
        data = json.loads(result)
        assert data["payment_id"] == "pay-1"
        assert data["status"] == "confirmed"

    @pytest.mark.asyncio
    async def test_send_payment_requires_auth(self, unauthenticated_client):
        mcp = _build_mcp_with_tools(unauthenticated_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "send_payment":
                tool_fn = tool.fn
                break
        with pytest.raises(AuthError):
            await tool_fn(to_agent_name="alice", amount=0.5)

    @pytest.mark.asyncio
    async def test_get_payment_status(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "get_payment_status":
                tool_fn = tool.fn
                break
        result = await tool_fn(payment_id="pay-1")
        data = json.loads(result)
        assert data["status"] == "confirmed"

    @pytest.mark.asyncio
    async def test_get_payment_history(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "get_payment_history":
                tool_fn = tool.fn
                break
        result = await tool_fn(direction="out", limit=10, offset=0)
        data = json.loads(result)
        assert len(data) == 1


class TestBalanceTools:
    @pytest.mark.asyncio
    async def test_get_balance(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "get_balance":
                tool_fn = tool.fn
                break
        result = await tool_fn()
        data = json.loads(result)
        assert data["available"] == 1.0

    @pytest.mark.asyncio
    async def test_deposit(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "deposit":
                tool_fn = tool.fn
                break
        result = await tool_fn()
        data = json.loads(result)
        assert "deposit_address" in data

    @pytest.mark.asyncio
    async def test_withdraw(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "withdraw":
                tool_fn = tool.fn
                break
        result = await tool_fn(amount=0.5, address="5xTestAddr")
        data = json.loads(result)
        assert data["status"] == "pending"

    @pytest.mark.asyncio
    async def test_balance_tools_require_auth(self, unauthenticated_client):
        mcp = _build_mcp_with_tools(unauthenticated_client)
        for tool_name in ("get_balance", "deposit", "withdraw"):
            tool_fn = None
            for tool in mcp._tool_manager._tools.values():
                if tool.name == tool_name:
                    tool_fn = tool.fn
                    break
            with pytest.raises(AuthError):
                if tool_name == "withdraw":
                    await tool_fn(amount=0.5, address="5xAddr")
                else:
                    await tool_fn()


class TestServerCreation:
    def test_create_server_registers_12_tools(self):
        """Verify all 12 tools are registered."""
        from integrations.sthrip_mcp.server import create_server

        with patch.dict("os.environ", {"STHRIP_API_URL": "https://test.com"}, clear=False):
            mcp = create_server()

        tool_names = sorted(mcp._tool_manager._tools.keys())
        expected = sorted([
            "search_agents", "get_agent_profile", "get_leaderboard",
            "register_agent", "get_my_profile", "update_settings",
            "send_payment", "get_payment_status", "get_payment_history",
            "get_balance", "deposit", "withdraw",
        ])
        assert tool_names == expected
