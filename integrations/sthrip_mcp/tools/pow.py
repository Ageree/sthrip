"""PoW registration tools — get proof-of-work challenge for bot-resistant registration."""

import json

from mcp.server.fastmcp import FastMCP

from ..client import SthripClient


def register_pow_tools(mcp: FastMCP, client: SthripClient) -> None:
    """Register proof-of-work tools on the MCP server."""

    @mcp.tool()
    async def get_pow_challenge() -> str:
        """Get a proof-of-work challenge for agent registration.

        Returns a SHA-256 puzzle that must be solved before
        registering a new agent. This prevents spam registration
        by requiring computational work. The challenge includes
        a prefix and difficulty (number of leading zero bits
        required in the hash).

        No authentication required.

        Returns:
            JSON with challenge_id, prefix (hex string to include
            in the hash input), difficulty_bits (number of leading
            zeros required), algorithm ('sha256'), and expires_at
            timestamp. Solve by finding a nonce such that
            SHA256(prefix + nonce) has the required leading zero bits.
        """
        result = await client.get_pow_challenge()
        return json.dumps(result, indent=2)
