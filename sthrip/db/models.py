"""
SQLAlchemy models for Sthrip database
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any

from sqlalchemy import (
    create_engine, Column, String, Integer, BigInteger, Boolean,
    DateTime, ForeignKey, Numeric, Text, JSON, Enum as SQLEnum,
    Index, UniqueConstraint, CheckConstraint
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker
from sqlalchemy.sql import func

# Enum definitions live in sthrip.db.enums.  Import them here so that all
# existing code that does `from sthrip.db.models import <EnumName>` continues
# to work without any changes (backward-compatible re-export).
from sthrip.db.enums import (  # noqa: F401  (re-exported for backward compat)
    PrivacyLevel,
    AgentTier,
    RateLimitTier,
    TransactionStatus,
    PaymentType,
    EscrowStatus,
    MilestoneStatus,
    ChannelStatus,
    WebhookStatus,
    HubRouteStatus,
    FeeCollectionStatus,
    WithdrawalStatus,
    MultisigState,
    SLAStatus,
    MatchRequestStatus,
    RecurringInterval,
    StreamStatus,
    SwapStatus,
    LoanStatus,
    ConditionalPaymentState,
    MultiPartyPaymentState,
)


class Base(DeclarativeBase):
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class Agent(Base):
    """Agent registration and identity"""
    __tablename__ = "agents"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_name = Column(String(255), unique=True, nullable=False)
    did = Column(String(255), unique=True, nullable=True)
    
    # Authentication
    api_key_hash = Column(String(255), nullable=True, index=True)
    webhook_url = Column(Text, nullable=True)
    webhook_secret = Column(String(255), nullable=True)
    
    # Settings
    privacy_level = Column(SQLEnum(PrivacyLevel), default=PrivacyLevel.MEDIUM)
    
    # Wallet addresses (public only)
    xmr_address = Column(String(255), nullable=True)
    base_address = Column(String(255), nullable=True)
    solana_address = Column(String(255), nullable=True)
    
    # Tier & Verification
    tier = Column(SQLEnum(AgentTier), default=AgentTier.FREE)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verified_by = Column(String(255), nullable=True)
    
    # Staking
    staked_amount = Column(Numeric(20, 8), default=Decimal('0'))
    staked_token = Column(String(10), default='USDC')
    
    # Marketplace
    capabilities = Column(JSON, default=list)  # ["translation", "code-review", ...]
    pricing = Column(JSON, default=dict)       # {"translation": "0.01 XMR/1000 words"}
    description = Column(Text, nullable=True)  # max 500 chars, enforced at API layer
    accepts_escrow = Column(Boolean, default=True)

    # E2E Encrypted Messaging
    encryption_public_key = Column(Text, nullable=True)  # base64-encoded Curve25519 public key

    # Status
    is_active = Column(Boolean, default=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    
    # Rate limiting
    rate_limit_tier = Column(SQLEnum(RateLimitTier), default=RateLimitTier.STANDARD)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    
    # Relationships
    reputation = relationship("AgentReputation", back_populates="agent", uselist=False)
    sent_transactions = relationship("Transaction", foreign_keys="Transaction.from_agent_id", back_populates="from_agent")
    received_transactions = relationship("Transaction", foreign_keys="Transaction.to_agent_id", back_populates="to_agent")
    escrow_deals_as_buyer = relationship("EscrowDeal", foreign_keys="EscrowDeal.buyer_id", back_populates="buyer")
    escrow_deals_as_seller = relationship("EscrowDeal", foreign_keys="EscrowDeal.seller_id", back_populates="seller")
    channels_as_a = relationship("PaymentChannel", foreign_keys="PaymentChannel.agent_a_id", back_populates="agent_a")
    channels_as_b = relationship("PaymentChannel", foreign_keys="PaymentChannel.agent_b_id", back_populates="agent_b")


class AgentReputation(Base):
    """Agent reputation metrics"""
    __tablename__ = "agent_reputation"
    
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    
    # Core metrics
    total_transactions = Column(Integer, default=0)
    successful_transactions = Column(Integer, default=0)
    failed_transactions = Column(Integer, default=0)
    disputed_transactions = Column(Integer, default=0)
    
    # Ratings
    average_rating = Column(Numeric(3, 2), default=Decimal('0'))
    total_reviews = Column(Integer, default=0)
    
    # Calculated trust score
    trust_score = Column(Integer, default=0)
    
    # Volume
    total_volume_usd = Column(Numeric(20, 2), default=Decimal('0'))
    total_fees_paid = Column(Numeric(20, 8), default=Decimal('0'))
    
    # Raw data
    raw_data = Column(JSON, default=dict)

    # ZK reputation proofs (Task 7)
    reputation_commitment = Column(Text, nullable=True)   # serialized Pedersen commitment
    reputation_blinding = Column(Text, nullable=True)      # blinding factor (private, never exposed via API)

    calculated_at = Column(DateTime(timezone=True), default=func.now())

    # Relationships
    agent = relationship("Agent", back_populates="reputation")


class Transaction(Base):
    """On-chain transactions (observed)"""
    __tablename__ = "transactions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    tx_hash = Column(String(255), unique=True, nullable=False)
    network = Column(String(50), nullable=False)
    token = Column(String(20), default='XMR')
    
    from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True, index=True)
    to_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True, index=True)
    
    amount = Column(Numeric(20, 12), nullable=False)
    fee = Column(Numeric(20, 12), default=Decimal('0'))
    fee_collected = Column(Numeric(20, 12), default=Decimal('0'))
    
    payment_type = Column(SQLEnum(PaymentType), default=PaymentType.P2P)
    status = Column(SQLEnum(TransactionStatus), default=TransactionStatus.PENDING)
    block_number = Column(BigInteger, nullable=True)
    confirmations = Column(Integer, default=0)
    
    memo = Column(Text, nullable=True)
    tx_metadata = Column("metadata", JSON, default=dict)

    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())

    # Relationships
    from_agent = relationship("Agent", foreign_keys=[from_agent_id], back_populates="sent_transactions")
    to_agent = relationship("Agent", foreign_keys=[to_agent_id], back_populates="received_transactions")

    __table_args__ = (
        Index("ix_transactions_status", "status"),
        Index("ix_transactions_status_created", "status", "created_at"),
        Index("ix_transactions_from_agent_created", "from_agent_id", "created_at"),
        Index("ix_transactions_to_agent_created", "to_agent_id", "created_at"),
    )


class EscrowDeal(Base):
    """Hub-held escrow deals (no multisig, no arbiter)"""
    __tablename__ = "escrow_deals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deal_hash = Column(String(64), unique=True, nullable=False)

    # Participants
    buyer_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    seller_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)

    # Deal terms
    amount = Column(Numeric(20, 12), nullable=False)
    token = Column(String(20), default='XMR')
    description = Column(Text, nullable=True)

    # Fee (0.1% of released amount)
    fee_percent = Column(Numeric(5, 4), default=Decimal('0.001'))
    fee_amount = Column(Numeric(20, 12), default=Decimal('0'))

    # Timeouts (hours)
    accept_timeout_hours = Column(Integer, default=24)
    delivery_timeout_hours = Column(Integer, default=48)
    review_timeout_hours = Column(Integer, default=24)

    # Deadlines (computed from timeouts at state transitions)
    accept_deadline = Column(DateTime(timezone=True), nullable=True)
    delivery_deadline = Column(DateTime(timezone=True), nullable=True)
    review_deadline = Column(DateTime(timezone=True), nullable=True)

    # Release (single-milestone)
    release_amount = Column(Numeric(20, 12), nullable=True)

    # Multi-milestone
    is_multi_milestone = Column(Boolean, default=False)
    milestone_count = Column(Integer, default=1)
    current_milestone = Column(Integer, default=1)
    total_released = Column(Numeric(20, 12), default=Decimal('0'))
    total_fees = Column(Numeric(20, 12), default=Decimal('0'))

    status = Column(SQLEnum(EscrowStatus), default=EscrowStatus.CREATED)

    deal_metadata = Column("metadata", JSON, default=dict)

    # Timestamps
    created_at = Column(DateTime(timezone=True), default=func.now())
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    buyer = relationship("Agent", foreign_keys=[buyer_id], back_populates="escrow_deals_as_buyer")
    seller = relationship("Agent", foreign_keys=[seller_id], back_populates="escrow_deals_as_seller")
    milestones = relationship("EscrowMilestone", back_populates="escrow",
                              order_by="EscrowMilestone.sequence",
                              cascade="all, delete-orphan")


class EscrowMilestone(Base):
    """Individual milestone within a multi-milestone escrow deal."""
    __tablename__ = "escrow_milestones"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    escrow_id = Column(UUID(as_uuid=True), ForeignKey("escrow_deals.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)

    # Terms
    description = Column(Text, nullable=False)
    amount = Column(Numeric(20, 12), nullable=False)

    # Per-milestone timeouts (hours)
    delivery_timeout_hours = Column(Integer, nullable=False)
    review_timeout_hours = Column(Integer, nullable=False)

    # Deadlines (set when milestone becomes ACTIVE / DELIVERED)
    delivery_deadline = Column(DateTime(timezone=True), nullable=True)
    review_deadline = Column(DateTime(timezone=True), nullable=True)

    # Release
    release_amount = Column(Numeric(20, 12), nullable=True)
    fee_amount = Column(Numeric(20, 12), default=Decimal('0'))

    status = Column(SQLEnum(MilestoneStatus), default=MilestoneStatus.PENDING)

    # Timestamps
    activated_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    escrow = relationship("EscrowDeal", back_populates="milestones")

    __table_args__ = (
        UniqueConstraint("escrow_id", "sequence", name="uq_milestone_sequence"),
        CheckConstraint("sequence >= 1 AND sequence <= 10", name="ck_milestone_sequence_range"),
        CheckConstraint("amount > 0", name="ck_milestone_amount_positive"),
    )


class PaymentChannel(Base):
    """Payment channels for off-chain payments"""
    __tablename__ = "payment_channels"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_hash = Column(String(64), unique=True, nullable=False)
    
    agent_a_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    agent_b_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    
    capacity = Column(Numeric(20, 12), nullable=False)
    status = Column(SQLEnum(ChannelStatus), default=ChannelStatus.PENDING)
    
    funding_tx_hash = Column(String(255), ForeignKey("transactions.tx_hash"), nullable=True)
    closing_tx_hash = Column(String(255), ForeignKey("transactions.tx_hash"), nullable=True)
    multisig_address = Column(String(255), nullable=True)
    
    current_state = Column(JSON, nullable=True)
    
    created_at = Column(DateTime(timezone=True), default=func.now())
    funded_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    # Phase 3b: off-chain channel fields
    deposit_a = Column(Numeric(20, 8), default=Decimal('0'))
    deposit_b = Column(Numeric(20, 8), default=Decimal('0'))
    balance_a = Column(Numeric(20, 8), default=Decimal('0'))
    balance_b = Column(Numeric(20, 8), default=Decimal('0'))
    nonce = Column(Integer, default=0)
    last_update_sig_a = Column(Text, nullable=True)
    last_update_sig_b = Column(Text, nullable=True)
    settlement_period = Column(Integer, default=3600)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    closes_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    agent_a = relationship("Agent", foreign_keys=[agent_a_id], back_populates="channels_as_a")
    agent_b = relationship("Agent", foreign_keys=[agent_b_id], back_populates="channels_as_b")
    states = relationship("ChannelState", back_populates="channel", cascade="all, delete-orphan")


class ChannelState(Base):
    """Channel state history"""
    __tablename__ = "channel_states"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_id = Column(UUID(as_uuid=True), ForeignKey("payment_channels.id", ondelete="CASCADE"), nullable=False)
    
    sequence_number = Column(Integer, nullable=False)
    balance_a = Column(Numeric(20, 12), nullable=False)
    balance_b = Column(Numeric(20, 12), nullable=False)
    
    signature_a = Column(Text, nullable=True)
    signature_b = Column(Text, nullable=True)
    
    state_hash = Column(String(64), nullable=False)
    
    created_at = Column(DateTime(timezone=True), default=func.now())
    
    # Unique constraint
    __table_args__ = (UniqueConstraint('channel_id', 'sequence_number'),)
    
    # Relationships
    channel = relationship("PaymentChannel", back_populates="states")


class HubRoute(Base):
    """Hub-routed payments with fee collection"""
    __tablename__ = "hub_routes"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payment_id = Column(String(64), unique=True, nullable=False)
    
    from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    to_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)

    amount = Column(Numeric(20, 12), nullable=False)
    token = Column(String(20), default='XMR')

    fee_percent = Column(Numeric(5, 4), default=Decimal('0.001'))
    fee_amount = Column(Numeric(20, 12), nullable=False)
    fee_collected = Column(Boolean, default=False)
    fee_collected_at = Column(DateTime(timezone=True), nullable=True)

    instant_confirmation = Column(Boolean, default=True)
    status = Column(SQLEnum(HubRouteStatus), default=HubRouteStatus.PENDING)

    settlement_tx_hash = Column(String(255), ForeignKey("transactions.tx_hash"), nullable=True)

    created_at = Column(DateTime(timezone=True), default=func.now())
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_hub_routes_status", "status"),
    )


class WebhookEvent(Base):
    """Webhook events for reliable delivery"""
    __tablename__ = "webhook_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)

    event_type = Column(String(100), nullable=False)
    payload = Column(JSON, nullable=False)

    status = Column(SQLEnum(WebhookStatus), default=WebhookStatus.PENDING)
    attempt_count = Column(Integer, default=0)
    max_attempts = Column(Integer, default=5)

    last_response_code = Column(Integer, nullable=True)
    last_response_body = Column(Text, nullable=True)
    last_error = Column(Text, nullable=True)

    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=func.now())

    __table_args__ = (
        Index("ix_webhook_events_pending", "status", "next_attempt_at"),
    )


class AuditLog(Base):
    """Audit log for all actions.

    Tamper-evident chain (F-11):
    - prev_hmac: HMAC of the previous row's entry_hmac (genesis sentinel for
      the first row).
    - entry_hmac: HMAC-SHA256 of (prev_hmac, action, agent_id, ip,
      timestamp_iso, sanitized_details_json).  Computed in Python before
      insert so the value is available immediately without a round-trip.
    - entry_hmac has a UNIQUE index to prevent silent duplicate injection.

    Chain integrity is established from the migration point forward.
    Pre-migration rows are backfilled with computed values but their
    tamper-history prior to migration is undetectable.
    """
    __tablename__ = "audit_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True, index=True)

    action = Column(String(100), nullable=False, index=True)
    resource_type = Column(String(50), nullable=True)
    resource_id = Column(UUID(as_uuid=True), nullable=True)

    ip_address = Column(String(45), nullable=True)  # IPv4/IPv6 as string (portable)
    request_method = Column(String(10), nullable=True)
    request_path = Column(Text, nullable=True)
    request_body = Column(JSON, nullable=True)

    old_values = Column(JSON, nullable=True)
    new_values = Column(JSON, nullable=True)

    success = Column(Boolean, nullable=True)
    error_message = Column(Text, nullable=True)

    # created_at is set explicitly in Python (not server-side) so that the
    # timestamp is available for HMAC computation before INSERT, and so that
    # ordering by (created_at, id) provides stable insertion ordering for
    # chain verification.
    created_at = Column(DateTime(timezone=True), default=func.now())

    # Tamper-evident chain columns (F-11) — nullable to support backfill migration.
    prev_hmac = Column(String(64), nullable=True)
    entry_hmac = Column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_audit_log_entry_hmac", "entry_hmac", unique=True),
        Index("ix_audit_log_created_at", "created_at"),
    )


class FeeCollection(Base):
    """Revenue tracking"""
    __tablename__ = "fee_collections"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    source_type = Column(String(50), nullable=False)
    source_id = Column(UUID(as_uuid=True), nullable=True)

    amount = Column(Numeric(20, 12), nullable=False)
    token = Column(String(20), nullable=False)
    usd_value_at_collection = Column(Numeric(20, 2), nullable=True)

    status = Column(SQLEnum(FeeCollectionStatus), default=FeeCollectionStatus.PENDING)

    collection_tx_hash = Column(String(255), nullable=True)
    withdrawn_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=func.now())

    __table_args__ = (
        Index("ix_fee_collections_status_created", "status", "created_at"),
    )


class SystemState(Base):
    """Key-value store for system state (e.g. last_scanned_height)."""
    __tablename__ = "system_state"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class PendingWithdrawal(Base):
    """Saga journal for withdrawal operations."""
    __tablename__ = "pending_withdrawals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    amount = Column(Numeric(precision=18, scale=12), nullable=False)
    address = Column(String(256), nullable=False)
    status = Column(SQLEnum(WithdrawalStatus), nullable=False, default=WithdrawalStatus.PENDING)
    tx_hash = Column(String(128), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_pending_withdrawals_status_created", "status", "created_at"),
    )


class AgentBalance(Base):
    __tablename__ = "agent_balances"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    token = Column(String(10), nullable=False, default="XMR")
    available = Column(Numeric(20, 12), nullable=False, default=0)
    pending = Column(Numeric(20, 12), nullable=False, default=0)
    total_deposited = Column(Numeric(20, 12), nullable=False, default=0)
    total_withdrawn = Column(Numeric(20, 12), nullable=False, default=0)
    deposit_address = Column(String(200), index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("agent_id", "token", name="uq_agent_balance"),
        CheckConstraint("available >= 0", name="ck_balance_available_non_negative"),
        CheckConstraint("pending >= 0", name="ck_balance_pending_non_negative"),
    )


class SpendingPolicy(Base):
    """Per-agent spending controls and limits."""
    __tablename__ = "spending_policies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )

    # Per-transaction cap
    max_per_tx = Column(Numeric(20, 8), nullable=True)

    # Per-session cap (rolling window)
    max_per_session = Column(Numeric(20, 8), nullable=True)

    # Daily spending limit (rolling 24 h window)
    daily_limit = Column(Numeric(20, 8), nullable=True)

    # Recipient allow/block lists (fnmatch glob patterns)
    allowed_agents = Column(JSON, nullable=True)   # e.g. ["research-*"]
    blocked_agents = Column(JSON, nullable=True)

    # Auto-require escrow above this amount
    require_escrow_above = Column(Numeric(20, 8), nullable=True)

    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    # Relationships
    agent = relationship("Agent", backref="spending_policy", uselist=False)


class WebhookEndpoint(Base):
    """Self-service webhook endpoint registration for agents."""
    __tablename__ = "webhook_endpoints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    url = Column(String(2048), nullable=False)
    description = Column(String(256), nullable=True)
    secret_encrypted = Column(Text, nullable=False)
    event_filters = Column(JSON, nullable=True)  # ["payment.*", "escrow.*"] or null=all
    is_active = Column(Boolean, default=True)
    failure_count = Column(Integer, default=0)
    disabled_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    # Relationships
    agent = relationship("Agent", backref="webhook_endpoints")

    __table_args__ = (
        UniqueConstraint("agent_id", "url", name="uq_agent_webhook_url"),
    )


class MessageRelay(Base):
    """Ephemeral encrypted message relay. Hub stores ciphertext temporarily, never plaintext."""
    __tablename__ = "message_relays"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    to_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    payment_id = Column(String(64), nullable=True)
    ciphertext = Column(Text, nullable=False)  # base64-encoded NaCl Box ciphertext
    nonce = Column(String(64), nullable=False)  # base64-encoded nonce
    sender_public_key = Column(String(64), nullable=False)  # base64-encoded
    size_bytes = Column(Integer, nullable=False)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())


class MultisigEscrow(Base):
    """2-of-3 Monero multisig escrow deal."""
    __tablename__ = "multisig_escrows"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    escrow_deal_id = Column(
        UUID(as_uuid=True),
        ForeignKey("escrow_deals.id"),
        nullable=False,
        unique=True,
    )
    multisig_address = Column(String(255), nullable=True)

    # Wallet IDs for each participant (assigned during setup)
    buyer_wallet_id = Column(String(255), nullable=True)
    seller_wallet_id = Column(String(255), nullable=True)
    hub_wallet_id = Column(String(255), nullable=True)

    # State machine: setup_round_1 -> setup_round_2 -> setup_round_3 -> funded -> active -> ...
    state = Column(String(50), default="setup_round_1")

    # Fee collected upfront (1% of deal amount)
    fee_collected = Column(Numeric(20, 8), default=Decimal("0"))

    # Amount that enters the multisig wallet (deal amount minus fee)
    funded_amount = Column(Numeric(20, 8), nullable=True)
    funded_tx_hash = Column(String(255), nullable=True)

    # Release tracking
    release_tx_hex = Column(Text, nullable=True)
    release_initiator = Column(String(20), nullable=True)

    # Dispute
    dispute_reason = Column(Text, nullable=True)
    disputed_by = Column(String(20), nullable=True)

    timeout_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    # Relationships
    escrow_deal = relationship("EscrowDeal", backref="multisig_escrow", uselist=False)
    rounds = relationship(
        "MultisigRound",
        back_populates="multisig_escrow",
        cascade="all, delete-orphan",
        order_by="MultisigRound.round_number",
    )


class MultisigRound(Base):
    """Key exchange round data for multisig setup."""
    __tablename__ = "multisig_rounds"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    multisig_escrow_id = Column(
        UUID(as_uuid=True),
        ForeignKey("multisig_escrows.id"),
        nullable=False,
        index=True,
    )
    round_number = Column(Integer, nullable=False)
    participant = Column(String(20), nullable=False)  # "buyer", "seller", "hub"
    multisig_info = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())

    # Relationships
    multisig_escrow = relationship("MultisigEscrow", back_populates="rounds")

    __table_args__ = (
        UniqueConstraint(
            "multisig_escrow_id", "round_number", "participant",
            name="uq_multisig_round_participant",
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MARKETPLACE V2 — SLA CONTRACTS
# ═══════════════════════════════════════════════════════════════════════════════

class SLATemplate(Base):
    """Reusable service level agreement template published by a provider."""
    __tablename__ = "sla_templates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    service_description = Column(Text, nullable=False)
    deliverables = Column(JSON, default=list)
    response_time_secs = Column(Integer, nullable=False)
    delivery_time_secs = Column(Integer, nullable=False)
    base_price = Column(Numeric(20, 8), nullable=False)
    currency = Column(String(10), default="XMR")
    penalty_percent = Column(Integer, default=10)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=func.now())

    provider = relationship("Agent", backref="sla_templates")


class SLAContract(Base):
    """Concrete SLA contract between a consumer and provider."""
    __tablename__ = "sla_contracts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    consumer_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    template_id = Column(UUID(as_uuid=True), ForeignKey("sla_templates.id"), nullable=True)
    service_description = Column(Text, nullable=False)
    deliverables = Column(JSON, default=list)
    response_time_secs = Column(Integer, nullable=False)
    delivery_time_secs = Column(Integer, nullable=False)
    price = Column(Numeric(20, 8), nullable=False)
    currency = Column(String(10), default="XMR")
    penalty_percent = Column(Integer, default=10)
    state = Column(SQLEnum(SLAStatus), default=SLAStatus.PROPOSED)
    escrow_deal_id = Column(UUID(as_uuid=True), ForeignKey("escrow_deals.id"), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    response_time_actual = Column(Integer, nullable=True)
    delivery_time_actual = Column(Integer, nullable=True)
    sla_met = Column(Boolean, nullable=True)
    result_hash = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())

    provider = relationship("Agent", foreign_keys=[provider_id])
    consumer = relationship("Agent", foreign_keys=[consumer_id])
    template = relationship("SLATemplate")
    escrow_deal = relationship("EscrowDeal")


# ═══════════════════════════════════════════════════════════════════════════════
# MARKETPLACE V2 — REVIEWS & RATINGS
# ═══════════════════════════════════════════════════════════════════════════════

class AgentReview(Base):
    """Review tied to a completed transaction."""
    __tablename__ = "agent_reviews"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reviewer_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    reviewed_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    transaction_id = Column(UUID(as_uuid=True), nullable=False)
    transaction_type = Column(String(20), nullable=False)  # "payment", "escrow", "sla"
    overall_rating = Column(Integer, nullable=False)
    speed_rating = Column(Integer, nullable=True)
    quality_rating = Column(Integer, nullable=True)
    reliability_rating = Column(Integer, nullable=True)
    comment_encrypted = Column(Text, nullable=True)
    is_verified = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
    reviewer = relationship("Agent", foreign_keys=[reviewer_id])
    reviewed = relationship("Agent", foreign_keys=[reviewed_id])
    __table_args__ = (
        UniqueConstraint("reviewer_id", "transaction_id", name="uq_review_per_transaction"),
        CheckConstraint("overall_rating >= 1 AND overall_rating <= 5", name="ck_overall_rating_range"),
    )


class AgentRatingSummary(Base):
    """Materialized rating summary for an agent."""
    __tablename__ = "agent_rating_summary"

    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    total_reviews = Column(Integer, default=0)
    avg_overall = Column(Numeric(3, 2), default=Decimal('0'))
    avg_speed = Column(Numeric(3, 2), default=Decimal('0'))
    avg_quality = Column(Numeric(3, 2), default=Decimal('0'))
    avg_reliability = Column(Numeric(3, 2), default=Decimal('0'))
    five_star_count = Column(Integer, default=0)
    one_star_count = Column(Integer, default=0)
    last_review_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    agent = relationship("Agent", backref="rating_summary", uselist=False)


# ═══════════════════════════════════════════════════════════════════════════════
# MARKETPLACE V2 — MATCHMAKING
# ═══════════════════════════════════════════════════════════════════════════════

class MatchRequest(Base):
    """Automatic agent matchmaking request."""
    __tablename__ = "match_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requester_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    task_description = Column(Text, nullable=False)
    required_capabilities = Column(JSON, default=list)
    budget = Column(Numeric(20, 8), nullable=False)
    currency = Column(String(10), default="XMR")
    deadline_secs = Column(Integer, nullable=False)
    min_rating = Column(Numeric(3, 2), default=Decimal('0'))
    auto_assign = Column(Boolean, default=False)
    matched_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
    sla_contract_id = Column(UUID(as_uuid=True), ForeignKey("sla_contracts.id"), nullable=True)
    state = Column(SQLEnum(MatchRequestStatus), default=MatchRequestStatus.SEARCHING)
    created_at = Column(DateTime(timezone=True), default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    requester = relationship("Agent", foreign_keys=[requester_id])
    matched_agent = relationship("Agent", foreign_keys=[matched_agent_id])


# ═══════════════════════════════════════════════════════════════════════════════
# PAYMENT SCALING — CHANNELS, SUBSCRIPTIONS, STREAMS
# ═══════════════════════════════════════════════════════════════════════════════

class ChannelUpdate(Base):
    """Off-chain state update record for payment channels."""
    __tablename__ = "channel_updates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_id = Column(UUID(as_uuid=True), ForeignKey("payment_channels.id", ondelete="CASCADE"), nullable=False, index=True)
    nonce = Column(Integer, nullable=False)
    balance_a = Column(Numeric(20, 8), nullable=False)
    balance_b = Column(Numeric(20, 8), nullable=False)
    signature_a = Column(Text, nullable=True)
    signature_b = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())

    channel = relationship("PaymentChannel")

    __table_args__ = (
        UniqueConstraint("channel_id", "nonce", name="uq_channel_update_nonce"),
    )


class RecurringPayment(Base):
    """Server-side recurring payment schedule."""
    __tablename__ = "recurring_payments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    to_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    amount = Column(Numeric(20, 8), nullable=False)
    interval = Column(SQLEnum(RecurringInterval), nullable=False)
    next_payment_at = Column(DateTime(timezone=True), nullable=False)
    last_payment_at = Column(DateTime(timezone=True), nullable=True)
    total_paid = Column(Numeric(20, 8), default=Decimal('0'))
    max_payments = Column(Integer, nullable=True)
    payments_made = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    from_agent = relationship("Agent", foreign_keys=[from_agent_id])
    to_agent = relationship("Agent", foreign_keys=[to_agent_id])

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_recurring_amount_positive"),
    )


class PaymentStream(Base):
    """Real-time payment stream built on top of a payment channel."""
    __tablename__ = "payment_streams"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_id = Column(UUID(as_uuid=True), ForeignKey("payment_channels.id", ondelete="CASCADE"), nullable=False, index=True)
    from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    to_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    rate_per_second = Column(Numeric(20, 12), nullable=False)
    started_at = Column(DateTime(timezone=True), default=func.now())
    paused_at = Column(DateTime(timezone=True), nullable=True)
    stopped_at = Column(DateTime(timezone=True), nullable=True)
    total_streamed = Column(Numeric(20, 8), default=Decimal('0'))
    state = Column(SQLEnum(StreamStatus), default=StreamStatus.ACTIVE)

    channel = relationship("PaymentChannel")

    __table_args__ = (
        CheckConstraint("rate_per_second > 0", name="ck_stream_rate_positive"),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MULTI-CURRENCY — SWAPS & CONVERSIONS
# ═══════════════════════════════════════════════════════════════════════════════

class SwapOrder(Base):
    """Cross-chain swap order with HTLC."""
    __tablename__ = "swap_orders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    from_currency = Column(String(10), nullable=False)
    from_amount = Column(Numeric(20, 8), nullable=False)
    to_currency = Column(String(10), nullable=False, default="XMR")
    to_amount = Column(Numeric(20, 8), nullable=False)
    exchange_rate = Column(Numeric(20, 8), nullable=False)
    fee_amount = Column(Numeric(20, 8), nullable=False)
    state = Column(SQLEnum(SwapStatus), default=SwapStatus.CREATED)
    htlc_hash = Column(String(64), nullable=False)
    htlc_secret = Column(String(64), nullable=True)
    btc_tx_hash = Column(String(64), nullable=True)
    xmr_tx_hash = Column(String(64), nullable=True)
    lock_expiry = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())
    # Real exchange provider fields (added for ChangeNOW/SideShift integration)
    external_order_id = Column(String(128), nullable=True, index=True)
    deposit_address = Column(String(255), nullable=True)
    provider_name = Column(String(32), nullable=True)

    from_agent = relationship("Agent", foreign_keys=[from_agent_id])


class CurrencyConversion(Base):
    """Record of currency conversion between agent balances."""
    __tablename__ = "currency_conversions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    from_currency = Column(String(10), nullable=False)
    from_amount = Column(Numeric(20, 8), nullable=False)
    to_currency = Column(String(10), nullable=False)
    to_amount = Column(Numeric(20, 8), nullable=False)
    rate = Column(Numeric(20, 8), nullable=False)
    fee_amount = Column(Numeric(20, 8), nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())

    agent = relationship("Agent", foreign_keys=[agent_id])


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4a — AGENT FINANCIAL OS
# ═══════════════════════════════════════════════════════════════════════════════

class TreasuryPolicy(Base):
    """Agent treasury management configuration."""
    __tablename__ = "treasury_policies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )

    # Target allocation across currencies (percentages, must sum to 100)
    target_allocation = Column(JSON, nullable=False)  # {"XMR": 40, "xUSD": 50, "xEUR": 10}

    # Rebalance triggers
    rebalance_threshold_pct = Column(Integer, default=10)
    rebalance_cooldown_secs = Column(Integer, default=300)

    # Reserve requirements
    min_liquid_xmr = Column(Numeric(20, 8), nullable=True)
    min_liquid_xusd = Column(Numeric(20, 8), nullable=True)
    emergency_reserve_pct = Column(Integer, default=10)

    # Auto-lend settings
    auto_lend_enabled = Column(Boolean, default=False)
    max_lend_pct = Column(Integer, default=20)
    min_borrower_trust_score = Column(Integer, default=70)
    max_loan_duration_secs = Column(Integer, default=3600)

    is_active = Column(Boolean, default=True)
    last_rebalance_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    agent = relationship("Agent", backref="treasury_policy", uselist=False)


class TreasuryForecast(Base):
    """Predicted cash flow for treasury planning."""
    __tablename__ = "treasury_forecasts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)

    forecast_type = Column(String(50), nullable=False)  # subscription_due, escrow_release, loan_repayment
    source_id = Column(UUID(as_uuid=True), nullable=False)
    expected_amount = Column(Numeric(20, 8), nullable=False)
    expected_currency = Column(String(10), nullable=False, default="XMR")
    direction = Column(String(10), nullable=False)  # "inflow" or "outflow"
    expected_at = Column(DateTime(timezone=True), nullable=False)
    confidence = Column(Numeric(3, 2), nullable=False, default=Decimal("1.00"))

    created_at = Column(DateTime(timezone=True), default=func.now())

    __table_args__ = (
        Index("ix_treasury_forecasts_agent_expected", "agent_id", "expected_at"),
    )


class TreasuryRebalanceLog(Base):
    """Record of a treasury rebalance execution."""
    __tablename__ = "treasury_rebalance_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)

    trigger = Column(String(50), nullable=False)  # threshold_breach, forecast_adjustment, manual
    conversions = Column(JSON, nullable=False, default=list)
    pre_allocation = Column(JSON, nullable=False)
    post_allocation = Column(JSON, nullable=False)
    total_value_xusd = Column(Numeric(20, 8), nullable=False)

    created_at = Column(DateTime(timezone=True), default=func.now())

    __table_args__ = (
        Index("ix_treasury_rebalance_agent_created", "agent_id", "created_at"),
    )


class AgentCreditScore(Base):
    """Agent credit score derived from on-platform behavior."""
    __tablename__ = "agent_credit_scores"

    agent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )

    credit_score = Column(Integer, default=0)

    # Factors
    total_loans_taken = Column(Integer, default=0)
    total_loans_repaid = Column(Integer, default=0)
    total_loans_defaulted = Column(Integer, default=0)
    total_borrowed_volume = Column(Numeric(20, 8), default=Decimal("0"))
    avg_repayment_time_secs = Column(Integer, nullable=True)
    longest_default_secs = Column(Integer, nullable=True)

    # Derived limits
    max_borrow_amount = Column(Numeric(20, 8), default=Decimal("0"))
    max_concurrent_loans = Column(Integer, default=0)

    calculated_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    agent = relationship("Agent", backref="credit_score_record", uselist=False)


class AgentLoan(Base):
    """Loan between two agents."""
    __tablename__ = "agent_loans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    loan_hash = Column(String(64), unique=True, nullable=False)

    # Participants
    lender_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    borrower_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)

    # Terms
    principal = Column(Numeric(20, 8), nullable=False)
    currency = Column(String(10), default="XMR")
    interest_rate_bps = Column(Integer, nullable=False)  # basis points (100 = 1%)
    duration_secs = Column(Integer, nullable=False)
    collateral_amount = Column(Numeric(20, 8), default=Decimal("0"))
    collateral_currency = Column(String(10), nullable=True)

    # Repayment
    repayment_amount = Column(Numeric(20, 8), nullable=False)  # principal + interest
    repaid_amount = Column(Numeric(20, 8), default=Decimal("0"))

    # State
    state = Column(SQLEnum(LoanStatus), default=LoanStatus.REQUESTED, nullable=False)

    # Deadlines
    expires_at = Column(DateTime(timezone=True), nullable=False)
    grace_period_secs = Column(Integer, default=300)

    # Timestamps
    requested_at = Column(DateTime(timezone=True), default=func.now())
    funded_at = Column(DateTime(timezone=True), nullable=True)
    repaid_at = Column(DateTime(timezone=True), nullable=True)
    defaulted_at = Column(DateTime(timezone=True), nullable=True)

    # Fee
    platform_fee = Column(Numeric(20, 8), default=Decimal("0"))

    lender = relationship("Agent", foreign_keys=[lender_id])
    borrower = relationship("Agent", foreign_keys=[borrower_id])

    __table_args__ = (
        Index("ix_agent_loans_state", "state"),
        Index("ix_agent_loans_expires", "state", "expires_at"),
    )


class LendingOffer(Base):
    """Lending offer posted by an agent (order book for interest rate discovery)."""
    __tablename__ = "lending_offers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lender_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)

    max_amount = Column(Numeric(20, 8), nullable=False)
    currency = Column(String(10), default="XMR")
    interest_rate_bps = Column(Integer, nullable=False)  # minimum acceptable rate
    max_duration_secs = Column(Integer, nullable=False)
    min_borrower_credit_score = Column(Integer, default=0)
    require_collateral = Column(Boolean, default=False)
    collateral_ratio_pct = Column(Integer, default=100)

    is_active = Column(Boolean, default=True)
    remaining_amount = Column(Numeric(20, 8), nullable=False)

    created_at = Column(DateTime(timezone=True), default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)

    lender = relationship("Agent", foreign_keys=[lender_id])

    __table_args__ = (
        Index("ix_lending_offers_active", "is_active", "currency"),
    )


class ConditionalPayment(Base):
    """Payment that executes only when specified conditions are met."""
    __tablename__ = "conditional_payments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payment_hash = Column(String(64), unique=True, nullable=False)

    from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    to_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)

    amount = Column(Numeric(20, 8), nullable=False)
    currency = Column(String(10), default="XMR")
    memo = Column(Text, nullable=True)

    # Conditions
    condition_type = Column(String(50), nullable=False)  # webhook, escrow_completed, time_lock, balance_threshold
    condition_config = Column(JSON, nullable=False)

    # Funds locked from sender
    locked_amount = Column(Numeric(20, 8), nullable=False)

    # State
    state = Column(SQLEnum(ConditionalPaymentState), default=ConditionalPaymentState.PENDING, nullable=False)

    # Timeouts
    expires_at = Column(DateTime(timezone=True), nullable=False)

    # Timestamps
    created_at = Column(DateTime(timezone=True), default=func.now())
    triggered_at = Column(DateTime(timezone=True), nullable=True)
    executed_at = Column(DateTime(timezone=True), nullable=True)

    from_agent = relationship("Agent", foreign_keys=[from_agent_id])
    to_agent = relationship("Agent", foreign_keys=[to_agent_id])

    __table_args__ = (
        Index("ix_conditional_payments_state", "state"),
        Index("ix_conditional_payments_expires", "state", "expires_at"),
    )


class MultiPartyPayment(Base):
    """Atomic multi-party payment (all-or-nothing group payment)."""
    __tablename__ = "multi_party_payments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payment_hash = Column(String(64), unique=True, nullable=False)

    sender_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)

    total_amount = Column(Numeric(20, 8), nullable=False)
    currency = Column(String(10), default="XMR")

    require_all_accept = Column(Boolean, default=True)

    state = Column(SQLEnum(MultiPartyPaymentState), default=MultiPartyPaymentState.PENDING, nullable=False)

    accept_deadline = Column(DateTime(timezone=True), nullable=False)

    created_at = Column(DateTime(timezone=True), default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    sender = relationship("Agent", foreign_keys=[sender_id])
    recipients = relationship(
        "MultiPartyRecipient",
        back_populates="payment",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_multi_party_state", "state"),
    )


class MultiPartyRecipient(Base):
    """Individual recipient in a multi-party payment."""
    __tablename__ = "multi_party_recipients"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("multi_party_payments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recipient_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)

    amount = Column(Numeric(20, 8), nullable=False)

    accepted = Column(Boolean, nullable=True)  # null = pending, true = accepted, false = rejected
    accepted_at = Column(DateTime(timezone=True), nullable=True)

    payment = relationship("MultiPartyPayment", back_populates="recipients")
    recipient = relationship("Agent", foreign_keys=[recipient_id])

    __table_args__ = (
        UniqueConstraint("payment_id", "recipient_id", name="uq_multi_party_recipient"),
    )


# ---------------------------------------------------------------------------
# F-4: DB-backed idempotency key store
# ---------------------------------------------------------------------------

class IdempotencyKey(Base):
    """Persistent idempotency key store (F-4 fix).

    Authoritative record of every completed mutation request.  Redis is used
    as a write-through hot-path cache, but THIS table is the source of truth.
    Keys are retained indefinitely so that replay attacks after Redis TTL expiry
    are always detected.

    Unique constraint on (agent_id, endpoint, key) preserves the existing
    three-dimensional scoping: the same key value on /hub-routing and /withdraw
    are independent entries (matching the Redis key format).

    UUID column type
    ----------------
    Uses ``sqlalchemy.dialects.postgresql.UUID(as_uuid=True)`` which renders
    as the native ``UUID`` type on Postgres and degrades gracefully to a string
    representation on SQLite (test environment). This is consistent with all
    other primary-key columns in this file.

    Retention
    ---------
    Rows older than ``idempotency_db_retention_days`` (default 90) may be
    purged by a maintenance cron job (cleanup is out of scope for F-4;
    see sthrip/config.py). The retention window must be >> any realistic
    retry window — 90 days is conservative.
    """
    __tablename__ = "idempotency_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Scoping dimensions — must match the Redis key composition in IdempotencyStore._key
    agent_id = Column(String(255), nullable=False)
    endpoint = Column(String(255), nullable=False)
    key = Column(String(512), nullable=False)

    # sha256(canonical JSON body) — used to detect same-key + different-body (422)
    request_hash = Column(String(64), nullable=False)

    # Stored response (authoritative replay payload)
    response_status = Column(Integer, nullable=False, default=200)
    response_body = Column(JSON, nullable=False)

    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("agent_id", "endpoint", "key", name="uq_idempotency_agent_endpoint_key"),
        Index("ix_idempotency_keys_created_at", "created_at"),
    )
