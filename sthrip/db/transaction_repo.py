"""
TransactionRepository — data-access layer for Transaction records.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import desc, func, and_

from . import models
from ._repo_base import _MAX_QUERY_LIMIT


class InsufficientBalanceError(ValueError):
    """Sender does not have enough balance to cover ``amount + fee``.

    Subclass of ``ValueError`` so existing ``except ValueError``-style call
    sites (e.g. the legacy hub-routing path in api/routers/payments.py)
    keep working unchanged.
    """


class TransactionRepository:
    """Transaction data access"""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        tx_hash: str,
        network: str,
        from_agent_id: Optional[UUID],
        to_agent_id: Optional[UUID],
        amount: Decimal,
        token: str = "XMR",
        payment_type: str = "p2p",
        status: str = "pending",
        fee: Decimal = Decimal('0'),
        fee_collected: Decimal = Decimal('0'),
        memo: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> models.Transaction:
        """Record new transaction"""
        tx = models.Transaction(
            tx_hash=tx_hash,
            network=network,
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            amount=amount,
            token=token,
            payment_type=payment_type,
            status=status,
            fee=fee,
            fee_collected=fee_collected,
            memo=memo,
            metadata=metadata or {}
        )
        # Sprint 3 dual-write: encrypted participant envelope alongside FKs.
        # Sprint 4 will cut reads over and drop the FKs. Lazy import keeps
        # the db package free of a service-layer cycle (services import db).
        from sthrip.services.payment_envelope_writer import apply_envelope
        apply_envelope(
            tx,
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            amount=amount,
            description=memo,
        )
        self.db.add(tx)
        return tx

    def create_with_commission(
        self,
        tx_hash: str,
        network: str,
        from_agent_id: UUID,
        to_agent_id: UUID,
        amount_piconero: int,
        token: str = "XMR",
        payment_type: str = "hub_routing",
        status: str = "confirmed",
        memo: Optional[str] = None,
        metadata: Optional[Dict] = None,
        idempotency_key: Optional[str] = None,
    ) -> models.Transaction:
        """Atomic transfer + commission deduction.

        Phase 2 Sprint 2 hot-path write:

        1. **Idempotency check first** — if ``idempotency_key`` matches an
           existing IdempotencyKey row, return the cached Transaction and
           DO NOT re-deduct the fee. Prevents double-charge on client retries.
        2. **Lock sender balance** with ``SELECT ... FOR UPDATE`` (or SQLite
           best-effort row read) so concurrent transfers serialize.
        3. **Compute commission** via ``fee_calculator.compute_fee`` using
           the cached or freshly-read ``Agent.tier``.
        4. **Verify** ``balance.available >= amount + fee``. If not, raise
           ``InsufficientBalanceError`` — no partial mutation.
        5. **Deduct ``amount + fee``** from sender, **credit ``amount``** to
           receiver (full amount, no skim).
        6. **Insert** Transaction row + FeeCollection row. All in the same
           DB transaction, so a failure rolls everything back.

        ``amount_piconero`` is the integer piconero amount (1 XMR = 10**12
        piconero). Stored in ``Transaction.amount`` as Decimal.

        NOTE: This method does NOT call ``commit()`` — the caller controls
        commit/rollback via the surrounding session's ``with`` block.
        """
        # Lazy imports to avoid import cycles (services -> db -> services).
        from sthrip.services.fee_calculator import compute_fee, rate_bps_for_tier
        from sthrip.services.tier_cache import get_tier
        from sthrip.db.balance_repo import BalanceRepository
        from sthrip.db.enums import FeeCollectionStatus, PaymentType

        if amount_piconero < 0:
            raise ValueError(
                f"amount_piconero must be non-negative, got {amount_piconero}"
            )

        # 1. Idempotency replay check — return cached Transaction without
        # re-deducting fee. Match on tx_hash since the hub-routing path
        # derives tx_hash deterministically from idempotency_key.
        if idempotency_key is not None:
            existing = self.db.query(models.Transaction).filter(
                models.Transaction.tx_hash == tx_hash
            ).first()
            if existing is not None:
                return existing

        # 2. Look up sender tier (request-cached; falls back to direct DB read).
        tier = get_tier(from_agent_id, self.db)

        # 3. Compute commission in piconero (integer math, deterministic).
        fee_piconero = compute_fee(amount_piconero, tier)

        # 4. BalanceRepository stores balances as XMR Decimal (Numeric(20, 12))
        # while the fee calculator works in integer piconero (avoids float
        # drift on dust). Convert at the boundary so the comparisons and the
        # stored Transaction.amount / Transaction.fee values align with the
        # existing XMR-scale balance ledger.
        PICO = Decimal(10) ** 12
        amount_xmr = Decimal(amount_piconero) / PICO
        fee_xmr = Decimal(fee_piconero) / PICO
        total_xmr = amount_xmr + fee_xmr

        balance_repo = BalanceRepository(self.db)
        # _get_for_update applies SELECT ... FOR UPDATE on Postgres; on SQLite
        # falls back to plain read (acceptable for tests; the test suite is
        # single-process).
        sender_balance = balance_repo._get_for_update(from_agent_id, token)
        available = sender_balance.available or Decimal("0")
        if available < total_xmr:
            raise InsufficientBalanceError(
                f"Insufficient balance: available={available} XMR, "
                f"required={total_xmr} XMR (amount={amount_xmr} + fee={fee_xmr})"
            )

        # 5. Mutate balances. Sender pays amount + fee; receiver gets
        # amount only — fee is the hub's revenue.
        sender_balance.available = available - total_xmr
        sender_balance.updated_at = datetime.now(timezone.utc)
        balance_repo.credit(to_agent_id, amount_xmr, token)

        # 6. Create Transaction + FeeCollection rows.
        tx = models.Transaction(
            tx_hash=tx_hash,
            network=network,
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            amount=amount_xmr,
            token=token,
            payment_type=payment_type,
            status=status,
            fee=fee_xmr,
            fee_collected=fee_xmr,
            memo=memo,
            metadata=metadata or {},
        )
        from sthrip.services.payment_envelope_writer import apply_envelope
        apply_envelope(
            tx,
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            amount=amount_xmr,
            description=memo,
        )
        self.db.add(tx)
        self.db.flush()

        now = datetime.now(timezone.utc)
        fee_row = models.FeeCollection(
            source_type=PaymentType.FEE_COLLECTION.value,
            source_id=tx.id,
            amount=fee_xmr,
            token=token,
            status=FeeCollectionStatus.PENDING,
            payer_agent_id=from_agent_id,
            amount_piconero=fee_piconero,
            rate_applied_bps=rate_bps_for_tier(tier),
            transaction_ref=tx_hash,
            collected_at=now,
        )
        self.db.add(fee_row)
        self.db.flush()

        return tx

    def get_by_hash(self, tx_hash: str) -> Optional[models.Transaction]:
        """Get transaction by hash"""
        tx = self.db.query(models.Transaction).filter(
            models.Transaction.tx_hash == tx_hash
        ).first()
        # Sprint 4a dual-read: when STHRIP_READ_FROM_ENVELOPE is on, swap
        # row attributes to the envelope-decrypted values. Off by default,
        # so legacy callers get unchanged behaviour.
        if tx is not None:
            from sthrip.services.payment_envelope_reader import apply_envelope_to_row
            apply_envelope_to_row(tx)
        return tx

    def _agent_direction_filter(self, query, agent_id: UUID, direction: Optional[str]):
        """Apply direction filter to a transaction query."""
        if direction == 'in':
            return query.filter(models.Transaction.to_agent_id == agent_id)
        elif direction == 'out':
            return query.filter(models.Transaction.from_agent_id == agent_id)
        return query.filter(
            (models.Transaction.from_agent_id == agent_id) |
            (models.Transaction.to_agent_id == agent_id)
        )

    def count_by_agent(self, agent_id: UUID, direction: Optional[str] = None) -> int:
        """Count transactions for agent."""
        query = self.db.query(func.count(models.Transaction.id))
        query = self._agent_direction_filter(query, agent_id, direction)
        return query.scalar() or 0

    def list_by_agent(
        self,
        agent_id: UUID,
        direction: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[models.Transaction]:
        """List transactions for agent"""
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(models.Transaction)

        if direction == 'in':
            query = query.filter(models.Transaction.to_agent_id == agent_id)
        elif direction == 'out':
            query = query.filter(models.Transaction.from_agent_id == agent_id)
        else:
            query = query.filter(
                (models.Transaction.from_agent_id == agent_id) |
                (models.Transaction.to_agent_id == agent_id)
            )

        rows = (
            query.order_by(desc(models.Transaction.created_at))
            .offset(offset)
            .limit(limit)
            .all()
        )
        # Sprint 4a dual-read: feature-flag-gated post-process.
        from sthrip.services.payment_envelope_reader import apply_envelope_to_row
        for row in rows:
            apply_envelope_to_row(row)
        return rows

    def confirm_transaction(
        self,
        tx_hash: str,
        block_number: int,
        confirmations: int = 1
    ):
        """Mark transaction as confirmed"""
        self.db.query(models.Transaction).filter(
            models.Transaction.tx_hash == tx_hash
        ).update({
            "status": models.TransactionStatus.CONFIRMED,
            "block_number": block_number,
            "confirmations": confirmations,
            "confirmed_at": datetime.now(timezone.utc)
        })

    def get_volume_by_agent(self, agent_id: UUID, days: int = 30) -> Decimal:
        """Get total volume for agent in last N days"""
        since = datetime.now(timezone.utc) - timedelta(days=days)

        result = self.db.query(func.sum(models.Transaction.amount)).filter(
            and_(
                (models.Transaction.from_agent_id == agent_id) |
                (models.Transaction.to_agent_id == agent_id),
                models.Transaction.status == models.TransactionStatus.CONFIRMED,
                models.Transaction.created_at >= since
            )
        ).scalar()

        return result or Decimal('0')
