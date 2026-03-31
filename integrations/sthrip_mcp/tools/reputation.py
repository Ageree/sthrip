"""ZK reputation tools — generate and verify zero-knowledge reputation proofs."""

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from ..auth import load_api_key, require_auth
from ..client import SthripClient


def register_reputation_tools(
    mcp: FastMCP,
    client: SthripClient,
) -> None:
    """Register ZK reputation tools on the MCP server."""

    @mcp.tool()
    async def reputation_proof(
        claim_type: str,
        threshold: Optional[float] = None,
    ) -> str:
        """Generate a zero-knowledge proof of reputation.

        Creates a ZK proof that demonstrates a reputation claim
        (e.g., trust score above a threshold) without revealing
        the exact score or transaction history.

        Requires authentication (API key).

        Args:
            claim_type: Type of reputation claim. One of:
                'trust_above' — prove trust score exceeds threshold,
                'tx_count_above' — prove transaction count exceeds threshold,
                'account_age_days' — prove account is older than threshold days.
            threshold: Numeric threshold for the claim (required for
                'trust_above', 'tx_count_above', 'account_age_days').

        Returns:
            JSON with proof (opaque base64 string), claim_type,
            threshold, and expiry timestamp. The proof can be
            shared with other agents for verification.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.generate_reputation_proof(
            claim_type=claim_type,
            threshold=threshold,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def verify_reputation(
        proof: str,
        claim_type: str,
        agent_id: Optional[str] = None,
    ) -> str:
        """Verify a zero-knowledge reputation proof.

        Validates a ZK proof without learning any private
        information beyond whether the claim is true. No
        authentication required for verification.

        Args:
            proof: The base64-encoded ZK proof string to verify.
            claim_type: The claimed reputation type (must match
                the type used to generate the proof).
            agent_id: Optional agent ID to bind the proof to
                a specific agent (prevents proof reuse).

        Returns:
            JSON with valid (boolean), claim_type, threshold,
            and expiry. If the proof is invalid or expired,
            valid is false with a reason.
        """
        result = await client.verify_reputation_proof(
            proof=proof,
            claim_type=claim_type,
            agent_id=agent_id,
        )
        return json.dumps(result, indent=2)
