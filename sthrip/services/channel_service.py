"""
Off-chain payment channel service.

Flow: OPEN -> CLOSING -> SETTLED -> CLOSED
Fee: 1% on NET transfer (abs(balance_a - deposit_a)).
"""

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.models import (
    ChannelStatus, FeeCollection, FeeCollectionStatus, PaymentChannel,
)
from sthrip.db.channel_repo import ChannelRepository
from sthrip.db.repository import BalanceRepository
from sthrip.services.audit_logger import log_event as audit_log
from sthrip.services.webhook_service import queue_webhook

logger = logging.getLogger("sthrip.channel")

_FEE_PERCENT = Decimal("0.01")  # 1% on net transfer


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _channel_to_dict(ch: PaymentChannel) -> dict:
    status_val = ch.status.value if hasattr(ch.status, "value") else ch.status
    return {
        "channel_id": str(ch.id),
        "channel_hash": ch.channel_hash,
        "agent_a_id": str(ch.agent_a_id),
        "agent_b_id": str(ch.agent_b_id),
        "capacity": str(ch.capacity),
        "deposit_a": str(ch.deposit_a),
        "deposit_b": str(ch.deposit_b),
        "balance_a": str(ch.balance_a),
        "balance_b": str(ch.balance_b),
        "nonce": ch.nonce,
        "status": status_val,
        "settlement_period": ch.settlement_period,
        "closes_at": _iso(ch.closes_at),
        "settled_at": _iso(ch.settled_at),
        "closed_at": _iso(ch.closed_at),
        "created_at": _iso(ch.created_at),
    }


def _generate_channel_hash(agent_a_id: UUID, agent_b_id: UUID) -> str:
    salt = secrets.token_hex(8)
    raw = f"{agent_a_id}{agent_b_id}{_now().isoformat()}{salt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _record_fee(db: Session, channel_id: UUID, token: str, fee_amount: Decimal) -> None:
    if fee_amount <= Decimal("0"):
        return
    db.add(FeeCollection(
        source_type="channel",
        source_id=channel_id,
        amount=fee_amount,
        token=token,
        status=FeeCollectionStatus.PENDING,
    ))


class ChannelService:
    """Hub-held payment channel lifecycle service."""

    def open_channel(
        self,
        db: Session,
        agent_a_id: UUID,
        agent_b_id: UUID,
        deposit_a: Decimal,
        deposit_b: Decimal = Decimal("0"),
        settlement_period: int = 3600,
    ) -> dict:
        """Open a new payment channel between two agents.

        Validates deposits, deducts balances, creates the channel record.
        Raises ValueError for bad input, PermissionError for access violations.
        """
        if agent_a_id == agent_b_id:
            raise ValueError("Cannot open a channel with yourself — agents must be different")
        if deposit_a <= Decimal("0") and deposit_b <= Decimal("0"):
            raise ValueError("At least one deposit must be positive")

        bal_repo = BalanceRepository(db)
        repo = ChannelRepository(db)

        # Deduct deposits from available balances
        if deposit_a > Decimal("0"):
            bal_repo.deduct(agent_a_id, deposit_a, token="XMR")
        if deposit_b > Decimal("0"):
            bal_repo.deduct(agent_b_id, deposit_b, token="XMR")

        ch_hash = _generate_channel_hash(agent_a_id, agent_b_id)
        channel = repo.open_with_deposit(
            channel_hash=ch_hash,
            agent_a_id=agent_a_id,
            agent_b_id=agent_b_id,
            deposit_a=deposit_a,
            deposit_b=deposit_b,
            settlement_period=settlement_period,
        )
        db.flush()

        result = _channel_to_dict(channel)

        audit_log(
            db=db, action="channel.opened",
            resource_type="channel", resource_id=channel.id,
            agent_id=agent_a_id,
            new_values={"deposit_a": str(deposit_a), "deposit_b": str(deposit_b)},
        )
        queue_webhook(str(agent_b_id), "channel.opened", {
            "channel_id": str(channel.id),
            "deposit_a": str(deposit_a),
            "deposit_b": str(deposit_b),
        })

        return result

    def submit_update(
        self,
        db: Session,
        channel_id: UUID,
        agent_id: UUID,
        nonce: int,
        balance_a: Decimal,
        balance_b: Decimal,
        signature_a: str,
        signature_b: str,
    ) -> dict:
        """Record an off-chain state update for a channel.

        Verifies that the caller is a participant, the nonce advances
        monotonically, and the sum of balances equals total capacity.
        """
        channel_id = _to_uuid(channel_id)
        repo = ChannelRepository(db)
        channel = repo.get_by_id(channel_id)
        if channel is None:
            raise LookupError(f"Channel {channel_id} not found")

        _verify_participant(channel, agent_id)

        # Nonce must advance
        current_nonce = channel.nonce or 0
        latest = repo.get_latest_update(channel_id)
        if latest is not None:
            current_nonce = max(current_nonce, latest.nonce)
        if nonce <= current_nonce:
            raise ValueError(
                f"Nonce {nonce} must be greater than current nonce {current_nonce}"
            )

        # Balance conservation
        total = (channel.deposit_a or Decimal("0")) + (channel.deposit_b or Decimal("0"))
        if balance_a + balance_b != total:
            raise ValueError(
                f"Balance conservation violated: {balance_a}+{balance_b}={balance_a+balance_b} "
                f"!= total capacity {total}"
            )

        update = repo.submit_update(
            channel_id=channel_id,
            nonce=nonce,
            balance_a=balance_a,
            balance_b=balance_b,
            signature_a=signature_a,
            signature_b=signature_b,
        )
        db.flush()

        return {
            "channel_id": str(channel_id),
            "nonce": update.nonce,
            "balance_a": str(update.balance_a),
            "balance_b": str(update.balance_b),
        }

    def settle(
        self,
        db: Session,
        channel_id: UUID,
        agent_id: UUID,
        nonce: int,
        balance_a: Decimal,
        balance_b: Decimal,
        sig_a: str,
        sig_b: str,
    ) -> dict:
        """Initiate settlement — OPEN -> CLOSING.

        Validates signatures are present, calculates fee, stores settlement state.
        Fee = 1% of abs(balance_a - deposit_a) (the net transfer amount).
        """
        if not sig_a or not sig_b:
            raise ValueError("Both signatures (sig_a and sig_b) are required to initiate settlement")

        channel_id = _to_uuid(channel_id)
        repo = ChannelRepository(db)
        channel = repo.get_by_id(channel_id)
        if channel is None:
            raise LookupError(f"Channel {channel_id} not found")

        _verify_participant(channel, agent_id)

        if channel.status != ChannelStatus.OPEN:
            raise ValueError(f"Channel must be OPEN to settle; current status: {channel.status}")

        deposit_a = channel.deposit_a or Decimal("0")
        net_transfer = abs(balance_a - deposit_a)
        fee = (net_transfer * _FEE_PERCENT).quantize(Decimal("0.00000001"))

        rows = repo.initiate_settlement(
            channel_id=channel_id,
            nonce=nonce,
            balance_a=balance_a,
            balance_b=balance_b,
            sig_a=sig_a,
            sig_b=sig_b,
        )
        if rows == 0:
            raise ValueError("Settlement initiation failed — channel may have changed state")

        _record_fee(db, channel_id, "XMR", fee)
        db.flush()

        # Re-fetch for accurate dict
        channel = repo.get_by_id(channel_id)
        result = _channel_to_dict(channel)
        result["fee"] = str(fee)

        audit_log(
            db=db, action="channel.settling",
            resource_type="channel", resource_id=channel_id,
            agent_id=agent_id,
            new_values={"nonce": nonce, "balance_a": str(balance_a), "balance_b": str(balance_b)},
        )

        return result

    def close(
        self,
        db: Session,
        channel_id: UUID,
        agent_id: UUID,
    ) -> dict:
        """Finalize channel close — SETTLED -> CLOSED (or CLOSING past period -> CLOSED).

        Credits balances back to participants.
        """
        channel_id = _to_uuid(channel_id)
        repo = ChannelRepository(db)
        channel = repo.get_by_id(channel_id)
        if channel is None:
            raise LookupError(f"Channel {channel_id} not found")

        _verify_participant(channel, agent_id)

        if channel.status == ChannelStatus.CLOSING:
            # Check if settlement period has elapsed
            # closes_at may be naive (SQLite) — compare as naive UTC
            now_utc = _now()
            closes_at = channel.closes_at
            if closes_at is not None:
                if closes_at.tzinfo is None:
                    import pytz as _pytz  # noqa: F401
                    from datetime import datetime as _dt
                    closes_at_cmp = closes_at
                    now_cmp = now_utc.replace(tzinfo=None)
                else:
                    closes_at_cmp = closes_at
                    now_cmp = now_utc
                if closes_at_cmp > now_cmp:
                    raise ValueError("Settlement period has not elapsed yet")
            # Auto-settle first
            repo.settle(channel_id)
            db.flush()
            channel = repo.get_by_id(channel_id)

        if channel.status != ChannelStatus.SETTLED:
            raise ValueError(
                f"Channel must be SETTLED to close; current status: {channel.status}"
            )

        bal_repo = BalanceRepository(db)
        balance_a = channel.balance_a or Decimal("0")
        balance_b = channel.balance_b or Decimal("0")

        if balance_a > Decimal("0"):
            bal_repo.credit(channel.agent_a_id, balance_a, token="XMR")
        if balance_b > Decimal("0"):
            bal_repo.credit(channel.agent_b_id, balance_b, token="XMR")

        rows = repo.finalize_close(channel_id)
        if rows == 0:
            raise ValueError("Close failed — channel may have changed state")
        db.flush()

        channel = repo.get_by_id(channel_id)
        result = _channel_to_dict(channel)

        audit_log(
            db=db, action="channel.closed",
            resource_type="channel", resource_id=channel_id,
            agent_id=agent_id,
        )
        queue_webhook(str(channel.agent_b_id if agent_id == channel.agent_a_id else channel.agent_a_id),
                      "channel.closed", {"channel_id": str(channel_id)})

        return result

    def dispute(
        self,
        db: Session,
        channel_id: UUID,
        agent_id: UUID,
        nonce: int,
        balance_a: Decimal,
        balance_b: Decimal,
        sig_a: str,
        sig_b: str,
    ) -> dict:
        """Submit a higher-nonce state during CLOSING window.

        Replaces the current closing state if nonce is strictly greater.
        """
        channel_id = _to_uuid(channel_id)
        repo = ChannelRepository(db)
        channel = repo.get_by_id(channel_id)
        if channel is None:
            raise LookupError(f"Channel {channel_id} not found")

        _verify_participant(channel, agent_id)

        if channel.status != ChannelStatus.CLOSING:
            raise ValueError(f"Dispute only valid during CLOSING; current status: {channel.status}")

        current_nonce = channel.nonce or 0
        if nonce <= current_nonce:
            raise ValueError(
                f"Dispute nonce {nonce} must exceed current nonce {current_nonce}"
            )

        rows = repo.dispute(
            channel_id=channel_id,
            nonce=nonce,
            balance_a=balance_a,
            balance_b=balance_b,
            sig_a=sig_a,
            sig_b=sig_b,
        )
        if rows == 0:
            raise ValueError("Dispute update failed")
        db.flush()

        channel = repo.get_by_id(channel_id)
        result = _channel_to_dict(channel)

        audit_log(
            db=db, action="channel.disputed",
            resource_type="channel", resource_id=channel_id,
            agent_id=agent_id,
            new_values={"nonce": nonce},
        )

        return result

    def get_channel(self, db: Session, channel_id, agent_id) -> dict:
        """Get channel details, verifying the caller is a participant."""
        channel_id = _to_uuid(channel_id)
        repo = ChannelRepository(db)
        channel = repo.get_by_id(channel_id)
        if channel is None:
            raise LookupError(f"Channel {channel_id} not found")
        _verify_participant(channel, agent_id)
        return _channel_to_dict(channel)

    def list_channels(
        self,
        db: Session,
        agent_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """List channels for an agent with pagination."""
        repo = ChannelRepository(db)
        channels = repo.list_by_agent(agent_id, limit=limit + offset)
        paged = channels[offset:offset + limit]
        return {
            "total": len(channels),
            "channels": [_channel_to_dict(c) for c in paged],
            "limit": limit,
            "offset": offset,
        }

    def auto_settle_expired(self, db: Session) -> int:
        """Find CLOSING channels past their settlement period and settle them.

        Returns number of channels settled.
        """
        repo = ChannelRepository(db)
        ready = repo.get_channels_ready_to_settle()
        count = 0
        for channel in ready:
            rows = repo.settle(channel.id)
            if rows > 0:
                count += 1
        if count:
            db.flush()
        return count


def _to_uuid(value) -> UUID:
    """Coerce string or UUID to UUID."""
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _verify_participant(channel: PaymentChannel, agent_id) -> None:
    """Raise PermissionError if agent_id is not a channel participant."""
    a_id = _to_uuid(channel.agent_a_id)
    b_id = _to_uuid(channel.agent_b_id)
    check_id = _to_uuid(agent_id)
    if check_id not in (a_id, b_id):
        raise PermissionError(f"Agent {agent_id} is not a participant in this channel")
