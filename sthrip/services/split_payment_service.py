"""
SplitPaymentService -- atomic multi-recipient payments.

All-or-nothing: if any recipient lookup fails or balance is insufficient,
the entire transaction is rolled back.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.models import Agent
from sthrip.db.repository import AgentRepository, BalanceRepository, TransactionRepository
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.services.webhook_service import queue_webhook

logger = logging.getLogger("sthrip.split")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _generate_tx_hash(
    from_id: UUID, to_id: UUID, amount: Decimal, timestamp: datetime,
) -> str:
    salt = secrets.token_hex(8)
    raw = f"split:{from_id}:{to_id}:{amount}:{timestamp.isoformat()}:{salt}"
    return hashlib.sha256(raw.encode()).hexdigest()


class SplitPaymentService:
    """Atomic multi-recipient payment service."""

    @staticmethod
    def pay_split(
        db: Session,
        from_agent_id: UUID,
        recipients: List[Dict],
        currency: str = "XMR",
        memo: Optional[str] = None,
    ) -> List[dict]:
        """Execute an atomic split payment to multiple recipients.

        Args:
            db: Database session.
            from_agent_id: Sender agent UUID.
            recipients: List of {"agent_name": str, "amount": Decimal}.
            currency: Currency token (default "XMR").
            memo: Optional memo for all transactions.

        Returns:
            List of receipt dicts, one per recipient.

        Raises:
            ValueError: If recipients is empty, insufficient balance, or self-payment.
            LookupError: If any recipient agent name is not found.
        """
        if not recipients:
            raise ValueError("Recipients list must not be empty")

        agent_repo = AgentRepository(db)
        bal_repo = BalanceRepository(db)
        tx_repo = TransactionRepository(db)

        # Phase 1: Resolve all agent names (fail fast if any not found)
        resolved: List[Dict] = []
        for entry in recipients:
            name = entry["agent_name"]
            amount = Decimal(str(entry["amount"]))
            agent = agent_repo.get_by_name(name)
            if not agent:
                raise LookupError(f"Recipient agent '{name}' not found")
            if str(agent.id) == str(from_agent_id):
                raise ValueError(f"Cannot send split payment to self (agent '{name}')")
            resolved.append({"agent": agent, "amount": amount})

        # Phase 2: Calculate total and validate balance
        total = sum(r["amount"] for r in resolved)
        bal_repo.deduct(from_agent_id, total, token=currency)

        # Phase 3: Credit each recipient and create transaction records
        now = _now()
        receipts: List[dict] = []

        for entry in resolved:
            recipient = entry["agent"]
            amount = entry["amount"]

            bal_repo.credit(recipient.id, amount, token=currency)

            tx_hash = _generate_tx_hash(from_agent_id, recipient.id, amount, now)
            tx = tx_repo.create(
                tx_hash=tx_hash,
                network="hub",
                from_agent_id=from_agent_id,
                to_agent_id=recipient.id,
                amount=amount,
                token=currency,
                payment_type="hub_routing",
                status="confirmed",
                memo=memo,
            )
            db.flush()

            receipts.append({
                "tx_hash": tx.tx_hash,
                "from_agent_id": str(from_agent_id),
                "to_agent_id": str(recipient.id),
                "to_agent_name": recipient.agent_name,
                "amount": str(amount),
                "currency": currency,
                "memo": memo,
            })

        audit_log(
            action="split_payment.executed",
            agent_id=from_agent_id,
            resource_type="split_payment",
            details={
                "total_amount": str(total),
                "recipient_count": len(resolved),
                "currency": currency,
            },
            db=db,
        )

        return receipts
