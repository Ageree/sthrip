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
    ChannelStatus,
    WebhookStatus,
    HubRouteStatus,
    FeeCollectionStatus,
    WithdrawalStatus,
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

    # Release
    release_amount = Column(Numeric(20, 12), nullable=True)

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
    """Audit log for all actions"""
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
    
    created_at = Column(DateTime(timezone=True), default=func.now())


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
