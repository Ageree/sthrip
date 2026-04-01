"""
Matchmaking Service — agent discovery and automatic match assignment.

Flow:
  1. Requester submits a MatchRequest with capabilities, budget, deadline, min_rating.
  2. Service queries active agents whose SLA templates fit the constraints.
  3. Best candidate is scored via a weighted multi-factor formula.
  4. If auto_assign=True and a match exists, an SLA contract is auto-created.
  5. A background task marks SEARCHING requests as EXPIRED after 5 minutes.
"""

import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.matchmaking_repo import MatchmakingRepository
from sthrip.db.models import (
    Agent,
    AgentRatingSummary,
    MatchRequest,
    SLATemplate,
    MatchRequestStatus,
)

logger = logging.getLogger("sthrip.matchmaking")

# Expiry window for new match requests (minutes)
_MATCH_REQUEST_TTL_MINUTES = 5

# Scoring weights (must sum to 1.0)
_W_RATING = 0.4
_W_PRICE = 0.3
_W_SPEED = 0.2
_W_AVAILABILITY = 0.1

# Threshold for "recently active" agents (minutes)
_ACTIVE_THRESHOLD_MINUTES = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _request_to_dict(req: MatchRequest) -> dict:
    """Convert a MatchRequest ORM object to an immutable dict."""
    state_val = req.state.value if hasattr(req.state, "value") else req.state
    return {
        "request_id": str(req.id),
        "requester_id": str(req.requester_id),
        "task_description": req.task_description,
        "required_capabilities": req.required_capabilities or [],
        "budget": str(req.budget),
        "currency": req.currency,
        "deadline_secs": req.deadline_secs,
        "min_rating": str(req.min_rating),
        "auto_assign": req.auto_assign,
        "matched_agent_id": str(req.matched_agent_id) if req.matched_agent_id else None,
        "sla_contract_id": str(req.sla_contract_id) if req.sla_contract_id else None,
        "state": state_val,
        "created_at": _iso(req.created_at),
        "expires_at": _iso(req.expires_at),
    }


def _availability_score(agent: Agent) -> float:
    """Return 1.0 if agent was seen within the threshold window, else exponential decay."""
    if not agent.last_seen_at:
        return 0.0
    last_seen = agent.last_seen_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    age_minutes = (_now() - last_seen).total_seconds() / 60.0
    if age_minutes <= _ACTIVE_THRESHOLD_MINUTES:
        return 1.0
    # Decay: halve every additional 5 minutes beyond threshold
    excess = age_minutes - _ACTIVE_THRESHOLD_MINUTES
    return max(0.0, 1.0 - (excess / 60.0))


def _compute_score(
    agent: Agent,
    rating_summary: Optional[AgentRatingSummary],
    template: SLATemplate,
    budget: Decimal,
    deadline_secs: int,
) -> float:
    """Compute the composite match score for one candidate (higher is better)."""
    # Rating score: avg_overall / 5.0
    avg_overall = float(rating_summary.avg_overall) if rating_summary else 0.0
    rating_score = avg_overall / 5.0

    # Price score: how much cheaper the template is relative to budget
    base_price = float(template.base_price)
    bgt = float(budget)
    price_score = max(0.0, 1.0 - (base_price / bgt)) if bgt > 0 else 0.0

    # Speed score: how much faster than the deadline
    delivery = template.delivery_time_secs
    speed_score = max(0.0, 1.0 - (delivery / deadline_secs)) if deadline_secs > 0 else 0.0

    # Availability score
    avail_score = _availability_score(agent)

    return (
        _W_RATING * rating_score
        + _W_PRICE * price_score
        + _W_SPEED * speed_score
        + _W_AVAILABILITY * avail_score
    )


class MatchmakingService:
    """Business logic for agent matchmaking requests."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_request(
        self,
        db: Session,
        requester_id: UUID,
        task_description: str,
        required_capabilities: list,
        budget: Decimal,
        currency: str,
        deadline_secs: int,
        min_rating: Decimal,
        auto_assign: bool,
    ) -> dict:
        """Create a MatchRequest, run matching, optionally auto-assign.

        Returns the serialised MatchRequest dict.
        """
        repo = MatchmakingRepository(db)
        expires_at = _now() + timedelta(minutes=_MATCH_REQUEST_TTL_MINUTES)

        req = repo.create(
            requester_id=requester_id,
            task_description=task_description,
            required_capabilities=required_capabilities,
            budget=budget,
            currency=currency,
            deadline_secs=deadline_secs,
            min_rating=min_rating,
            auto_assign=auto_assign,
            expires_at=expires_at,
        )
        db.flush()

        match = self._find_best_match(db, req)

        if match is None:
            return _request_to_dict(req)

        matched_agent, _score = match

        if auto_assign:
            sla_contract_id = self._create_sla_for_match(
                db, req, matched_agent
            )
            new_state = MatchRequestStatus.ASSIGNED
        else:
            sla_contract_id = None
            new_state = MatchRequestStatus.MATCHED

        repo.update_match(
            request_id=req.id,
            matched_agent_id=matched_agent.id,
            sla_contract_id=sla_contract_id,
            state=new_state,
        )
        db.flush()

        # Re-fetch for accurate serialisation
        updated = repo.get_by_id(req.id)
        return _request_to_dict(updated)

    def _find_best_match(
        self,
        db: Session,
        request: MatchRequest,
    ) -> Optional[Tuple[Agent, float]]:
        """Find and score all qualifying agents; return the best (agent, score) or None.

        Uses Python-side filtering for SQLite compatibility (JSONB operators are
        PostgreSQL-only).
        """
        required = set(request.required_capabilities or [])
        min_rating = float(request.min_rating or 0)
        budget = Decimal(str(request.budget))

        # Fetch all active agents who are NOT the requester
        candidates = (
            db.query(Agent)
            .filter(
                Agent.is_active.is_(True),
                Agent.id != request.requester_id,
            )
            .all()
        )

        best_agent: Optional[Agent] = None
        best_score: float = -1.0

        for agent in candidates:
            # Capability filter (Python-side for SQLite compat)
            caps = set(agent.capabilities or [])
            if required and not required.issubset(caps):
                continue

            # Fetch the cheapest active SLA template within budget
            template = (
                db.query(SLATemplate)
                .filter(
                    SLATemplate.provider_id == agent.id,
                    SLATemplate.is_active.is_(True),
                    SLATemplate.base_price <= budget,
                )
                .order_by(SLATemplate.base_price)
                .first()
            )
            if template is None:
                continue

            # Rating filter
            rating_summary = (
                db.query(AgentRatingSummary)
                .filter(AgentRatingSummary.agent_id == agent.id)
                .first()
            )
            avg_overall = float(rating_summary.avg_overall) if rating_summary else 0.0
            if avg_overall < min_rating:
                continue

            score = _compute_score(
                agent=agent,
                rating_summary=rating_summary,
                template=template,
                budget=budget,
                deadline_secs=request.deadline_secs,
            )

            if score > best_score:
                best_score = score
                best_agent = agent

        if best_agent is None:
            return None
        return (best_agent, best_score)

    def accept_match(
        self,
        db: Session,
        request_id: UUID,
        requester_id: UUID,
    ) -> dict:
        """Requester accepts the matched agent and creates an SLA contract.

        Raises:
            LookupError: request not found.
            PermissionError: caller is not the requester.
            ValueError: no matched agent yet.
        """
        repo = MatchmakingRepository(db)
        req = repo.get_by_id(request_id)
        if req is None:
            raise LookupError(f"Match request {request_id} not found")
        if req.requester_id != requester_id:
            raise PermissionError("Only the requester may accept a match")
        if req.matched_agent_id is None:
            raise ValueError("No matched agent on this request yet")

        matched_agent = db.query(Agent).filter(Agent.id == req.matched_agent_id).first()
        if matched_agent is None:
            raise LookupError("Matched agent no longer exists")

        sla_contract_id = self._create_sla_for_match(db, req, matched_agent)

        repo.update_match(
            request_id=request_id,
            matched_agent_id=req.matched_agent_id,
            sla_contract_id=sla_contract_id,
            state=MatchRequestStatus.ASSIGNED,
        )
        db.flush()

        updated = repo.get_by_id(request_id)
        return _request_to_dict(updated)

    def expire_stale(self, db: Session) -> int:
        """Mark all expired SEARCHING requests as EXPIRED.

        Returns:
            Number of requests transitioned.
        """
        repo = MatchmakingRepository(db)
        stale = repo.get_expired_searching()
        count = 0
        for req in stale:
            rows = (
                db.query(MatchRequest)
                .filter(
                    MatchRequest.id == req.id,
                    MatchRequest.state == MatchRequestStatus.SEARCHING,
                )
                .update({"state": MatchRequestStatus.EXPIRED})
            )
            count += rows
        return count

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_sla_for_match(
        self,
        db: Session,
        req: MatchRequest,
        matched_agent: Agent,
    ) -> Optional[UUID]:
        """Create an SLA contract between the requester and matched agent.

        Returns the new contract's UUID, or None if creation fails.
        """
        from sthrip.services.sla_service import SLAService

        # Find the best matching template
        template = (
            db.query(SLATemplate)
            .filter(
                SLATemplate.provider_id == matched_agent.id,
                SLATemplate.is_active.is_(True),
                SLATemplate.base_price <= req.budget,
            )
            .order_by(SLATemplate.base_price)
            .first()
        )
        if template is None:
            logger.warning(
                "No SLA template found for matched agent %s during auto-assign",
                matched_agent.id,
            )
            return None

        try:
            sla_svc = SLAService()
            result = sla_svc.create_contract(
                db=db,
                consumer_id=req.requester_id,
                provider_id=matched_agent.id,
                name=template.name,
                service_description=template.service_description,
                deliverables=template.deliverables or [],
                response_time_secs=template.response_time_secs,
                delivery_time_secs=template.delivery_time_secs,
                price=Decimal(str(template.base_price)),
                currency=req.currency,
                penalty_percent=template.penalty_percent,
                template_id=template.id,
            )
            return UUID(result["contract_id"])
        except Exception as exc:
            logger.warning("Failed to create SLA contract during auto-assign: %s", exc)
            return None
