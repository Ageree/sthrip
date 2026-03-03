"""
SQLAlchemy models for StealthPay database
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any
from enum import Enum as PyEnum

from sqlalchemy import (
    create_engine, Column, String, Integer, BigInteger, Boolean,
    DateTime, ForeignKey, Numeric, Text, JSON, Enum as SQLEnum,
    Index, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID, INET
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy.sql import func

Base = declarative_base()


# ═══════════════════════════════════════════════════════════════════════════════
# ENUMS
# ═══════════════════════════════════════════════════════════════════════════════

class PrivacyLevel(str, PyEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PARANOID = "paranoid"


class AgentTier(str, PyEnum):
    FREE = "free"
    VERIFIED = "verified"
    PREMIUM = "premium"
    ENTERPRISE = "enterprise"


class RateLimitTier(str, PyEnum):
    LOW = "low"
    STANDARD = "standard"
    HIGH = "high"
    UNLIMITED = "unlimited"


class TransactionStatus(str, PyEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    ORPHANED = "orphaned"


class PaymentType(str, PyEnum):
    P2P = "p2p"
    HUB_ROUTING = "hub_routing"
    ESCROW_DEPOSIT = "escrow_deposit"
    ESCROW_RELEASE = "escrow_release"
    CHANNEL_OPEN = "channel_open"
    CHANNEL_CLOSE = "channel_close"
    FEE_COLLECTION = "fee_collection"


class EscrowStatus(str, PyEnum):
    PENDING = "pending"
    FUNDED = "funded"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    DISPUTED = "disputed"
    REFUNDED = "refunded"
    EXPIRED = "expired"


class ChannelStatus(str, PyEnum):
    PENDING = "pending"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    DISPUTED = "disputed"


class WebhookStatus(str, PyEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    RETRYING = "retrying"


class HubRouteStatus(str, PyEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    SETTLED = "settled"
    FAILED = "failed"


class FeeCollectionStatus(str, PyEnum):
    PENDING = "pending"
    COLLECTED = "collected"
    WITHDRAWN = "withdrawn"


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
    api_key_hash = Column(String(255), nullable=True)
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
    verified_at = Column(DateTime, nullable=True)
    verified_by = Column(String(255), nullable=True)
    
    # Staking
    staked_amount = Column(Numeric(20, 8), default=Decimal('0'))
    staked_token = Column(String(10), default='USDC')
    
    # Status
    is_active = Column(Boolean, default=True)
    last_seen_at = Column(DateTime, nullable=True)
    
    # Rate limiting
    rate_limit_tier = Column(SQLEnum(RateLimitTier), default=RateLimitTier.STANDARD)
    
    # Timestamps
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    
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
    
    calculated_at = Column(DateTime, default=func.now())
    
    # Relationships
    agent = relationship("Agent", back_populates="reputation")


class Transaction(Base):
    """On-chain transactions (observed)"""
    __tablename__ = "transactions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    tx_hash = Column(String(255), unique=True, nullable=False)
    network = Column(String(50), nullable=False)
    token = Column(String(20), default='XMR')
    
    from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
    to_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
    
    amount = Column(Numeric(20, 12), nullable=False)
    fee = Column(Numeric(20, 12), default=Decimal('0'))
    fee_collected = Column(Numeric(20, 12), default=Decimal('0'))
    
    payment_type = Column(SQLEnum(PaymentType), default=PaymentType.P2P)
    status = Column(SQLEnum(TransactionStatus), default=TransactionStatus.PENDING)
    block_number = Column(BigInteger, nullable=True)
    confirmations = Column(Integer, default=0)
    
    memo = Column(Text, nullable=True)
    tx_metadata = Column("metadata", JSON, default=dict)

    confirmed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())

    # Relationships
    from_agent = relationship("Agent", foreign_keys=[from_agent_id], back_populates="sent_transactions")
    to_agent = relationship("Agent", foreign_keys=[to_agent_id], back_populates="received_transactions")


class EscrowDeal(Base):
    """2-of-3 multisig escrow deals"""
    __tablename__ = "escrow_deals"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deal_hash = Column(String(64), unique=True, nullable=False)
    
    buyer_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    seller_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    arbiter_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
    
    amount = Column(Numeric(20, 12), nullable=False)
    token = Column(String(20), default='XMR')
    description = Column(Text, nullable=True)
    
    # Fees
    platform_fee_percent = Column(Numeric(5, 4), default=Decimal('0.01'))
    platform_fee_amount = Column(Numeric(20, 12), default=Decimal('0'))
    arbiter_fee_percent = Column(Numeric(5, 4), default=Decimal('0.005'))
    arbiter_fee_amount = Column(Numeric(20, 12), default=Decimal('0'))
    
    timeout_hours = Column(Integer, default=48)
    
    status = Column(SQLEnum(EscrowStatus), default=EscrowStatus.PENDING)
    
    deposit_tx_hash = Column(String(255), ForeignKey("transactions.tx_hash"), nullable=True)
    release_tx_hash = Column(String(255), ForeignKey("transactions.tx_hash"), nullable=True)
    multisig_address = Column(String(255), nullable=True)
    
    # Dispute
    disputed_at = Column(DateTime, nullable=True)
    disputed_by = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
    dispute_reason = Column(Text, nullable=True)
    arbiter_decision = Column(String(20), nullable=True)
    arbiter_signature = Column(Text, nullable=True)

    deal_metadata = Column("metadata", JSON, default=dict)

    created_at = Column(DateTime, default=func.now())
    funded_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    
    # Relationships
    buyer = relationship("Agent", foreign_keys=[buyer_id], back_populates="escrow_deals_as_buyer")
    seller = relationship("Agent", foreign_keys=[seller_id], back_populates="escrow_deals_as_seller")


class PaymentChannel(Base):
    """Payment channels for off-chain payments"""
    __tablename__ = "payment_channels"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    channel_hash = Column(String(64), unique=True, nullable=False)
    
    agent_a_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    agent_b_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    
    capacity = Column(Numeric(20, 12), nullable=False)
    status = Column(SQLEnum(ChannelStatus), default=ChannelStatus.PENDING)
    
    funding_tx_hash = Column(String(255), ForeignKey("transactions.tx_hash"), nullable=True)
    closing_tx_hash = Column(String(255), ForeignKey("transactions.tx_hash"), nullable=True)
    multisig_address = Column(String(255), nullable=True)
    
    current_state = Column(JSON, nullable=True)
    
    created_at = Column(DateTime, default=func.now())
    funded_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    
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
    
    created_at = Column(DateTime, default=func.now())
    
    # Unique constraint
    __table_args__ = (UniqueConstraint('channel_id', 'sequence_number'),)
    
    # Relationships
    channel = relationship("PaymentChannel", back_populates="states")


class HubRoute(Base):
    """Hub-routed payments with fee collection"""
    __tablename__ = "hub_routes"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payment_id = Column(String(64), unique=True, nullable=False)
    
    from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    to_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    
    amount = Column(Numeric(20, 12), nullable=False)
    token = Column(String(20), default='XMR')
    
    fee_percent = Column(Numeric(5, 4), default=Decimal('0.001'))
    fee_amount = Column(Numeric(20, 12), nullable=False)
    fee_collected = Column(Boolean, default=False)
    fee_collected_at = Column(DateTime, nullable=True)
    
    instant_confirmation = Column(Boolean, default=True)
    status = Column(SQLEnum(HubRouteStatus), default=HubRouteStatus.PENDING)
    
    settlement_tx_hash = Column(String(255), ForeignKey("transactions.tx_hash"), nullable=True)
    
    created_at = Column(DateTime, default=func.now())
    confirmed_at = Column(DateTime, nullable=True)
    settled_at = Column(DateTime, nullable=True)


class WebhookEvent(Base):
    """Webhook events for reliable delivery"""
    __tablename__ = "webhook_events"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    
    event_type = Column(String(100), nullable=False)
    payload = Column(JSON, nullable=False)
    
    status = Column(SQLEnum(WebhookStatus), default=WebhookStatus.PENDING)
    attempt_count = Column(Integer, default=0)
    max_attempts = Column(Integer, default=5)
    
    last_response_code = Column(Integer, nullable=True)
    last_response_body = Column(Text, nullable=True)
    last_error = Column(Text, nullable=True)
    
    next_attempt_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=func.now())


class ApiSession(Base):
    """API sessions for authentication"""
    __tablename__ = "api_sessions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    
    session_token_hash = Column(String(255), nullable=False)
    
    ip_address = Column(INET, nullable=True)
    user_agent = Column(Text, nullable=True)
    
    is_active = Column(Boolean, default=True)
    
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=func.now())
    last_used_at = Column(DateTime, nullable=True)


class AuditLog(Base):
    """Audit log for all actions"""
    __tablename__ = "audit_log"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
    
    action = Column(String(100), nullable=False)
    resource_type = Column(String(50), nullable=True)
    resource_id = Column(UUID(as_uuid=True), nullable=True)
    
    ip_address = Column(INET, nullable=True)
    request_method = Column(String(10), nullable=True)
    request_path = Column(Text, nullable=True)
    request_body = Column(JSON, nullable=True)
    
    old_values = Column(JSON, nullable=True)
    new_values = Column(JSON, nullable=True)
    
    success = Column(Boolean, nullable=True)
    error_message = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=func.now())


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
    withdrawn_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=func.now())


class AgentBalance(Base):
    __tablename__ = "agent_balances"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    token = Column(String(10), nullable=False, default="XMR")
    available = Column(Numeric(20, 12), nullable=False, default=0)
    pending = Column(Numeric(20, 12), nullable=False, default=0)
    total_deposited = Column(Numeric(20, 12), nullable=False, default=0)
    total_withdrawn = Column(Numeric(20, 12), nullable=False, default=0)
    deposit_address = Column(String(200))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("agent_id", "token", name="uq_agent_balance"),
    )
