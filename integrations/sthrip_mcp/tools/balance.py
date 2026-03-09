"""Balance tools — check balance, deposit, withdraw."""

import json

from mcp.server.fastmcp import FastMCP

from ..auth import load_api_key, require_auth
from ..client import SthripClient


def register_balance_tools(mcp: FastMCP, client: SthripClient) -> None:
    """Register balance tools on the MCP server."""

    @mcp.tool()
    async def get_balance() -> str:
        """Get the current agent's XMR balance.

        Requires authentication (API key).

        Returns:
            JSON with available, pending, total_deposited, total_withdrawn,
            and deposit_address.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.get_balance()
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def deposit() -> str:
        """Get a deposit subaddress to receive XMR.

        Requires authentication (API key). Each agent gets a unique
        Monero subaddress. Deposits are auto-credited after confirmation.

        Returns:
            JSON with deposit subaddress and instructions.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.deposit()
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def withdraw(amount: float, address: str) -> str:
        """Withdraw XMR to an external Monero address.

        Requires authentication (API key).

        Args:
            amount: Amount in XMR to withdraw (max 10,000).
            address: Destination Monero address (95 or 106 chars).

        Returns:
            JSON with withdrawal status and transaction details.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.withdraw(amount=amount, address=address)
        return json.dumps(result, indent=2)
