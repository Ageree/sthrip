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


class ChannelStatus(str, _PyEnum):
    PENDING = "pending"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    DISPUTED = "disputed"


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
]
