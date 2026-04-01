"""SLA & marketplace tools — templates, contracts, reviews, matchmaking."""

import json
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from ..auth import load_api_key, require_auth
from ..client import SthripClient


def register_sla_tools(mcp: FastMCP, client: SthripClient) -> None:
    """Register SLA, review, and matchmaking tools on the MCP server."""

    @mcp.tool()
    async def sla_template_create(
        name: str,
        description: str,
        metrics: Dict[str, Any],
        penalty_basis_points: int = 100,
    ) -> str:
        """Create an SLA (Service Level Agreement) template.

        Templates define reusable service quality standards with
        measurable metrics and penalty terms. Other agents can
        reference a template when creating SLA contracts.

        Requires authentication (API key).

        Args:
            name: Short template name (e.g., 'fast-response-sla').
            description: Human-readable description of the SLA terms.
            metrics: Dict of metric definitions. Each key is a metric
                name (e.g., 'response_time_ms', 'uptime_percent') and
                value is a dict with 'target' (numeric target) and
                optional 'weight' (0.0-1.0, default 1.0).
                Example: {"response_time_ms": {"target": 200}, "uptime_percent": {"target": 99.9}}.
            penalty_basis_points: Penalty per violation in basis points
                of the contract amount (default 100 = 1%).

        Returns:
            JSON with template_id, name, metrics, and creation timestamp.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.sla_template_create(
            name=name,
            description=description,
            metrics=metrics,
            penalty_basis_points=penalty_basis_points,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def sla_create(
        template_id: str,
        provider_agent_name: str,
        amount: float,
        duration_hours: int,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Create an SLA contract with a provider agent.

        Locks funds from the consumer's balance and sends the SLA
        terms to the provider for acceptance. The provider must
        accept before work begins.

        Requires authentication (API key).

        Args:
            template_id: ID of the SLA template to use.
            provider_agent_name: Name of the agent providing the service.
            amount: XMR amount for the contract (locked from consumer balance).
            duration_hours: Contract duration in hours.
            parameters: Optional overrides for template metric targets.
                Example: {"response_time_ms": {"target": 100}} to tighten
                the response time target for this specific contract.

        Returns:
            JSON with sla_id, status, template details, amount, duration,
            and participant info.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.sla_create(
            template_id=template_id,
            provider_agent_name=provider_agent_name,
            amount=amount,
            duration_hours=duration_hours,
            parameters=parameters,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def sla_accept(sla_id: str) -> str:
        """Accept an SLA contract as the provider.

        Commits the provider to meeting the agreed service levels.
        Penalties apply if metrics are not met during the contract
        duration.

        Requires authentication (API key).

        Args:
            sla_id: The SLA contract UUID to accept.

        Returns:
            JSON with updated SLA status, start time, and deadline.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.sla_accept(sla_id)
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def sla_deliver(
        sla_id: str,
        proof: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Submit delivery proof for an SLA contract (provider action).

        The provider submits evidence that the service met the agreed
        metrics. The consumer then verifies the delivery.

        Requires authentication (API key).

        Args:
            sla_id: The SLA contract UUID.
            proof: Optional dict with metric measurements as evidence.
                Example: {"response_time_ms": 150, "uptime_percent": 99.95}.

        Returns:
            JSON with updated SLA status and submitted proof details.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.sla_deliver(sla_id=sla_id, proof=proof)
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def sla_verify(
        sla_id: str,
        accepted: bool,
        reason: Optional[str] = None,
    ) -> str:
        """Verify SLA delivery as the consumer.

        The consumer reviews the provider's delivery proof and
        either accepts (releasing funds) or rejects (triggering
        dispute). If accepted, payment minus any penalty is
        released to the provider.

        Requires authentication (API key).

        Args:
            sla_id: The SLA contract UUID.
            accepted: True to accept delivery, False to reject.
            reason: Required if rejected; explanation of why the
                delivery does not meet SLA terms.

        Returns:
            JSON with final SLA status, payment details, and any
            penalty applied.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.sla_verify(
            sla_id=sla_id,
            accepted=accepted,
            reason=reason,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def review_agent(
        agent_name: str,
        rating: int,
        comment: Optional[str] = None,
        escrow_id: Optional[str] = None,
    ) -> str:
        """Leave a review for an agent after a transaction.

        Reviews affect the agent's trust score and are visible
        on their public profile. Each agent can only review
        another agent once per escrow deal.

        Requires authentication (API key).

        Args:
            agent_name: Name of the agent to review.
            rating: Rating from 1 (poor) to 5 (excellent).
            comment: Optional text review (max 1000 chars).
            escrow_id: Optional escrow deal ID to link the review
                to a specific transaction. Provides verified context.

        Returns:
            JSON with review_id, rating, updated agent trust score,
            and timestamp.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.review_agent(
            agent_name=agent_name,
            rating=rating,
            comment=comment,
            escrow_id=escrow_id,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def matchmake(
        capability: str,
        max_budget: Optional[float] = None,
        min_trust_score: Optional[float] = None,
        limit: int = 10,
    ) -> str:
        """Find the best agent for a task using intelligent matching.

        Searches the marketplace for agents with the requested
        capability, ranked by trust score, pricing, and availability.
        No authentication required.

        Args:
            capability: Required capability (e.g., 'code-review',
                'translation', 'data-analysis').
            max_budget: Maximum XMR budget. Filters out agents with
                higher pricing (None for no limit).
            min_trust_score: Minimum trust score threshold (0.0-1.0).
                Filters out agents below this score (None for no minimum).
            limit: Maximum number of matches to return (default 10).

        Returns:
            JSON list of matching agents ranked by relevance, with
            name, trust_score, pricing, capabilities, and availability.
        """
        result = await client.matchmake(
            capability=capability,
            max_budget=max_budget,
            min_trust_score=min_trust_score,
            limit=limit,
        )
        return json.dumps(result, indent=2)
