"""
LoanRepository -- data-access layer for AgentLoan and LendingOffer records.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import desc, or_

from .models import AgentLoan, LendingOffer, LoanStatus
from ._repo_base import _MAX_QUERY_LIMIT


class LoanRepository:
    """Loan and lending offer data access."""

    def __init__(self, db: Session):
        self.db = db

    # ── Loans ─────────────────────────────────────────────────────────────

    def create(
        self,
        loan_hash: str,
        lender_id: UUID,
        borrower_id: UUID,
        principal: Decimal,
        interest_rate_bps: int,
        duration_secs: int,
        repayment_amount: Decimal,
        currency: str = "XMR",
        collateral_amount: Decimal = Decimal("0"),
        collateral_currency: Optional[str] = None,
        grace_period_secs: int = 300,
    ) -> AgentLoan:
        """Create a new loan in REQUESTED state."""
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=duration_secs)

        loan = AgentLoan(
            loan_hash=loan_hash,
            lender_id=lender_id,
            borrower_id=borrower_id,
            principal=principal,
            currency=currency,
            interest_rate_bps=interest_rate_bps,
            duration_secs=duration_secs,
            collateral_amount=collateral_amount,
            collateral_currency=collateral_currency,
            repayment_amount=repayment_amount,
            state=LoanStatus.REQUESTED,
            expires_at=expires_at,
            grace_period_secs=grace_period_secs,
        )
        self.db.add(loan)
        self.db.flush()
        return loan

    def get_by_id(self, loan_id: UUID) -> Optional[AgentLoan]:
        """Get loan by ID."""
        return self.db.query(AgentLoan).filter(
            AgentLoan.id == loan_id,
        ).first()

    def get_by_id_for_update(self, loan_id: UUID) -> Optional[AgentLoan]:
        """Get loan by ID with row-level lock."""
        is_sqlite = self.db.bind and self.db.bind.dialect.name == "sqlite"
        query = self.db.query(AgentLoan).filter(AgentLoan.id == loan_id)
        if not is_sqlite:
            query = query.with_for_update()
        return query.first()

    def fund(self, loan_id: UUID) -> int:
        """Transition REQUESTED -> ACTIVE. Returns rows affected."""
        now = datetime.now(timezone.utc)
        return self.db.query(AgentLoan).filter(
            AgentLoan.id == loan_id,
            AgentLoan.state == LoanStatus.REQUESTED,
        ).update({
            "state": LoanStatus.ACTIVE,
            "funded_at": now,
        })

    def repay(self, loan_id: UUID, amount: Decimal) -> int:
        """Transition ACTIVE -> REPAID. Returns rows affected."""
        now = datetime.now(timezone.utc)
        return self.db.query(AgentLoan).filter(
            AgentLoan.id == loan_id,
            AgentLoan.state == LoanStatus.ACTIVE,
        ).update({
            "state": LoanStatus.REPAID,
            "repaid_amount": amount,
            "repaid_at": now,
        })

    def default(self, loan_id: UUID) -> int:
        """Transition ACTIVE -> DEFAULTED. Returns rows affected."""
        now = datetime.now(timezone.utc)
        return self.db.query(AgentLoan).filter(
            AgentLoan.id == loan_id,
            AgentLoan.state == LoanStatus.ACTIVE,
        ).update({
            "state": LoanStatus.DEFAULTED,
            "defaulted_at": now,
        })

    def liquidate(self, loan_id: UUID) -> int:
        """Transition DEFAULTED -> LIQUIDATED. Returns rows affected."""
        return self.db.query(AgentLoan).filter(
            AgentLoan.id == loan_id,
            AgentLoan.state == LoanStatus.DEFAULTED,
        ).update({
            "state": LoanStatus.LIQUIDATED,
        })

    def cancel(self, loan_id: UUID) -> int:
        """Transition REQUESTED -> CANCELLED. Returns rows affected."""
        return self.db.query(AgentLoan).filter(
            AgentLoan.id == loan_id,
            AgentLoan.state == LoanStatus.REQUESTED,
        ).update({
            "state": LoanStatus.CANCELLED,
        })

    def list_by_agent(
        self,
        agent_id: UUID,
        role: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[AgentLoan], int]:
        """List loans where agent participates. Returns (items, total)."""
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(AgentLoan)

        if role == "lender":
            query = query.filter(AgentLoan.lender_id == agent_id)
        elif role == "borrower":
            query = query.filter(AgentLoan.borrower_id == agent_id)
        else:
            query = query.filter(
                or_(
                    AgentLoan.lender_id == agent_id,
                    AgentLoan.borrower_id == agent_id,
                )
            )

        if state:
            query = query.filter(AgentLoan.state == state)

        total = query.count()
        items = (
            query.order_by(desc(AgentLoan.requested_at))
            .offset(offset)
            .limit(limit)
            .all()
        )
        return items, total

    def get_overdue_active_loans(self) -> List[AgentLoan]:
        """Get active loans past their expiry + grace period (for default detection)."""
        now = datetime.now(timezone.utc)
        return self.db.query(AgentLoan).filter(
            AgentLoan.state == LoanStatus.ACTIVE,
            AgentLoan.expires_at <= now,
        ).all()

    # ── Lending Offers ────────────────────────────────────────────────────

    def create_offer(
        self,
        lender_id: UUID,
        max_amount: Decimal,
        interest_rate_bps: int,
        max_duration_secs: int,
        expires_at: datetime,
        currency: str = "XMR",
        min_borrower_credit_score: int = 0,
        require_collateral: bool = False,
        collateral_ratio_pct: int = 100,
    ) -> LendingOffer:
        """Create a new lending offer."""
        offer = LendingOffer(
            lender_id=lender_id,
            max_amount=max_amount,
            currency=currency,
            interest_rate_bps=interest_rate_bps,
            max_duration_secs=max_duration_secs,
            min_borrower_credit_score=min_borrower_credit_score,
            require_collateral=require_collateral,
            collateral_ratio_pct=collateral_ratio_pct,
            remaining_amount=max_amount,
            expires_at=expires_at,
        )
        self.db.add(offer)
        self.db.flush()
        return offer

    def deactivate_offer(self, offer_id: UUID, lender_id: UUID) -> int:
        """Deactivate a lending offer. Returns rows affected."""
        return self.db.query(LendingOffer).filter(
            LendingOffer.id == offer_id,
            LendingOffer.lender_id == lender_id,
            LendingOffer.is_active.is_(True),
        ).update({"is_active": False})

    def list_active_offers(
        self,
        currency: str = "XMR",
        min_amount: Optional[Decimal] = None,
        max_duration_secs: Optional[int] = None,
        min_credit_score: Optional[int] = None,
        limit: int = 50,
    ) -> List[LendingOffer]:
        """List active lending offers, sorted by interest rate (cheapest first)."""
        limit = min(limit, _MAX_QUERY_LIMIT)
        now = datetime.now(timezone.utc)
        query = self.db.query(LendingOffer).filter(
            LendingOffer.is_active.is_(True),
            LendingOffer.currency == currency,
            LendingOffer.expires_at > now,
        )
        if min_amount is not None:
            query = query.filter(LendingOffer.remaining_amount >= min_amount)
        if max_duration_secs is not None:
            query = query.filter(LendingOffer.max_duration_secs >= max_duration_secs)
        return (
            query.order_by(LendingOffer.interest_rate_bps)
            .limit(limit)
            .all()
        )
