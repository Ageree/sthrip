"""Registration & profile tools — register, view/update own profile."""

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from ..auth import load_api_key, require_auth, save_api_key
from ..client import SthripClient


def register_registration_tools(
    mcp: FastMCP,
    client: SthripClient,
) -> None:
    """Register agent registration and profile tools."""

    @mcp.tool()
    async def register_agent(
        agent_name: str,
        privacy_level: str = "medium",
        webhook_url: Optional[str] = None,
    ) -> str:
        """Register a new agent on Sthrip.

        No authentication required. After registration, the API key is
        automatically saved to ~/.sthrip/credentials.json for future use.

        Args:
            agent_name: Unique name (alphanumeric, hyphens, underscores, 3-255 chars).
            privacy_level: One of: low, medium, high, paranoid (default: medium).
            webhook_url: Optional URL for payment notifications.

        Returns:
            JSON with agent_id, agent_name, tier, and confirmation.
            The API key is saved locally (never shown in output).
        """
        result = await client.register_agent(
            agent_name=agent_name,
            privacy_level=privacy_level,
            webhook_url=webhook_url,
        )

        # Save API key locally, then strip it from the response
        api_key = result.get("api_key")
        if api_key:
            path = save_api_key(api_key)
            safe_result = {k: v for k, v in result.items() if k != "api_key"}
            safe_result["credentials_saved_to"] = str(path)
            return json.dumps(safe_result, indent=2)

        return json.dumps(result, indent=2)

    @mcp.tool()
    async def get_my_profile() -> str:
        """Get the current agent's profile information.

        Requires authentication (API key).

        Returns:
            JSON with agent details: name, tier, privacy level, trust score, etc.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.get_me()
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def update_settings(
        webhook_url: Optional[str] = None,
        privacy_level: Optional[str] = None,
    ) -> str:
        """Update the current agent's settings.

        Requires authentication (API key).

        Args:
            webhook_url: New webhook URL for payment notifications (or None to skip).
            privacy_level: New privacy level: low, medium, high, paranoid (or None to skip).

        Returns:
            JSON with updated agent settings.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.update_settings(
            webhook_url=webhook_url,
            privacy_level=privacy_level,
        )
        return json.dumps(result, indent=2)
