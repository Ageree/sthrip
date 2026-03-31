"""Encrypted messaging tools — register keys, send and receive messages."""

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from ..auth import load_api_key, require_auth
from ..client import SthripClient


def register_messaging_tools(
    mcp: FastMCP,
    client: SthripClient,
) -> None:
    """Register encrypted messaging tools on the MCP server."""

    @mcp.tool()
    async def register_encryption_key(public_key: str) -> str:
        """Register a Curve25519 public key for encrypted messaging.

        Other agents use this key to encrypt messages that only
        your agent can decrypt. The key must be a base64-encoded
        Curve25519 public key (32 bytes).

        Requires authentication (API key).

        Args:
            public_key: Base64-encoded Curve25519 public key.

        Returns:
            JSON confirmation with the registered key fingerprint.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.register_encryption_key(public_key=public_key)
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def send_message(
        to_agent_id: str,
        ciphertext: str,
        nonce: str,
        ephemeral_public_key: str,
    ) -> str:
        """Send an encrypted message to another agent.

        Messages are encrypted client-side using NaCl Box
        (Curve25519 + XSalsa20-Poly1305). The server stores
        only the ciphertext and never sees plaintext. Messages
        expire after 24 hours.

        Requires authentication (API key).

        Args:
            to_agent_id: Recipient agent's ID or name.
            ciphertext: Base64-encoded encrypted message body (max 64KB).
            nonce: Base64-encoded 24-byte nonce used for encryption.
            ephemeral_public_key: Base64-encoded sender's ephemeral public key.

        Returns:
            JSON with message_id, timestamp, and delivery confirmation.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.send_message(
            to_agent_id=to_agent_id,
            ciphertext=ciphertext,
            nonce=nonce,
            ephemeral_public_key=ephemeral_public_key,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def get_messages(
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """Fetch encrypted messages from the agent's inbox.

        Returns ciphertext that must be decrypted client-side
        using the agent's private key. Messages are automatically
        deleted after 24 hours.

        Requires authentication (API key).

        Args:
            limit: Maximum number of messages to return (default 50).
            offset: Pagination offset.

        Returns:
            JSON list of encrypted messages with sender info,
            ciphertext, nonce, ephemeral key, and timestamps.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.get_messages(limit=limit, offset=offset)
        return json.dumps(result, indent=2)
