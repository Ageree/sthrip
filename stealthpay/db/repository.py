"""
Repository pattern for database operations
"""

import hashlib
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import desc, func, and_

from . import models
from .models import AgentBalance


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT REPOSITORY
# ═══════════════════════════════════════════════════════════════════════════════

class AgentRepository:
    """Agent data access"""
    
    def __init__(self, db: Session):
        self.db = db
    
    def create_agent(
        self,
        agent_name: str,
        webhook_url: Optional[str] = None,
        privacy_level: str = "medium",
        tier: str = "free"
    ) -> models.Agent:
        """Create new agent with API key"""
        # Generate API key
        api_key = f"sk_{secrets.token_hex(32)}"
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        
        # Create agent
        agent = models.Agent(
            agent_name=agent_name,
            api_key_hash=api_key_hash,
            webhook_url=webhook_url,
            privacy_level=privacy_level,
            tier=tier,
            is_active=True
        )
        
        self.db.add(agent)
        self.db.flush()  # Get ID without committing
        
        # Create reputation record
        reputation = models.AgentReputation(agent_id=agent.id)
        self.db.add(reputation)
        
        # Store plain API key (returned once)
        agent._plain_api_key = api_key
        
        return agent
    
    def get_by_api_key(self, api_key: str) -> Optional[models.Agent]:
        """Get agent by API key"""
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        return self.db.query(models.Agent).filter(
            models.Agent.api_key_hash == api_key_hash,
            models.Agent.is_active == True
        ).first()
    
    def get_by_id(self, agent_id: UUID) -> Optional[models.Agent]:
        """Get agent by ID"""
        return self.db.query(models.Agent).filter(
            models.Agent.id == agent_id
        ).first()
    
    def get_by_name(self, agent_name: str) -> Optional[models.Agent]:
        """Get agent by name"""
        return self.db.query(models.Agent).filter(
            models.Agent.agent_name == agent_name
        ).first()
    
    def list_agents(
        self,
        tier: Optional[str] = None,
        is_active: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[models.Agent]:
        """List agents with optional filters"""
        query = self.db.query(models.Agent)
        
        if tier:
            query = query.filter(models.Agent.tier == tier)
        if is_active is not None:
            query = query.filter(models.Agent.is_active == is_active)
        
        return query.order_by(desc(models.Agent.created_at)).offset(offset).limit(limit).all()
    
    def update_last_seen(self, agent_id: UUID):
        """Update last seen timestamp"""
        self.db.query(models.Agent).filter(
            models.Agent.id == agent_id
        ).update({
            "last_seen_at": datetime.utcnow()
        })
    
    def update_wallet_addresses(
        self,
        agent_id: UUID,
        xmr_address: Optional[str] = None,
        base_address: Optional[str] = None,
        solana_address: Optional[str] = None
    ):
        """Update agent wallet addresses"""
        updates = {}
        if xmr_address:
            updates["xmr_address"] = xmr_address
        if base_address:
            updates["base_address"] = base_address
        if solana_address:
            updates["solana_address"] = solana_address
        
        if updates:
            self.db.query(models.Agent).filter(
                models.Agent.id == agent_id
            ).update(updates)


# ═══════════════════════════════════════════════════════════════════════════════
# TRANSACTION REPOSITORY
# ═══════════════════════════════════════════════════════════════════════════════

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
            fee=fee,
            fee_collected=fee_collected,
            memo=memo,
            metadata=metadata or {}
        )
        self.db.add(tx)
        return tx
    
    def get_by_hash(self, tx_hash: str) -> Optional[models.Transaction]:
        """Get transaction by hash"""
        return self.db.query(models.Transaction).filter(
            models.Transaction.tx_hash == tx_hash
        ).first()
    
    def list_by_agent(
        self,
        agent_id: UUID,
        direction: Optional[str] = None,  # 'in', 'out', None for both
        limit: int = 100,
        offset: int = 0
    ) -> List[models.Transaction]:
        """List transactions for agent"""
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
        
        return query.order_by(desc(models.Transaction.created_at)).offset(offset).limit(limit).all()
    
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
            "confirmed_at": datetime.utcnow()
        })
    
    def get_volume_by_agent(self, agent_id: UUID, days: int = 30) -> Decimal:
        """Get total volume for agent in last N days"""
        since = datetime.utcnow() - timedelta(days=days)
        
        result = self.db.query(func.sum(models.Transaction.amount)).filter(
            and_(
                (models.Transaction.from_agent_id == agent_id) |
                (models.Transaction.to_agent_id == agent_id),
                models.Transaction.status == models.TransactionStatus.CONFIRMED,
                models.Transaction.created_at >= since
            )
        ).scalar()
        
        return result or Decimal('0')


# ═══════════════════════════════════════════════════════════════════════════════
# ESCROW REPOSITORY
# ═══════════════════════════════════════════════════════════════════════════════

class EscrowRepository:
    """Escrow deal data access"""
    
    def __init__(self, db: Session):
        self.db = db
    
    def create(
        self,
        deal_hash: str,
        buyer_id: UUID,
        seller_id: UUID,
        amount: Decimal,
        description: str,
        arbiter_id: Optional[UUID] = None,
        timeout_hours: int = 48,
        platform_fee_percent: Decimal = Decimal('0.01')
    ) -> models.EscrowDeal:
        """Create new escrow deal"""
        # Calculate fees
        platform_fee_amount = amount * platform_fee_percent
        
        deal = models.EscrowDeal(
            deal_hash=deal_hash,
            buyer_id=buyer_id,
            seller_id=seller_id,
            arbiter_id=arbiter_id,
            amount=amount,
            description=description,
            timeout_hours=timeout_hours,
            platform_fee_percent=platform_fee_percent,
            platform_fee_amount=platform_fee_amount,
            status=models.EscrowStatus.PENDING,
            expires_at=datetime.utcnow() + timedelta(hours=timeout_hours)
        )
        
        self.db.add(deal)
        return deal
    
    def get_by_id(self, deal_id: UUID) -> Optional[models.EscrowDeal]:
        """Get deal by ID"""
        return self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id
        ).first()
    
    def get_by_hash(self, deal_hash: str) -> Optional[models.EscrowDeal]:
        """Get deal by hash"""
        return self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.deal_hash == deal_hash
        ).first()
    
    def list_by_agent(
        self,
        agent_id: UUID,
        role: Optional[str] = None,  # 'buyer', 'seller', 'arbiter', None for all
        status: Optional[str] = None,
        limit: int = 100
    ) -> List[models.EscrowDeal]:
        """List deals where agent participates"""
        query = self.db.query(models.EscrowDeal)
        
        if role == 'buyer':
            query = query.filter(models.EscrowDeal.buyer_id == agent_id)
        elif role == 'seller':
            query = query.filter(models.EscrowDeal.seller_id == agent_id)
        elif role == 'arbiter':
            query = query.filter(models.EscrowDeal.arbiter_id == agent_id)
        else:
            query = query.filter(
                (models.EscrowDeal.buyer_id == agent_id) |
                (models.EscrowDeal.seller_id == agent_id) |
                (models.EscrowDeal.arbiter_id == agent_id)
            )
        
        if status:
            query = query.filter(models.EscrowDeal.status == status)
        
        return query.order_by(desc(models.EscrowDeal.created_at)).limit(limit).all()
    
    def fund_deal(self, deal_id: UUID, deposit_tx_hash: str, multisig_address: str):
        """Mark deal as funded"""
        self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id
        ).update({
            "status": models.EscrowStatus.FUNDED,
            "deposit_tx_hash": deposit_tx_hash,
            "multisig_address": multisig_address,
            "funded_at": datetime.utcnow()
        })
    
    def mark_delivered(self, deal_id: UUID):
        """Mark deal as delivered"""
        self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id
        ).update({
            "status": models.EscrowStatus.DELIVERED
        })
    
    def release(self, deal_id: UUID, release_tx_hash: str):
        """Release funds to seller"""
        self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id
        ).update({
            "status": models.EscrowStatus.COMPLETED,
            "release_tx_hash": release_tx_hash,
            "completed_at": datetime.utcnow()
        })
    
    def open_dispute(self, deal_id: UUID, reason: str, opened_by: UUID):
        """Open dispute on deal"""
        self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id
        ).update({
            "status": models.EscrowStatus.DISPUTED,
            "disputed_at": datetime.utcnow(),
            "disputed_by": opened_by,
            "dispute_reason": reason
        })
    
    def arbitrate(self, deal_id: UUID, decision: str, arbiter_signature: str):
        """Arbiter makes decision"""
        updates = {
            "arbiter_decision": decision,
            "arbiter_signature": arbiter_signature
        }
        
        if decision == 'release':
            updates["status"] = models.EscrowStatus.COMPLETED
            updates["completed_at"] = datetime.utcnow()
        elif decision == 'refund':
            updates["status"] = models.EscrowStatus.REFUNDED
            updates["completed_at"] = datetime.utcnow()
        
        self.db.query(models.EscrowDeal).filter(
            models.EscrowDeal.id == deal_id
        ).update(updates)


# ═══════════════════════════════════════════════════════════════════════════════
# CHANNEL REPOSITORY
# ═══════════════════════════════════════════════════════════════════════════════

class ChannelRepository:
    """Payment channel data access"""
    
    def __init__(self, db: Session):
        self.db = db
    
    def create(
        self,
        channel_hash: str,
        agent_a_id: UUID,
        agent_b_id: UUID,
        capacity: Decimal,
        initial_state: Dict[str, Any]
    ) -> models.PaymentChannel:
        """Create new channel"""
        channel = models.PaymentChannel(
            channel_hash=channel_hash,
            agent_a_id=agent_a_id,
            agent_b_id=agent_b_id,
            capacity=capacity,
            status=models.ChannelStatus.PENDING,
            current_state=initial_state
        )
        
        self.db.add(channel)
        return channel
    
    def get_by_id(self, channel_id: UUID) -> Optional[models.PaymentChannel]:
        """Get channel by ID"""
        return self.db.query(models.PaymentChannel).filter(
            models.PaymentChannel.id == channel_id
        ).first()
    
    def get_by_hash(self, channel_hash: str) -> Optional[models.PaymentChannel]:
        """Get channel by hash"""
        return self.db.query(models.PaymentChannel).filter(
            models.PaymentChannel.channel_hash == channel_hash
        ).first()
    
    def list_by_agent(
        self,
        agent_id: UUID,
        status: Optional[str] = None,
        limit: int = 100
    ) -> List[models.PaymentChannel]:
        """List channels for agent"""
        query = self.db.query(models.PaymentChannel).filter(
            (models.PaymentChannel.agent_a_id == agent_id) |
            (models.PaymentChannel.agent_b_id == agent_id)
        )
        
        if status:
            query = query.filter(models.PaymentChannel.status == status)
        
        return query.order_by(desc(models.PaymentChannel.created_at)).limit(limit).all()
    
    def fund_channel(self, channel_id: UUID, funding_tx_hash: str, multisig_address: str):
        """Mark channel as funded"""
        self.db.query(models.PaymentChannel).filter(
            models.PaymentChannel.id == channel_id
        ).update({
            "status": models.ChannelStatus.OPEN,
            "funding_tx_hash": funding_tx_hash,
            "multisig_address": multisig_address,
            "funded_at": datetime.utcnow()
        })
    
    def update_state(
        self,
        channel_id: UUID,
        sequence_number: int,
        balance_a: Decimal,
        balance_b: Decimal,
        signature_a: Optional[str] = None,
        signature_b: Optional[str] = None
    ):
        """Update channel state"""
        # Update current state
        new_state = {
            "sequence_number": sequence_number,
            "balance_a": str(balance_a),
            "balance_b": str(balance_b),
            "signature_a": signature_a,
            "signature_b": signature_b,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        self.db.query(models.PaymentChannel).filter(
            models.PaymentChannel.id == channel_id
        ).update({
            "current_state": new_state
        })
        
        # Add to history
        import hashlib
        state_hash = hashlib.sha256(
            f"{sequence_number}:{balance_a}:{balance_b}".encode()
        ).hexdigest()
        
        state_record = models.ChannelState(
            channel_id=channel_id,
            sequence_number=sequence_number,
            balance_a=balance_a,
            balance_b=balance_b,
            signature_a=signature_a,
            signature_b=signature_b,
            state_hash=state_hash
        )
        self.db.add(state_record)
    
    def close_channel(self, channel_id: UUID, closing_tx_hash: str):
        """Mark channel as closed"""
        self.db.query(models.PaymentChannel).filter(
            models.PaymentChannel.id == channel_id
        ).update({
            "status": models.ChannelStatus.CLOSED,
            "closing_tx_hash": closing_tx_hash,
            "closed_at": datetime.utcnow()
        })


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK REPOSITORY
# ═══════════════════════════════════════════════════════════════════════════════

class WebhookRepository:
    """Webhook event data access"""
    
    def __init__(self, db: Session):
        self.db = db
    
    def create_event(
        self,
        agent_id: UUID,
        event_type: str,
        payload: Dict[str, Any]
    ) -> models.WebhookEvent:
        """Create new webhook event"""
        event = models.WebhookEvent(
            agent_id=agent_id,
            event_type=event_type,
            payload=payload,
            status=models.WebhookStatus.PENDING,
            attempt_count=0,
            max_attempts=5,
            next_attempt_at=datetime.utcnow()
        )
        self.db.add(event)
        return event
    
    def get_pending_events(self, limit: int = 100) -> List[models.WebhookEvent]:
        """Get events pending delivery"""
        return self.db.query(models.WebhookEvent).filter(
            models.WebhookEvent.status.in_([
                models.WebhookStatus.PENDING,
                models.WebhookStatus.RETRYING
            ]),
            models.WebhookEvent.next_attempt_at <= datetime.utcnow(),
            models.WebhookEvent.attempt_count < models.WebhookEvent.max_attempts
        ).order_by(models.WebhookEvent.created_at).limit(limit).all()
    
    def mark_delivered(self, event_id: UUID, response_code: int, response_body: str):
        """Mark event as delivered"""
        self.db.query(models.WebhookEvent).filter(
            models.WebhookEvent.id == event_id
        ).update({
            "status": models.WebhookStatus.DELIVERED,
            "last_response_code": response_code,
            "last_response_body": response_body[:1000],  # Truncate
            "delivered_at": datetime.utcnow()
        })
    
    def mark_failed(self, event_id: UUID, error: str):
        """Mark event as failed (max attempts reached)"""
        self.db.query(models.WebhookEvent).filter(
            models.WebhookEvent.id == event_id
        ).update({
            "status": models.WebhookStatus.FAILED,
            "last_error": error[:1000]
        })
    
    def schedule_retry(self, event_id: UUID, error: str):
        """Schedule retry with exponential backoff"""
        event = self.db.query(models.WebhookEvent).filter(
            models.WebhookEvent.id == event_id
        ).first()
        
        if event:
            attempt = event.attempt_count + 1
            # Exponential backoff: 1min, 5min, 15min, 30min, 1hour
            delays = [60, 300, 900, 1800, 3600]
            delay = delays[min(attempt - 1, len(delays) - 1)]
            
            self.db.query(models.WebhookEvent).filter(
                models.WebhookEvent.id == event_id
            ).update({
                "status": models.WebhookStatus.RETRYING,
                "attempt_count": attempt,
                "last_error": error[:1000] if error else None,
                "next_attempt_at": datetime.utcnow() + timedelta(seconds=delay)
            })


# ═══════════════════════════════════════════════════════════════════════════════
# REPUTATION REPOSITORY
# ═══════════════════════════════════════════════════════════════════════════════

class ReputationRepository:
    """Agent reputation data access"""
    
    def __init__(self, db: Session):
        self.db = db
    
    def get_by_agent(self, agent_id: UUID) -> Optional[models.AgentReputation]:
        """Get reputation for agent"""
        return self.db.query(models.AgentReputation).filter(
            models.AgentReputation.agent_id == agent_id
        ).first()
    
    def record_transaction(
        self,
        agent_id: UUID,
        success: bool = True,
        amount_usd: Decimal = Decimal('0')
    ):
        """Record transaction for reputation"""
        rep = self.get_by_agent(agent_id)
        if not rep:
            return
        
        rep.total_transactions += 1
        if success:
            rep.successful_transactions += 1
        else:
            rep.failed_transactions += 1
        
        rep.total_volume_usd += amount_usd
        rep.calculated_at = datetime.utcnow()
    
    def record_dispute(self, agent_id: UUID):
        """Record dispute for agent"""
        rep = self.get_by_agent(agent_id)
        if rep:
            rep.disputed_transactions += 1
    
    def get_leaderboard(self, limit: int = 100) -> List[models.AgentReputation]:
        """Get top agents by trust score"""
        return self.db.query(models.AgentReputation).join(
            models.Agent
        ).filter(
            models.Agent.is_active == True
        ).order_by(
            desc(models.AgentReputation.trust_score)
        ).limit(limit).all()


# ═══════════════════════════════════════════════════════════════════════════════
# BALANCE REPOSITORY
# ═══════════════════════════════════════════════════════════════════════════════

class BalanceRepository:
    def __init__(self, db):
        self.db = db

    def get_or_create(self, agent_id, token="XMR"):
        """Get balance record, create if not exists"""
        balance = self.db.query(AgentBalance).filter(
            AgentBalance.agent_id == agent_id,
            AgentBalance.token == token
        ).first()
        if not balance:
            balance = AgentBalance(agent_id=agent_id, token=token)
            self.db.add(balance)
            self.db.flush()
        return balance

    def get_available(self, agent_id, token="XMR"):
        """Get available balance"""
        balance = self.get_or_create(agent_id, token)
        return balance.available or Decimal("0")

    def deposit(self, agent_id, amount, token="XMR"):
        """Credit agent balance after deposit confirmed"""
        balance = self.get_or_create(agent_id, token)
        balance.available = (balance.available or Decimal("0")) + amount
        balance.total_deposited = (balance.total_deposited or Decimal("0")) + amount
        balance.updated_at = datetime.utcnow()
        return balance

    def deduct(self, agent_id, amount, token="XMR"):
        """Deduct from available balance (for hub routing)"""
        balance = self.get_or_create(agent_id, token)
        if (balance.available or Decimal("0")) < amount:
            raise ValueError(f"Insufficient balance: {balance.available} < {amount}")
        balance.available = balance.available - amount
        balance.updated_at = datetime.utcnow()
        return balance

    def credit(self, agent_id, amount, token="XMR"):
        """Credit to available balance (receiving hub payment)"""
        balance = self.get_or_create(agent_id, token)
        balance.available = (balance.available or Decimal("0")) + amount
        balance.updated_at = datetime.utcnow()
        return balance

    def set_deposit_address(self, agent_id, address, token="XMR"):
        """Set the deposit subaddress for an agent"""
        balance = self.get_or_create(agent_id, token)
        balance.deposit_address = address
        return balance
