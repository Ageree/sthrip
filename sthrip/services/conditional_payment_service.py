"""
ConditionalPaymentService -- business logic for conditional payments.

Payments that execute only when specified conditions are met.
Condition types: time_lock, escrow_completed, balance_threshold, webhook.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.models import (
    ConditionalPayment,
    ConditionalPaymentState,
    EscrowDeal,
)
from sthrip.db.conditional_payment_repo import ConditionalPaymentRepository
from sthrip.db.repository import BalanceRepository
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.services.webhook_service import queue_webhook

logger = logging.getLogger("sthrip.conditional")

_VALID_CONDITION_TYPES = frozenset({
    "time_lock",
    "escrow_completed",
    "balance_threshold",
    "webhook",
})

_REQUIRED_CONFIG_FIELDS: Dict[str, List[str]] = {
    "time_lock": ["release_at"],
    "escrow_completed": ["escrow_id", "required_status"],
    "balance_threshold": ["agent_id", "threshold"],
    "webhook": ["callback_url"],
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _generate_payment_hash(
    from_id: UUID, to_id: UUID, amount: Decimal, timestamp: datetime,
) -> str:
    salt = secrets.token_hex(8)
    raw = f"{from_id}{to_id}{amount}{timestamp.isoformat()}{salt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _payment_to_dict(p: ConditionalPayment) -> dict:
    state_val = p.state.value if hasattr(p.state, "value") else p.state
    return {
        "id": str(p.id),
        "payment_hash": p.payment_hash,
        "from_agent_id": str(p.from_agent_id),
        "to_agent_id": str(p.to_agent_id),
        "amount": str(p.amount),
        "currency": p.currency,
        "memo": p.memo,
        "condition_type": p.condition_type,
        "condition_config": p.condition_config,
        "locked_amount": str(p.locked_amount),
        "state": state_val,
        "expires_at": _iso(p.expires_at),
        "created_at": _iso(p.created_at),
        "triggered_at": _iso(p.triggered_at),
        "executed_at": _iso(p.executed_at),
    }


def _validate_condition_config(condition_type: str, config: Dict[str, Any]) -> None:
    """Validate that condition_config has all required fields for the type."""
    required = _REQUIRED_CONFIG_FIELDS.get(condition_type, [])
    for field in required:
        if field not in config:
            raise ValueError(
                f"condition_config missing required field '{field}' "
                f"for condition_type '{condition_type}'"
            )


class ConditionalPaymentService:
    """Service for conditional payments that execute when conditions are met."""

    @staticmethod
    def create_conditional(
        db: Session,
        from_agent_id: UUID,
        to_agent_id: UUID,
        amount: Decimal,
        currency: str,
        condition_type: str,
        condition_config: Dict[str, Any],
        expires_hours: int = 24,
        memo: Optional[str] = None,
    ) -> dict:
        """Create a conditional payment. Locks funds from sender."""
        # Validate from != to
        if str(from_agent_id) == str(to_agent_id):
            raise ValueError("Sender and recipient must be different agents")

        # Validate condition_type
        if condition_type not in _VALID_CONDITION_TYPES:
            raise ValueError(
                f"Invalid condition_type '{condition_type}'. "
                f"Must be one of: {', '.join(sorted(_VALID_CONDITION_TYPES))}"
            )

        # Validate condition_config has required fields
        _validate_condition_config(condition_type, condition_config)

        # Lock funds from sender
        BalanceRepository(db).deduct(from_agent_id, amount, token=currency)

        now = _now()
        expires_at = now + timedelta(hours=max(expires_hours, 0))
        payment_hash = _generate_payment_hash(from_agent_id, to_agent_id, amount, now)

        repo = ConditionalPaymentRepository(db)
        payment = repo.create(
            payment_hash=payment_hash,
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            amount=amount,
            condition_type=condition_type,
            condition_config=condition_config,
            locked_amount=amount,
            expires_at=expires_at,
            currency=currency,
            memo=memo,
        )

        audit_log(
            action="conditional_payment.created",
            agent_id=from_agent_id,
            resource_type="conditional_payment",
            resource_id=payment.id,
            details={
                "to_agent_id": str(to_agent_id),
                "amount": str(amount),
                "condition_type": condition_type,
            },
            db=db,
        )

        return _payment_to_dict(payment)

    @staticmethod
    def evaluate_conditions(db: Session) -> int:
        """Background task: evaluate PENDING payments and execute those whose conditions are met.

        Returns the number of payments executed.
        """
        pending = db.query(ConditionalPayment).filter(
            ConditionalPayment.state == ConditionalPaymentState.PENDING,
        ).all()

        executed_count = 0
        now = _now()

        for payment in pending:
            condition_met = False
            ctype = payment.condition_type
            config = payment.condition_config or {}

            if ctype == "time_lock":
                release_at_str = config.get("release_at", "")
                try:
                    release_at = datetime.fromisoformat(release_at_str)
                    # Handle naive datetimes (SQLite test compat)
                    if release_at.tzinfo is None:
                        naive_now = now.replace(tzinfo=None)
                        condition_met = naive_now >= release_at
                    else:
                        condition_met = now >= release_at
                except (ValueError, TypeError):
                    logger.warning(
                        "Invalid release_at in conditional payment %s", payment.id
                    )

            elif ctype == "escrow_completed":
                escrow_id_str = config.get("escrow_id", "")
                required_status = config.get("required_status", "completed")
                try:
                    escrow_id = UUID(escrow_id_str)
                    deal = db.query(EscrowDeal).filter(
                        EscrowDeal.id == escrow_id,
                    ).first()
                    if deal:
                        deal_status = (
                            deal.status.value
                            if hasattr(deal.status, "value")
                            else deal.status
                        )
                        condition_met = deal_status == required_status
                except (ValueError, TypeError):
                    logger.warning(
                        "Invalid escrow_id in conditional payment %s", payment.id
                    )

            elif ctype == "balance_threshold":
                agent_id_str = config.get("agent_id", "")
                threshold_str = config.get("threshold", "0")
                try:
                    target_agent_id = UUID(agent_id_str)
                    threshold = Decimal(threshold_str)
                    balance = BalanceRepository(db).get_available(
                        target_agent_id, token=payment.currency
                    )
                    condition_met = balance < threshold
                except (ValueError, TypeError, ArithmeticError):
                    logger.warning(
                        "Invalid balance_threshold config in conditional payment %s",
                        payment.id,
                    )

            elif ctype == "webhook":
                # Webhooks are triggered externally, skip in evaluate loop
                continue

            if condition_met:
                # Transition PENDING -> TRIGGERED -> EXECUTED
                repo = ConditionalPaymentRepository(db)
                triggered = repo.trigger(payment.id)
                if triggered:
                    db.flush()
                    ConditionalPaymentService.execute_payment(db, payment.id)
                    executed_count += 1

        return executed_count

    @staticmethod
    def trigger_webhook(
        db: Session, payment_id: UUID, agent_id: UUID,
    ) -> dict:
        """Webhook trigger: verify agent is sender, transition PENDING->TRIGGERED->EXECUTED."""
        repo = ConditionalPaymentRepository(db)
        payment = repo.get_by_id_for_update(payment_id)
        if not payment:
            raise LookupError("Conditional payment not found")

        if str(payment.from_agent_id) != str(agent_id):
            raise PermissionError("Only the sender can trigger this payment")

        state_val = payment.state.value if hasattr(payment.state, "value") else payment.state
        if state_val != "pending":
            raise ValueError(f"Cannot trigger payment in state '{state_val}'")

        triggered = repo.trigger(payment_id)
        if not triggered:
            raise ValueError("Failed to trigger payment (state conflict)")
        db.flush()

        result = ConditionalPaymentService.execute_payment(db, payment_id)
        return result

    @staticmethod
    def execute_payment(db: Session, payment_id: UUID) -> dict:
        """Credit locked_amount to recipient. Transition TRIGGERED->EXECUTED."""
        repo = ConditionalPaymentRepository(db)
        payment = repo.get_by_id_for_update(payment_id)
        if not payment:
            raise LookupError("Conditional payment not found")

        # Credit recipient
        BalanceRepository(db).credit(
            payment.to_agent_id, payment.locked_amount, token=payment.currency
        )

        executed = repo.execute(payment_id)
        if not executed:
            raise ValueError("Failed to execute payment (state conflict)")
        db.flush()

        # Re-fetch for updated timestamps
        payment = repo.get_by_id(payment_id)

        audit_log(
            action="conditional_payment.executed",
            agent_id=payment.from_agent_id,
            resource_type="conditional_payment",
            resource_id=payment.id,
            details={
                "to_agent_id": str(payment.to_agent_id),
                "amount": str(payment.locked_amount),
            },
            db=db,
        )

        return _payment_to_dict(payment)

    @staticmethod
    def cancel(db: Session, agent_id: UUID, payment_id: UUID) -> dict:
        """Cancel a PENDING conditional payment and refund the sender."""
        repo = ConditionalPaymentRepository(db)
        payment = repo.get_by_id_for_update(payment_id)
        if not payment:
            raise LookupError("Conditional payment not found")

        if str(payment.from_agent_id) != str(agent_id):
            raise PermissionError("Only the sender can cancel this payment")

        state_val = payment.state.value if hasattr(payment.state, "value") else payment.state
        if state_val != "pending":
            raise ValueError(
                f"Cannot cancel payment in state '{state_val}'"
            )

        # Refund sender
        BalanceRepository(db).credit(
            payment.from_agent_id, payment.locked_amount, token=payment.currency
        )

        cancelled = repo.cancel(payment_id)
        if not cancelled:
            raise ValueError("Failed to cancel payment (state conflict)")
        db.flush()

        payment = repo.get_by_id(payment_id)

        audit_log(
            action="conditional_payment.cancelled",
            agent_id=agent_id,
            resource_type="conditional_payment",
            resource_id=payment.id,
            details={"refunded_amount": str(payment.locked_amount)},
            db=db,
        )

        return _payment_to_dict(payment)

    @staticmethod
    def expire_stale(db: Session) -> int:
        """Find PENDING payments past expires_at, refund, transition to EXPIRED."""
        repo = ConditionalPaymentRepository(db)
        expired_payments = repo.get_pending_expired()
        count = 0

        for payment in expired_payments:
            # Refund sender
            BalanceRepository(db).credit(
                payment.from_agent_id, payment.locked_amount, token=payment.currency
            )

            expired = repo.expire(payment.id)
            if expired:
                count += 1
                audit_log(
                    action="conditional_payment.expired",
                    agent_id=payment.from_agent_id,
                    resource_type="conditional_payment",
                    resource_id=payment.id,
                    details={"refunded_amount": str(payment.locked_amount)},
                    db=db,
                )

        db.flush()
        return count
