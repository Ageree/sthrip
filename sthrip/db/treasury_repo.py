"""
TreasuryRepository -- data-access layer for treasury management.

Handles TreasuryPolicy, TreasuryForecast, and TreasuryRebalanceLog records.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Any
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import desc

from .models import TreasuryPolicy, TreasuryForecast, TreasuryRebalanceLog
from ._repo_base import _MAX_QUERY_LIMIT


class TreasuryRepository:
    """Treasury management data access."""

    def __init__(self, db: Session):
        self.db = db

    # ── Policy ────────────────────────────────────────────────────────────

    def get_policy(self, agent_id: UUID) -> Optional[TreasuryPolicy]:
        """Get treasury policy for an agent."""
        return self.db.query(TreasuryPolicy).filter(
            TreasuryPolicy.agent_id == agent_id,
        ).first()

    def set_policy(
        self,
        agent_id: UUID,
        target_allocation: Dict[str, int],
        rebalance_threshold_pct: int = 10,
        rebalance_cooldown_secs: int = 300,
        min_liquid_xmr: Optional[Decimal] = None,
        min_liquid_xusd: Optional[Decimal] = None,
        emergency_reserve_pct: int = 10,
        auto_lend_enabled: bool = False,
        max_lend_pct: int = 20,
        min_borrower_trust_score: int = 70,
        max_loan_duration_secs: int = 3600,
    ) -> TreasuryPolicy:
        """Create or update treasury policy for an agent (upsert)."""
        policy = self.get_policy(agent_id)
        if policy is None:
            policy = TreasuryPolicy(
                agent_id=agent_id,
                target_allocation=target_allocation,
                rebalance_threshold_pct=rebalance_threshold_pct,
                rebalance_cooldown_secs=rebalance_cooldown_secs,
                min_liquid_xmr=min_liquid_xmr,
                min_liquid_xusd=min_liquid_xusd,
                emergency_reserve_pct=emergency_reserve_pct,
                auto_lend_enabled=auto_lend_enabled,
                max_lend_pct=max_lend_pct,
                min_borrower_trust_score=min_borrower_trust_score,
                max_loan_duration_secs=max_loan_duration_secs,
            )
            self.db.add(policy)
        else:
            policy.target_allocation = target_allocation
            policy.rebalance_threshold_pct = rebalance_threshold_pct
            policy.rebalance_cooldown_secs = rebalance_cooldown_secs
            policy.min_liquid_xmr = min_liquid_xmr
            policy.min_liquid_xusd = min_liquid_xusd
            policy.emergency_reserve_pct = emergency_reserve_pct
            policy.auto_lend_enabled = auto_lend_enabled
            policy.max_lend_pct = max_lend_pct
            policy.min_borrower_trust_score = min_borrower_trust_score
            policy.max_loan_duration_secs = max_loan_duration_secs
        self.db.flush()
        return policy

    def deactivate_policy(self, agent_id: UUID) -> int:
        """Deactivate treasury policy. Returns rows affected."""
        return self.db.query(TreasuryPolicy).filter(
            TreasuryPolicy.agent_id == agent_id,
            TreasuryPolicy.is_active.is_(True),
        ).update({"is_active": False})

    def get_active_policies(self) -> List[TreasuryPolicy]:
        """Get all active treasury policies (for background rebalance task)."""
        return self.db.query(TreasuryPolicy).filter(
            TreasuryPolicy.is_active.is_(True),
        ).all()

    def update_last_rebalance(self, agent_id: UUID) -> None:
        """Update last_rebalance_at timestamp."""
        self.db.query(TreasuryPolicy).filter(
            TreasuryPolicy.agent_id == agent_id,
        ).update({"last_rebalance_at": datetime.now(timezone.utc)})

    # ── Forecasts ─────────────────────────────────────────────────────────

    def add_forecast(
        self,
        agent_id: UUID,
        forecast_type: str,
        source_id: UUID,
        expected_amount: Decimal,
        expected_currency: str,
        direction: str,
        expected_at: datetime,
        confidence: Decimal = Decimal("1.00"),
    ) -> TreasuryForecast:
        """Add a cash flow forecast."""
        forecast = TreasuryForecast(
            agent_id=agent_id,
            forecast_type=forecast_type,
            source_id=source_id,
            expected_amount=expected_amount,
            expected_currency=expected_currency,
            direction=direction,
            expected_at=expected_at,
            confidence=confidence,
        )
        self.db.add(forecast)
        self.db.flush()
        return forecast

    def list_forecasts(
        self,
        agent_id: UUID,
        direction: Optional[str] = None,
        limit: int = 50,
    ) -> List[TreasuryForecast]:
        """List forecasts for an agent, ordered by expected_at."""
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(TreasuryForecast).filter(
            TreasuryForecast.agent_id == agent_id,
        )
        if direction:
            query = query.filter(TreasuryForecast.direction == direction)
        return query.order_by(TreasuryForecast.expected_at).limit(limit).all()

    def delete_forecasts_by_source(self, source_id: UUID) -> int:
        """Delete stale forecasts for a completed/cancelled source."""
        return self.db.query(TreasuryForecast).filter(
            TreasuryForecast.source_id == source_id,
        ).delete()

    # ── Rebalance Log ─────────────────────────────────────────────────────

    def add_rebalance_log(
        self,
        agent_id: UUID,
        trigger: str,
        conversions: List[Dict[str, Any]],
        pre_allocation: Dict[str, int],
        post_allocation: Dict[str, int],
        total_value_xusd: Decimal,
    ) -> TreasuryRebalanceLog:
        """Record a rebalance execution."""
        log = TreasuryRebalanceLog(
            agent_id=agent_id,
            trigger=trigger,
            conversions=conversions,
            pre_allocation=pre_allocation,
            post_allocation=post_allocation,
            total_value_xusd=total_value_xusd,
        )
        self.db.add(log)
        self.db.flush()
        return log

    def list_rebalance_history(
        self,
        agent_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> List[TreasuryRebalanceLog]:
        """List rebalance history for an agent, newest first."""
        limit = min(limit, _MAX_QUERY_LIMIT)
        return (
            self.db.query(TreasuryRebalanceLog)
            .filter(TreasuryRebalanceLog.agent_id == agent_id)
            .order_by(desc(TreasuryRebalanceLog.created_at))
            .offset(offset)
            .limit(limit)
            .all()
        )
