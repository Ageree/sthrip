"""Tests for sthrip_mcp tools — all 19 tools via mocked client."""

import json
from unittest.mock import AsyncMock, patch

import pytest

mcp = pytest.importorskip("mcp", reason="MCP SDK requires Python 3.10+")
from mcp.server.fastmcp import FastMCP

from integrations.sthrip_mcp.auth import AuthError
from integrations.sthrip_mcp.client import SthripClient
from integrations.sthrip_mcp.tools.balance import register_balance_tools
from integrations.sthrip_mcp.tools.discovery import register_discovery_tools
from integrations.sthrip_mcp.tools.escrow import register_escrow_tools
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
    # Escrow
    client.escrow_create = AsyncMock(return_value={
        "escrow_id": "esc-1", "status": "pending", "amount": 1.0,
        "seller_agent_name": "seller", "description": "test deal",
    })
    client.escrow_accept = AsyncMock(return_value={
        "escrow_id": "esc-1", "status": "accepted",
    })
    client.escrow_deliver = AsyncMock(return_value={
        "escrow_id": "esc-1", "status": "delivered",
    })
    client.escrow_release = AsyncMock(return_value={
        "escrow_id": "esc-1", "status": "released", "release_amount": 1.0,
    })
    client.escrow_cancel = AsyncMock(return_value={
        "escrow_id": "esc-1", "status": "cancelled",
    })
    client.escrow_get = AsyncMock(return_value={
        "escrow_id": "esc-1", "status": "pending", "amount": 1.0,
    })
    client.escrow_list = AsyncMock(return_value=[
        {"escrow_id": "esc-1", "status": "pending"},
    ])
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
    # Escrow (will fail auth before reaching these)
    client.escrow_create = AsyncMock(return_value={})
    client.escrow_accept = AsyncMock(return_value={})
    client.escrow_deliver = AsyncMock(return_value={})
    client.escrow_release = AsyncMock(return_value={})
    client.escrow_cancel = AsyncMock(return_value={})
    client.escrow_get = AsyncMock(return_value={})
    client.escrow_list = AsyncMock(return_value=[])
    return client


def _build_mcp_with_tools(client):
    """Build a FastMCP server with all tools registered."""
    mcp = FastMCP("test")
    register_discovery_tools(mcp, client)
    register_registration_tools(mcp, client)
    register_payment_tools(mcp, client)
    register_balance_tools(mcp, client)
    register_escrow_tools(mcp, client)
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


class TestEscrowTools:
    @pytest.mark.asyncio
    async def test_escrow_create(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "escrow_create":
                tool_fn = tool.fn
                break
        result = await tool_fn(
            seller_agent_name="seller", amount=1.0, description="test deal",
        )
        data = json.loads(result)
        assert data["escrow_id"] == "esc-1"
        assert data["status"] == "pending"

    @pytest.mark.asyncio
    async def test_escrow_accept(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "escrow_accept":
                tool_fn = tool.fn
                break
        result = await tool_fn(escrow_id="esc-1")
        data = json.loads(result)
        assert data["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_escrow_deliver(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "escrow_deliver":
                tool_fn = tool.fn
                break
        result = await tool_fn(escrow_id="esc-1")
        data = json.loads(result)
        assert data["status"] == "delivered"

    @pytest.mark.asyncio
    async def test_escrow_release(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "escrow_release":
                tool_fn = tool.fn
                break
        result = await tool_fn(escrow_id="esc-1", release_amount=1.0)
        data = json.loads(result)
        assert data["status"] == "released"
        assert data["release_amount"] == 1.0

    @pytest.mark.asyncio
    async def test_escrow_cancel(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "escrow_cancel":
                tool_fn = tool.fn
                break
        result = await tool_fn(escrow_id="esc-1")
        data = json.loads(result)
        assert data["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_escrow_get(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "escrow_get":
                tool_fn = tool.fn
                break
        result = await tool_fn(escrow_id="esc-1")
        data = json.loads(result)
        assert data["escrow_id"] == "esc-1"

    @pytest.mark.asyncio
    async def test_escrow_list(self, mock_client):
        mcp = _build_mcp_with_tools(mock_client)
        tool_fn = None
        for tool in mcp._tool_manager._tools.values():
            if tool.name == "escrow_list":
                tool_fn = tool.fn
                break
        result = await tool_fn(role="buyer", limit=10, offset=0)
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["escrow_id"] == "esc-1"

    @pytest.mark.asyncio
    async def test_escrow_tools_require_auth(self, unauthenticated_client):
        mcp = _build_mcp_with_tools(unauthenticated_client)
        auth_tools = {
            "escrow_create": {"seller_agent_name": "s", "amount": 1.0, "description": "d"},
            "escrow_accept": {"escrow_id": "esc-1"},
            "escrow_deliver": {"escrow_id": "esc-1"},
            "escrow_release": {"escrow_id": "esc-1", "release_amount": 1.0},
            "escrow_cancel": {"escrow_id": "esc-1"},
            "escrow_get": {"escrow_id": "esc-1"},
            "escrow_list": {},
        }
        for tool_name, kwargs in auth_tools.items():
            tool_fn = None
            for tool in mcp._tool_manager._tools.values():
                if tool.name == tool_name:
                    tool_fn = tool.fn
                    break
            assert tool_fn is not None, f"Tool {tool_name} not found"
            with pytest.raises(AuthError):
                await tool_fn(**kwargs)


class TestServerCreation:
    def test_create_server_registers_19_tools(self):
        """Verify all 19 tools are registered."""
        from integrations.sthrip_mcp.server import create_server

        with patch.dict("os.environ", {"STHRIP_API_URL": "https://test.com"}, clear=False):
            mcp = create_server()

        tool_names = sorted(mcp._tool_manager._tools.keys())
        expected = sorted([
            "search_agents", "get_agent_profile", "get_leaderboard",
            "register_agent", "get_my_profile", "update_settings",
            "send_payment", "get_payment_status", "get_payment_history",
            "get_balance", "deposit", "withdraw",
            "escrow_create", "escrow_accept", "escrow_deliver",
            "escrow_release", "escrow_cancel", "escrow_get", "escrow_list",
        ])
        assert tool_names == expected
