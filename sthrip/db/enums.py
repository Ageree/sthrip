"""
Enum definitions for Sthrip database models.

All enums subclass both str and Python's Enum so that:
  - SQLAlchemy can store/retrieve them as plain VARCHAR values.
  - Comparisons against raw strings work transparently.

Import from here directly, or from sthrip.db.models for backward
compatibility (models.py re-exports every name defined in this module).
"""

from enum import Enum as _PyEnum


class PrivacyLevel(str, _PyEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PARANOID = "paranoid"


class AgentTier(str, _PyEnum):
    FREE = "free"
    VERIFIED = "verified"
    PREMIUM = "premium"
    ENTERPRISE = "enterprise"


class RateLimitTier(str, _PyEnum):
    LOW = "low"
    STANDARD = "standard"
    HIGH = "high"
    UNLIMITED = "unlimited"


class TransactionStatus(str, _PyEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    ORPHANED = "orphaned"


class PaymentType(str, _PyEnum):
    P2P = "p2p"
    HUB_ROUTING = "hub_routing"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    ESCROW_DEPOSIT = "escrow_deposit"
    ESCROW_RELEASE = "escrow_release"
    CHANNEL_OPEN = "channel_open"
    CHANNEL_CLOSE = "channel_close"
    FEE_COLLECTION = "fee_collection"


class EscrowStatus(str, _PyEnum):
    CREATED = "created"
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    PARTIALLY_COMPLETED = "partially_completed"


class MilestoneStatus(str, _PyEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ChannelStatus(str, _PyEnum):
    PENDING = "pending"
    OPEN = "open"
    CLOSING = "closing"
    SETTLED = "settled"
    CLOSED = "closed"
    DISPUTED = "disputed"


class RecurringInterval(str, _PyEnum):
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class StreamStatus(str, _PyEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"


class WebhookStatus(str, _PyEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    RETRYING = "retrying"


class HubRouteStatus(str, _PyEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    SETTLED = "settled"
    FAILED = "failed"


class FeeCollectionStatus(str, _PyEnum):
    PENDING = "pending"
    COLLECTED = "collected"
    WITHDRAWN = "withdrawn"


class WithdrawalStatus(str, _PyEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class MultisigState(str, _PyEnum):
    SETUP_ROUND_1 = "setup_round_1"
    SETUP_ROUND_2 = "setup_round_2"
    SETUP_ROUND_3 = "setup_round_3"
    FUNDED = "funded"
    ACTIVE = "active"
    RELEASING = "releasing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"


class SLAStatus(str, _PyEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    ACTIVE = "active"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    BREACHED = "breached"
    DISPUTED = "disputed"


class MatchRequestStatus(str, _PyEnum):
    SEARCHING = "searching"
    MATCHED = "matched"
    ASSIGNED = "assigned"
    EXPIRED = "expired"


class SwapStatus(str, _PyEnum):
    CREATED = "created"
    LOCKED = "locked"
    COMPLETED = "completed"
    REFUNDED = "refunded"
    EXPIRED = "expired"


__all__ = [
    "PrivacyLevel",
    "AgentTier",
    "RateLimitTier",
    "TransactionStatus",
    "PaymentType",
    "EscrowStatus",
    "ChannelStatus",
    "WebhookStatus",
    "HubRouteStatus",
    "FeeCollectionStatus",
    "WithdrawalStatus",
    "MilestoneStatus",
    "MultisigState",
    "SLAStatus",
    "MatchRequestStatus",
    "RecurringInterval",
    "StreamStatus",
    "SwapStatus",
]
