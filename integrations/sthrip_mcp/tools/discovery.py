"""Discovery tools — search agents, view profiles, leaderboard. No auth required."""

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from ..client import SthripClient


def register_discovery_tools(mcp: FastMCP, client: SthripClient) -> None:
    """Register discovery tools on the MCP server."""

    @mcp.tool()
    async def search_agents(
        query: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """Search for agents registered on Sthrip.

        Args:
            query: Optional search query to filter agents by name.
            limit: Maximum number of results (default 20).
            offset: Pagination offset.

        Returns:
            JSON list of matching agent profiles.
        """
        result = await client.search_agents(query=query, limit=limit, offset=offset)
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def get_agent_profile(agent_name: str) -> str:
        """Get a specific agent's public profile by name.

        Args:
            agent_name: The unique name of the agent.

        Returns:
            JSON object with agent profile details.
        """
        result = await client.get_agent_profile(agent_name)
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def get_leaderboard(limit: int = 10) -> str:
        """Get top agents ranked by trust score.

        Args:
            limit: Number of top agents to return (default 10).

        Returns:
            JSON list of top agents with trust scores.
        """
        result = await client.get_leaderboard(limit=limit)
        return json.dumps(result, indent=2)
