"""Spending policy tools — set and get autonomous spending limits."""

import json
from typing import List, Optional

from mcp.server.fastmcp import FastMCP

from ..auth import load_api_key, require_auth
from ..client import SthripClient


def register_spending_policy_tools(
    mcp: FastMCP,
    client: SthripClient,
) -> None:
    """Register spending policy tools on the MCP server."""

    @mcp.tool()
    async def set_spending_policy(
        max_per_tx: Optional[float] = None,
        max_daily: Optional[float] = None,
        allowed_recipients: Optional[List[str]] = None,
        require_confirmation_above: Optional[float] = None,
    ) -> str:
        """Set autonomous spending policy for the current agent.

        Defines limits that constrain hub-routing payments. Payments
        that exceed policy limits are rejected automatically, protecting
        against runaway spending by autonomous agents.

        Requires authentication (API key).

        Args:
            max_per_tx: Maximum XMR per single transaction (None to skip).
            max_daily: Maximum total XMR per 24-hour rolling window (None to skip).
            allowed_recipients: List of agent names allowed as recipients (None for unrestricted).
            require_confirmation_above: XMR threshold above which manual confirmation is required (None to skip).

        Returns:
            JSON with the updated spending policy.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.set_spending_policy(
            max_per_tx=max_per_tx,
            max_daily=max_daily,
            allowed_recipients=allowed_recipients,
            require_confirmation_above=require_confirmation_above,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def get_spending_policy() -> str:
        """Get the current agent's spending policy.

        Returns the active spending limits and restrictions
        configured for autonomous payment operations.

        Requires authentication (API key).

        Returns:
            JSON with current spending policy: max_per_tx, max_daily,
            allowed_recipients, require_confirmation_above, and usage stats.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.get_spending_policy()
        return json.dumps(result, indent=2)
