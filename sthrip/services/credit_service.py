"""
CreditService -- business logic for credit scoring, lending offers, and loans.

Credit score formula (max 1000):
  - Reputation factor (0-300): min(300, trust_score * 3)
  - Loan history (0-300): starts at 0, +50 per repaid, -100 per default, capped [0, 300]
  - Account age + activity (0-200): min(200, days_since_created * 0.5 + total_transactions * 0.1)
  - Balance factor (0-200): min(200, portfolio_value_xmr * 20)

Max borrow amount: (score / 200) + collateral * 0.8

Loan lifecycle: REQUESTED -> ACTIVE -> REPAID (or DEFAULTED -> LIQUIDATED)
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.models import (
    Agent, AgentCreditScore, AgentLoan, AgentReputation,
    LendingOffer, LoanStatus,
)
from sthrip.db.repository import (
    AgentRepository, BalanceRepository, CreditRepository, LoanRepository,
    ReputationRepository,
)
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.services.webhook_service import queue_webhook

logger = logging.getLogger("sthrip.credit")

_STALENESS_SECONDS: int = 300  # 5 minutes
_PLATFORM_FEE_RATE: Decimal = Decimal("0.01")  # 1% on interest
_DEFAULT_OFFER_EXPIRY_DAYS: int = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _generate_loan_hash(
    borrower_id: UUID, lender_id: UUID, amount: Decimal, ts: datetime,
) -> str:
    salt = secrets.token_hex(8)
    raw = f"{borrower_id}{lender_id}{amount}{ts.isoformat()}{salt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _loan_to_dict(loan: AgentLoan) -> dict:
    state_val = loan.state.value if hasattr(loan.state, "value") else loan.state
    return {
        "loan_id": str(loan.id),
        "loan_hash": loan.loan_hash,
        "lender_id": str(loan.lender_id),
        "borrower_id": str(loan.borrower_id),
        "principal": str(loan.principal),
        "currency": loan.currency,
        "interest_rate_bps": loan.interest_rate_bps,
        "duration_secs": loan.duration_secs,
        "collateral_amount": str(loan.collateral_amount or Decimal("0")),
        "collateral_currency": loan.collateral_currency,
        "repayment_amount": str(loan.repayment_amount),
        "repaid_amount": str(loan.repaid_amount or Decimal("0")),
        "state": state_val,
        "expires_at": _iso(loan.expires_at),
        "platform_fee": str(loan.platform_fee or Decimal("0")),
        "requested_at": _iso(loan.requested_at),
        "funded_at": _iso(loan.funded_at),
        "repaid_at": _iso(loan.repaid_at),
        "defaulted_at": _iso(loan.defaulted_at),
    }


def _offer_to_dict(offer: LendingOffer) -> dict:
    return {
        "offer_id": str(offer.id),
        "lender_id": str(offer.lender_id),
        "max_amount": str(offer.max_amount),
        "currency": offer.currency,
        "interest_rate_bps": offer.interest_rate_bps,
        "max_duration_secs": offer.max_duration_secs,
        "min_borrower_credit_score": offer.min_borrower_credit_score,
        "require_collateral": offer.require_collateral,
        "collateral_ratio_pct": offer.collateral_ratio_pct,
        "remaining_amount": str(offer.remaining_amount),
        "is_active": offer.is_active,
        "created_at": _iso(offer.created_at),
        "expires_at": _iso(offer.expires_at),
    }


class CreditService:
    """Credit scoring, lending offers, and loan management."""

    # ── Credit Scoring ───────────────────────────────────────────────────

    def calculate_credit_score(self, db: Session, agent_id: UUID) -> int:
        """Calculate credit score using 4-factor formula. Returns int 0..1000."""
        agent = AgentRepository(db).get_by_id(agent_id)
        if agent is None:
            raise LookupError(f"Agent {agent_id} not found")

        # Factor 1: Reputation (0-300)
        rep = ReputationRepository(db).get_by_agent(agent_id)
        trust_score = rep.trust_score if rep else 0
        rep_factor = min(300, trust_score * 3)

        # Factor 2: Loan history (0-300)
        credit_repo = CreditRepository(db)
        credit_record = credit_repo.get_or_create(agent_id)
        repaid = credit_record.total_loans_repaid or 0
        defaulted = credit_record.total_loans_defaulted or 0
        loan_factor = max(0, min(300, repaid * 50 - defaulted * 100))

        # Factor 3: Account age + activity (0-200)
        created_at = agent.created_at
        if created_at is not None:
            now = _now()
            # Handle naive vs aware datetimes (SQLite returns naive)
            if created_at.tzinfo is not None:
                days_old = (now - created_at).days
            else:
                days_old = (now.replace(tzinfo=None) - created_at).days
        else:
            days_old = 0
        total_tx = rep.total_transactions if rep else 0
        activity_factor = min(200, int(days_old * 0.5 + total_tx * 0.1))

        # Factor 4: Balance (0-200)
        balance_xmr = BalanceRepository(db).get_available(agent_id, token="XMR")
        balance_factor = min(200, int(float(balance_xmr) * 20))

        total = min(1000, rep_factor + loan_factor + activity_factor + balance_factor)

        # Persist
        max_borrow = self.max_borrow_amount(total)
        max_concurrent = max(1, total // 200)
        credit_repo.update_score(agent_id, total, max_borrow, max_concurrent)

        audit_log(
            "credit.score_calculated",
            agent_id=str(agent_id),
            details={
                "score": total,
                "rep_factor": rep_factor,
                "loan_factor": loan_factor,
                "activity_factor": activity_factor,
                "balance_factor": balance_factor,
            },
        )

        return total

    def get_credit_score(self, db: Session, agent_id: UUID) -> dict:
        """Return cached score or recalculate if stale (>5 min)."""
        credit_repo = CreditRepository(db)
        record = credit_repo.get(agent_id)

        needs_calc = True
        if record is not None and record.calculated_at is not None:
            calc_at = record.calculated_at
            now = _now()
            # Handle naive vs aware datetimes
            if calc_at.tzinfo is not None:
                age_secs = (now - calc_at).total_seconds()
            else:
                age_secs = (now.replace(tzinfo=None) - calc_at).total_seconds()
            if age_secs < _STALENESS_SECONDS:
                needs_calc = False

        if needs_calc:
            self.calculate_credit_score(db, agent_id)
            record = credit_repo.get(agent_id)

        return {
            "agent_id": str(agent_id),
            "credit_score": record.credit_score if record else 0,
            "max_borrow_amount": str(record.max_borrow_amount) if record else "0",
            "max_concurrent_loans": record.max_concurrent_loans if record else 0,
            "total_loans_taken": record.total_loans_taken if record else 0,
            "total_loans_repaid": record.total_loans_repaid if record else 0,
            "total_loans_defaulted": record.total_loans_defaulted if record else 0,
            "calculated_at": _iso(record.calculated_at) if record else None,
        }

    @staticmethod
    def max_borrow_amount(
        credit_score: int,
        collateral_amount: Decimal = Decimal("0"),
    ) -> Decimal:
        """Calculate maximum borrow amount.

        Uncollateralized: score / 200
        Collateralized bonus: + collateral * 0.8
        """
        uncollateralized = Decimal(str(credit_score)) / Decimal("200")
        collateralized = collateral_amount * Decimal("0.8")
        return uncollateralized + collateralized

    # ── Lending Offers ───────────────────────────────────────────────────

    def create_offer(
        self,
        db: Session,
        lender_id: UUID,
        max_amount: Decimal,
        currency: str,
        interest_rate_bps: int,
        max_duration_secs: int,
        min_credit_score: int,
        require_collateral: bool,
        collateral_ratio: int,
    ) -> dict:
        """Create a lending offer. Validates lender has sufficient balance."""
        available = BalanceRepository(db).get_available(lender_id, token=currency)
        if available < max_amount:
            raise ValueError(
                f"Insufficient balance: available={available}, required={max_amount}"
            )

        expires_at = _now() + timedelta(days=_DEFAULT_OFFER_EXPIRY_DAYS)
        loan_repo = LoanRepository(db)
        offer = loan_repo.create_offer(
            lender_id=lender_id,
            max_amount=max_amount,
            interest_rate_bps=interest_rate_bps,
            max_duration_secs=max_duration_secs,
            expires_at=expires_at,
            currency=currency,
            min_borrower_credit_score=min_credit_score,
            require_collateral=require_collateral,
            collateral_ratio_pct=collateral_ratio,
        )

        audit_log(
            "lending.offer_created",
            agent_id=str(lender_id),
            details={"offer_id": str(offer.id), "max_amount": str(max_amount)},
        )

        return _offer_to_dict(offer)

    def list_offers(
        self,
        db: Session,
        currency: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[dict]:
        """List active lending offers."""
        loan_repo = LoanRepository(db)
        offers = loan_repo.list_active_offers(
            currency=currency or "XMR",
            limit=limit,
        )
        return [_offer_to_dict(o) for o in offers]

    def withdraw_offer(
        self, db: Session, lender_id: UUID, offer_id: UUID,
    ) -> dict:
        """Deactivate a lending offer. Only the owning lender can withdraw."""
        loan_repo = LoanRepository(db)
        rows = loan_repo.deactivate_offer(offer_id, lender_id)
        if rows == 0:
            raise LookupError(
                f"Offer {offer_id} not found or not owned by lender {lender_id}"
            )
        audit_log(
            "lending.offer_withdrawn",
            agent_id=str(lender_id),
            details={"offer_id": str(offer_id)},
        )
        return {"offer_id": str(offer_id), "status": "withdrawn"}

    # ── Loan Lifecycle ───────────────────────────────────────────────────

    def request_loan(
        self,
        db: Session,
        borrower_id: UUID,
        amount: Decimal,
        currency: str,
        duration_secs: int,
        collateral_amount: Decimal = Decimal("0"),
    ) -> dict:
        """Request a loan: check credit, find best offer, lock collateral."""
        # Calculate borrower's credit score
        score = self.calculate_credit_score(db, borrower_id)

        # Find best matching offer (lowest rate)
        loan_repo = LoanRepository(db)
        offers = loan_repo.list_active_offers(
            currency=currency,
            min_amount=amount,
            max_duration_secs=duration_secs,
        )

        # Filter by credit score requirement and collateral requirement
        matching = [
            o for o in offers
            if o.min_borrower_credit_score <= score
            and o.lender_id != borrower_id
            and (not o.require_collateral or collateral_amount > Decimal("0"))
        ]

        if not matching:
            raise ValueError(
                "No matching offer found for this loan request. "
                "Check credit score requirements and available offers."
            )

        best_offer = matching[0]  # Already sorted by interest_rate_bps asc

        # Lock collateral from borrower if required
        if collateral_amount > Decimal("0"):
            BalanceRepository(db).deduct(
                borrower_id, collateral_amount, token=currency
            )

        # Calculate repayment = principal * (1 + rate_bps / 10000)
        rate = Decimal(str(best_offer.interest_rate_bps)) / Decimal("10000")
        repayment_amount = amount * (Decimal("1") + rate)

        loan_hash = _generate_loan_hash(
            borrower_id, best_offer.lender_id, amount, _now()
        )

        loan = loan_repo.create(
            loan_hash=loan_hash,
            lender_id=best_offer.lender_id,
            borrower_id=borrower_id,
            principal=amount,
            interest_rate_bps=best_offer.interest_rate_bps,
            duration_secs=duration_secs,
            repayment_amount=repayment_amount,
            currency=currency,
            collateral_amount=collateral_amount,
            collateral_currency=currency if collateral_amount > Decimal("0") else None,
        )

        audit_log(
            "lending.loan_requested",
            agent_id=str(borrower_id),
            details={
                "loan_id": str(loan.id),
                "amount": str(amount),
                "offer_id": str(best_offer.id),
            },
        )

        return _loan_to_dict(loan)

    def fund_loan(self, db: Session, lender_id: UUID, loan_id: UUID) -> dict:
        """Lender funds a requested loan. Deducts from lender, credits borrower."""
        loan_repo = LoanRepository(db)
        loan = loan_repo.get_by_id_for_update(loan_id)
        if loan is None:
            raise LookupError(f"Loan {loan_id} not found")

        state_val = loan.state.value if hasattr(loan.state, "value") else loan.state
        if state_val != LoanStatus.REQUESTED.value:
            raise ValueError(f"Loan is not in REQUESTED state (current: {state_val})")

        if str(loan.lender_id) != str(lender_id):
            raise PermissionError("Only the matched lender can fund this loan")

        balance_repo = BalanceRepository(db)

        # Deduct principal from lender
        balance_repo.deduct(lender_id, loan.principal, token=loan.currency)

        # Credit principal to borrower
        balance_repo.credit(loan.borrower_id, loan.principal, token=loan.currency)

        # Transition to ACTIVE
        rows = loan_repo.fund(loan_id)
        if rows == 0:
            raise RuntimeError("Failed to transition loan to ACTIVE")

        # Record loan taken in credit score
        CreditRepository(db).record_loan_taken(loan.borrower_id, loan.principal)

        # Refresh loan
        loan = loan_repo.get_by_id(loan_id)

        audit_log(
            "lending.loan_funded",
            agent_id=str(lender_id),
            details={"loan_id": str(loan_id), "principal": str(loan.principal)},
        )

        queue_webhook(
            db, loan.borrower_id, "loan.funded",
            {"loan_id": str(loan_id), "principal": str(loan.principal)},
        )

        return _loan_to_dict(loan)

    def repay_loan(self, db: Session, borrower_id: UUID, loan_id: UUID) -> dict:
        """Borrower repays loan. Calculates interest, deducts, credits lender."""
        loan_repo = LoanRepository(db)
        loan = loan_repo.get_by_id_for_update(loan_id)
        if loan is None:
            raise LookupError(f"Loan {loan_id} not found")

        state_val = loan.state.value if hasattr(loan.state, "value") else loan.state
        if state_val != LoanStatus.ACTIVE.value:
            raise ValueError(f"Loan is not ACTIVE (current: {state_val})")

        if str(loan.borrower_id) != str(borrower_id):
            raise PermissionError("Only the borrower can repay this loan")

        repayment = loan.repayment_amount
        principal = loan.principal
        interest = repayment - principal

        # Platform fee: 1% of interest
        platform_fee = interest * _PLATFORM_FEE_RATE
        lender_payout = repayment - platform_fee

        balance_repo = BalanceRepository(db)

        # Deduct repayment from borrower
        balance_repo.deduct(borrower_id, repayment, token=loan.currency)

        # Credit lender (principal + interest - platform fee)
        balance_repo.credit(loan.lender_id, lender_payout, token=loan.currency)

        # Release collateral back to borrower
        collateral = loan.collateral_amount or Decimal("0")
        if collateral > Decimal("0"):
            collateral_token = loan.collateral_currency or loan.currency
            balance_repo.credit(borrower_id, collateral, token=collateral_token)

        # Record platform fee
        loan_obj = loan_repo.get_by_id(loan_id)
        loan_obj.platform_fee = platform_fee
        db.flush()

        # Transition to REPAID
        rows = loan_repo.repay(loan_id, repayment)
        if rows == 0:
            raise RuntimeError("Failed to transition loan to REPAID")

        # Update credit record
        credit_repo = CreditRepository(db)
        funded_at = loan.funded_at
        if funded_at is not None:
            now = _now()
            if funded_at.tzinfo is not None:
                repay_time = int((now - funded_at).total_seconds())
            else:
                repay_time = int((now.replace(tzinfo=None) - funded_at).total_seconds())
        else:
            repay_time = 0
        credit_repo.record_loan_repaid(borrower_id, repay_time)

        # Recalculate credit score
        self.calculate_credit_score(db, borrower_id)

        loan = loan_repo.get_by_id(loan_id)

        audit_log(
            "lending.loan_repaid",
            agent_id=str(borrower_id),
            details={
                "loan_id": str(loan_id),
                "repayment": str(repayment),
                "platform_fee": str(platform_fee),
            },
        )

        queue_webhook(
            db, loan.lender_id, "loan.repaid",
            {"loan_id": str(loan_id), "repayment": str(repayment)},
        )

        return _loan_to_dict(loan)

    def detect_defaults(self, db: Session) -> List[dict]:
        """Find active loans past due, transition to DEFAULTED, liquidate collateral."""
        loan_repo = LoanRepository(db)
        overdue = loan_repo.get_overdue_active_loans()
        results: List[dict] = []

        for loan in overdue:
            # Transition to DEFAULTED
            rows = loan_repo.default(loan.id)
            if rows == 0:
                continue

            # Liquidate collateral: transfer to lender
            collateral = loan.collateral_amount or Decimal("0")
            if collateral > Decimal("0"):
                collateral_token = loan.collateral_currency or loan.currency
                balance_repo = BalanceRepository(db)
                balance_repo.credit(
                    loan.lender_id, collateral, token=collateral_token
                )

            # Record default in credit score
            funded_at = loan.funded_at
            now = _now()
            if funded_at is not None:
                if funded_at.tzinfo is not None:
                    default_dur = int((now - funded_at).total_seconds())
                else:
                    default_dur = int((now.replace(tzinfo=None) - funded_at).total_seconds())
            else:
                default_dur = 0
            CreditRepository(db).record_loan_defaulted(loan.borrower_id, default_dur)

            refreshed = loan_repo.get_by_id(loan.id)

            audit_log(
                "lending.loan_defaulted",
                agent_id=str(loan.borrower_id),
                details={
                    "loan_id": str(loan.id),
                    "collateral_liquidated": str(collateral),
                },
            )

            results.append(_loan_to_dict(refreshed))

        return results

    # ── Query helpers ────────────────────────────────────────────────────

    def list_loans(
        self,
        db: Session,
        agent_id: UUID,
        role: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """List loans for an agent."""
        loan_repo = LoanRepository(db)
        items, total = loan_repo.list_by_agent(
            agent_id, role=role, state=state, limit=limit, offset=offset,
        )
        return {
            "items": [_loan_to_dict(i) for i in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_loan(
        self, db: Session, loan_id: UUID, agent_id: UUID,
    ) -> dict:
        """Get a single loan (must be participant)."""
        loan_repo = LoanRepository(db)
        loan = loan_repo.get_by_id(loan_id)
        if loan is None:
            raise LookupError(f"Loan {loan_id} not found")
        if str(loan.lender_id) != str(agent_id) and str(loan.borrower_id) != str(agent_id):
            raise PermissionError("Not a participant in this loan")
        return _loan_to_dict(loan)
