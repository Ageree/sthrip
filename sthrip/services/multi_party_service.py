"""
Multi-party payment service -- atomic group payments.

Supports two modes:
  - require_all_accept=True: all-or-nothing (all must accept, or whole payment rejects)
  - require_all_accept=False: individual distribution (each accept/reject is independent)
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.models import (
    Agent, MultiPartyPayment, MultiPartyPaymentState,
)
from sthrip.db.repository import (
    AgentRepository, BalanceRepository, MultiPartyRepository,
)
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.services.webhook_service import queue_webhook

logger = logging.getLogger("sthrip.multi_party")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _generate_payment_hash(sender_id: UUID, total: Decimal, ts: datetime) -> str:
    """Generate unique payment hash with random salt."""
    salt = secrets.token_hex(8)
    raw = f"{sender_id}{total}{ts.isoformat()}{salt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _state_value(state) -> str:
    """Extract string value from enum or string."""
    return state.value if hasattr(state, "value") else state


def _recipient_to_dict(r) -> dict:
    """Convert a MultiPartyRecipient ORM object to an immutable dict."""
    accepted_val = None
    if r.accepted is True:
        accepted_val = "accepted"
    elif r.accepted is False:
        accepted_val = "rejected"
    else:
        accepted_val = "pending"
    return {
        "recipient_id": str(r.recipient_id),
        "amount": str(r.amount),
        "status": accepted_val,
        "accepted_at": _iso(r.accepted_at),
    }


def _payment_to_dict(payment: MultiPartyPayment, recipients: list) -> dict:
    """Convert a MultiPartyPayment + recipients to an immutable dict."""
    return {
        "payment_id": str(payment.id),
        "payment_hash": payment.payment_hash,
        "sender_id": str(payment.sender_id),
        "total_amount": str(payment.total_amount),
        "currency": payment.currency,
        "require_all_accept": payment.require_all_accept,
        "state": _state_value(payment.state),
        "accept_deadline": _iso(payment.accept_deadline),
        "created_at": _iso(payment.created_at),
        "completed_at": _iso(payment.completed_at),
        "recipients": [_recipient_to_dict(r) for r in recipients],
    }


def _find_recipient(recipients: list, agent_id: UUID):
    """Find a recipient row matching the given agent_id. Returns None if not found."""
    target = UUID(str(agent_id))
    for r in recipients:
        if UUID(str(r.recipient_id)) == target:
            return r
    return None


class MultiPartyService:
    """Atomic multi-party payment service."""

    def create_multi_party(
        self,
        db: Session,
        sender_id: UUID,
        recipients: List[dict],
        currency: str = "XMR",
        require_all_accept: bool = True,
        accept_hours: int = 2,
    ) -> dict:
        """Create a multi-party payment.

        Args:
            recipients: list of {"agent_name": str, "amount": Decimal}
        """
        # Validate at least 1 recipient
        if not recipients:
            raise ValueError("At least 1 recipient is required")

        # Validate no duplicate recipients
        names = [r["agent_name"] for r in recipients]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate recipients are not allowed")

        # Validate all amounts are positive
        for r in recipients:
            if Decimal(str(r["amount"])) <= Decimal("0"):
                raise ValueError("All recipient amounts must be positive")

        # Resolve all agent names to IDs
        agent_repo = AgentRepository(db)
        resolved = []
        for r in recipients:
            agent = agent_repo.get_by_name(r["agent_name"])
            if not agent:
                raise LookupError(f"Recipient agent '{r['agent_name']}' not found")
            if not agent.is_active:
                raise ValueError(f"Recipient agent '{r['agent_name']}' is not active")
            resolved.append({
                "recipient_id": agent.id,
                "amount": Decimal(str(r["amount"])),
                "agent_name": r["agent_name"],
            })

        # Validate sender not in recipients
        sender_uuid = UUID(str(sender_id))
        for r in resolved:
            if UUID(str(r["recipient_id"])) == sender_uuid:
                raise ValueError("Sender cannot be a recipient")

        # Calculate total
        total_amount = sum(r["amount"] for r in resolved)

        # Deduct total from sender
        BalanceRepository(db).deduct(sender_uuid, total_amount, token=currency)

        # Create payment
        now = _now()
        accept_deadline = now + timedelta(hours=accept_hours)

        repo = MultiPartyRepository(db)
        payment = repo.create(
            payment_hash=_generate_payment_hash(sender_uuid, total_amount, now),
            sender_id=sender_uuid,
            total_amount=total_amount,
            recipients=[
                {"recipient_id": r["recipient_id"], "amount": r["amount"]}
                for r in resolved
            ],
            accept_deadline=accept_deadline,
            currency=currency,
            require_all_accept=require_all_accept,
        )

        audit_log(
            action="multi_party.created",
            agent_id=sender_uuid,
            resource_type="multi_party_payment",
            resource_id=payment.id,
            details={
                "total_amount": str(total_amount),
                "recipient_count": len(resolved),
                "require_all_accept": require_all_accept,
            },
            db=db,
        )

        # Queue webhooks to each recipient
        for r in resolved:
            queue_webhook(
                str(r["recipient_id"]),
                "multi_party.created",
                {
                    "payment_id": str(payment.id),
                    "sender_id": str(sender_uuid),
                    "amount": str(r["amount"]),
                    "total_amount": str(total_amount),
                    "accept_deadline": _iso(accept_deadline),
                },
            )

        recipient_rows = repo.get_recipients(payment.id)
        return _payment_to_dict(payment, recipient_rows)

    def accept(
        self,
        db: Session,
        recipient_agent_id: UUID,
        payment_id: UUID,
    ) -> dict:
        """Recipient accepts a multi-party payment."""
        repo = MultiPartyRepository(db)
        payment = repo.get_by_id_for_update(payment_id)
        if not payment:
            raise LookupError("Multi-party payment not found")

        state_val = _state_value(payment.state)
        if state_val != "pending":
            raise ValueError("Payment is not in PENDING state")

        # Verify recipient is a participant
        recipient_rows = repo.get_recipients(payment_id)
        recipient_row = _find_recipient(recipient_rows, recipient_agent_id)
        if recipient_row is None:
            raise PermissionError("Agent is not a recipient of this payment")

        # Check if already accepted
        if recipient_row.accepted is True:
            return {
                "payment_id": str(payment_id),
                "state": state_val,
                "recipient_state": "already_accepted",
            }

        # Mark as accepted
        rows_affected = repo.accept_recipient(payment_id, UUID(str(recipient_agent_id)))
        if rows_affected == 0:
            # Already responded (race condition)
            return {
                "payment_id": str(payment_id),
                "state": state_val,
                "recipient_state": "already_accepted",
            }

        balance_repo = BalanceRepository(db)

        if payment.require_all_accept:
            # Flush the accept before checking all recipients
            db.flush()
            db.expire_all()
            updated_recipients = repo.get_recipients(payment_id)
            all_accepted = all(r.accepted is True for r in updated_recipients)

            if all_accepted:
                # Credit each recipient
                for r in updated_recipients:
                    balance_repo.credit(
                        UUID(str(r.recipient_id)),
                        r.amount,
                        token=payment.currency,
                    )
                repo.complete(payment_id)

                audit_log(
                    action="multi_party.completed",
                    agent_id=recipient_agent_id,
                    resource_type="multi_party_payment",
                    resource_id=payment_id,
                    details={"trigger": "all_accepted"},
                    db=db,
                )

                return {
                    "payment_id": str(payment_id),
                    "state": "completed",
                    "recipient_state": "accepted",
                }

            return {
                "payment_id": str(payment_id),
                "state": "pending",
                "recipient_state": "accepted",
            }
        else:
            # require_all_accept=False: credit this recipient immediately
            balance_repo.credit(
                UUID(str(recipient_agent_id)),
                recipient_row.amount,
                token=payment.currency,
            )

            audit_log(
                action="multi_party.recipient_accepted",
                agent_id=recipient_agent_id,
                resource_type="multi_party_payment",
                resource_id=payment_id,
                details={"amount": str(recipient_row.amount)},
                db=db,
            )

            # Flush balance changes before checking recipient states
            db.flush()

            # Check if all recipients have now responded
            db.expire_all()
            updated_recipients = repo.get_recipients(payment_id)
            all_responded = all(r.accepted is not None for r in updated_recipients)
            if all_responded:
                repo.complete(payment_id)

            return {
                "payment_id": str(payment_id),
                "state": "completed" if all_responded else "pending",
                "recipient_state": "accepted",
            }

    def reject(
        self,
        db: Session,
        recipient_agent_id: UUID,
        payment_id: UUID,
    ) -> dict:
        """Recipient rejects a multi-party payment."""
        repo = MultiPartyRepository(db)
        payment = repo.get_by_id_for_update(payment_id)
        if not payment:
            raise LookupError("Multi-party payment not found")

        state_val = _state_value(payment.state)
        if state_val != "pending":
            raise ValueError("Payment is not in PENDING state")

        # Verify recipient is a participant
        recipient_rows = repo.get_recipients(payment_id)
        recipient_row = _find_recipient(recipient_rows, recipient_agent_id)
        if recipient_row is None:
            raise PermissionError("Agent is not a recipient of this payment")

        # Mark as rejected
        rows_affected = repo.reject_recipient(payment_id, UUID(str(recipient_agent_id)))
        if rows_affected == 0:
            raise ValueError("Recipient has already responded to this payment")

        balance_repo = BalanceRepository(db)

        if payment.require_all_accept:
            # Refund full amount to sender
            balance_repo.credit(
                UUID(str(payment.sender_id)),
                payment.total_amount,
                token=payment.currency,
            )
            repo.reject(payment_id)

            audit_log(
                action="multi_party.rejected",
                agent_id=recipient_agent_id,
                resource_type="multi_party_payment",
                resource_id=payment_id,
                details={"trigger": "recipient_rejected", "require_all_accept": True},
                db=db,
            )

            return {
                "payment_id": str(payment_id),
                "state": "rejected",
                "recipient_state": "rejected",
            }
        else:
            # Refund only this recipient's portion to sender
            balance_repo.credit(
                UUID(str(payment.sender_id)),
                recipient_row.amount,
                token=payment.currency,
            )

            audit_log(
                action="multi_party.recipient_rejected",
                agent_id=recipient_agent_id,
                resource_type="multi_party_payment",
                resource_id=payment_id,
                details={"refunded_amount": str(recipient_row.amount)},
                db=db,
            )

            # Flush balance changes before checking recipient states
            db.flush()

            # Check if all recipients have now responded
            db.expire_all()
            updated_recipients = repo.get_recipients(payment_id)
            all_responded = all(r.accepted is not None for r in updated_recipients)
            if all_responded:
                repo.complete(payment_id)

            return {
                "payment_id": str(payment_id),
                "state": "completed" if all_responded else "pending",
                "recipient_state": "rejected",
            }

    def get_status(
        self,
        db: Session,
        payment_id: UUID,
        agent_id: UUID,
    ) -> dict:
        """Get payment status. Agent must be sender or recipient."""
        repo = MultiPartyRepository(db)
        payment = repo.get_by_id(payment_id)
        if not payment:
            raise LookupError("Multi-party payment not found")

        recipient_rows = repo.get_recipients(payment_id)

        # Verify agent is sender or recipient
        is_sender = UUID(str(payment.sender_id)) == UUID(str(agent_id))
        is_recipient = any(
            UUID(str(r.recipient_id)) == UUID(str(agent_id))
            for r in recipient_rows
        )
        if not is_sender and not is_recipient:
            raise PermissionError("Agent is not authorized to view this payment")

        return _payment_to_dict(payment, recipient_rows)

    def expire_stale(self, db: Session) -> int:
        """Find PENDING payments past accept_deadline and refund to sender."""
        repo = MultiPartyRepository(db)
        expired_payments = repo.get_pending_expired()
        count = 0

        for payment in expired_payments:
            balance_repo = BalanceRepository(db)

            # Refund unaccepted portions
            if payment.require_all_accept:
                # Refund full amount (none distributed yet in all-or-nothing mode)
                balance_repo.credit(
                    UUID(str(payment.sender_id)),
                    payment.total_amount,
                    token=payment.currency,
                )
            else:
                # Refund only undistributed portions
                recipients = repo.get_recipients(payment.id)
                for r in recipients:
                    if r.accepted is not True:
                        balance_repo.credit(
                            UUID(str(payment.sender_id)),
                            r.amount,
                            token=payment.currency,
                        )

            repo.expire(payment.id)

            audit_log(
                action="multi_party.expired",
                agent_id=payment.sender_id,
                resource_type="multi_party_payment",
                resource_id=payment.id,
                details={"total_amount": str(payment.total_amount)},
                db=db,
            )

            count += 1

        return count

    def list_by_agent(
        self,
        db: Session,
        agent_id: UUID,
        role: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """List multi-party payments for an agent."""
        repo = MultiPartyRepository(db)
        items, total = repo.list_by_agent(
            agent_id=agent_id,
            role=role,
            limit=limit,
            offset=offset,
        )

        result_items = []
        for payment in items:
            recipients = repo.get_recipients(payment.id)
            result_items.append(_payment_to_dict(payment, recipients))

        return {
            "items": result_items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
