"""
Payment Channels for instant, off-chain micropayments between agents.

How it works:
1. Agents open a channel by locking funds in 2-of-2 multisig (1 on-chain tx)
2. They exchange signed "commitment transactions" off-chain instantly
3. When done, they close channel with final state (1 on-chain tx)

Benefits:
- Thousands of payments per second
- Zero fees for off-chain payments
- Instant settlement
- Privacy (only opening/closing visible on blockchain)
"""

import json
import hashlib
import time
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from decimal import Decimal


class ChannelStatus(Enum):
    """Payment channel status"""
    PENDING = "pending"       # Channel proposed
    OPEN = "open"             # Funds locked, ready for payments
    CLOSING = "closing"       # Close initiated
    CLOSED = "closed"         # Settled on-chain
    DISPUTED = "disputed"     # Force close in progress


@dataclass
class ChannelState:
    """Current state of payment channel"""
    sequence_number: int      # Increment with each payment
    balance_a: float          # Agent A balance (XMR)
    balance_b: float          # Agent B balance (XMR)
    signature_a: Optional[str] = None
    signature_b: Optional[str] = None
    timestamp: float = 0.0
    
    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()
    
    @property
    def total(self) -> float:
        """Total locked in channel"""
        return self.balance_a + self.balance_b
    
    def to_bytes(self) -> bytes:
        """Serialize for signing"""
        data = f"{self.sequence_number}:{self.balance_a}:{self.balance_b}:{self.timestamp}"
        return data.encode()
    
    def hash(self) -> str:
        """Get state hash"""
        return hashlib.sha256(self.to_bytes()).hexdigest()


@dataclass
class PaymentChannel:
    """Payment channel between two agents"""
    id: str
    
    # Participants
    agent_a_address: str      # Channel opener (funder)
    agent_b_address: str      # Counterparty
    
    # Channel params
    capacity: float           # Total XMR locked
    status: ChannelStatus
    
    # On-chain
    funding_tx_hash: Optional[str] = None
    closing_tx_hash: Optional[str] = None
    multisig_address: Optional[str] = None
    
    # Off-chain state
    current_state: Optional[ChannelState] = None
    states_history: List[ChannelState] = None
    
    # Timing
    created_at: datetime = None
    expires_at: datetime = None  # Force close available after
    
    def __post_init__(self):
        if self.states_history is None:
            self.states_history = []
        if self.created_at is None:
            self.created_at = datetime.utcnow()
    
    @property
    def can_close(self) -> bool:
        """Can initiate cooperative close"""
        return self.status == ChannelStatus.OPEN
    
    @property
    def can_force_close(self) -> bool:
        """Can force close if counterparty unresponsive"""
        if self.status != ChannelStatus.OPEN:
            return False
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at


class ChannelManager:
    """
    Manages payment channels for high-frequency micropayments.
    
    Simplified implementation - real Monero channels would need:
    - Full multisig coordination
    - Commitment transaction structure
    - Watchtowers for safety
    """
    
    def __init__(self):
        self._channels: Dict[str, PaymentChannel] = {}
        self._pending_payments: Dict[str, Dict] = {}  # Pending HTLCs
    
    def propose_channel(
        self,
        my_address: str,
        counterparty_address: str,
        my_capacity: float,
        their_capacity: float = 0.0
    ) -> PaymentChannel:
        """
        Propose new payment channel.
        
        Args:
            my_capacity: How much YOU lock in channel
            their_capacity: How much COUNTERPARTY locks (usually 0 for simple channel)
        """
        total = my_capacity + their_capacity
        
        channel_id = hashlib.sha256(
            f"{my_address}:{counterparty_address}:{total}:{time.time()}".encode()
        ).hexdigest()[:16]
        
        channel = PaymentChannel(
            id=channel_id,
            agent_a_address=my_address,
            agent_b_address=counterparty_address,
            capacity=total,
            status=ChannelStatus.PENDING,
            current_state=ChannelState(
                sequence_number=0,
                balance_a=my_capacity,
                balance_b=their_capacity
            )
        )
        
        self._channels[channel_id] = channel
        return channel
    
    def accept_channel(self, channel_id: str) -> PaymentChannel:
        """Counterparty accepts channel proposal"""
        channel = self._get_channel(channel_id)
        
        if channel.status != ChannelStatus.PENDING:
            raise ValueError("Channel not pending")
        
        # In real implementation: exchange multisig pubkeys, create funding tx
        channel.status = ChannelStatus.OPEN
        return channel
    
    def fund_channel(self, channel_id: str, funding_tx_hash: str) -> PaymentChannel:
        """Mark channel as funded (funding tx confirmed)"""
        channel = self._get_channel(channel_id)
        channel.funding_tx_hash = funding_tx_hash
        channel.status = ChannelStatus.OPEN
        return channel
    
    def pay(
        self,
        channel_id: str,
        amount: float,
        direction: str  # "a_to_b" or "b_to_a"
    ) -> ChannelState:
        """
        Make off-chain payment in channel.
        
        This creates new channel state and exchanges signatures.
        Instant and free!
        """
        channel = self._get_channel(channel_id)
        
        if channel.status != ChannelStatus.OPEN:
            raise ValueError("Channel not open")
        
        if amount <= 0:
            raise ValueError("Amount must be positive")
        
        old_state = channel.current_state
        
        # Create new state
        if direction == "a_to_b":
            if old_state.balance_a < amount:
                raise ValueError("Insufficient balance")
            new_state = ChannelState(
                sequence_number=old_state.sequence_number + 1,
                balance_a=old_state.balance_a - amount,
                balance_b=old_state.balance_b + amount
            )
        elif direction == "b_to_a":
            if old_state.balance_b < amount:
                raise ValueError("Insufficient balance")
            new_state = ChannelState(
                sequence_number=old_state.sequence_number + 1,
                balance_a=old_state.balance_a + amount,
                balance_b=old_state.balance_b - amount
            )
        else:
            raise ValueError("Invalid direction")
        
        # In real implementation:
        # 1. Create commitment transaction for new state
        # 2. Exchange signatures with counterparty
        # 3. Both parties hold signed state
        
        # For now, simulate signatures
        new_state.signature_a = f"sig_a_{new_state.hash()[:16]}"
        new_state.signature_b = f"sig_b_{new_state.hash()[:16]}"
        
        # Update channel
        channel.states_history.append(old_state)
        channel.current_state = new_state
        
        return new_state
    
    def close_cooperative(
        self,
        channel_id: str,
        my_signature: str
    ) -> PaymentChannel:
        """
        Cooperative close - both parties agree on final state.
        Creates closing transaction distributing funds as per current state.
        """
        channel = self._get_channel(channel_id)
        
        if channel.status != ChannelStatus.OPEN:
            raise ValueError("Channel not open")
        
        channel.status = ChannelStatus.CLOSING
        
        # In real implementation:
        # 1. Create closing tx with current balances
        # 2. Both parties sign
        # 3. Broadcast to blockchain
        
        return channel
    
    def close_force(
        self,
        channel_id: str,
        my_signature: str
    ) -> PaymentChannel:
        """
        Force close - counterparty unresponsive.
        Publish latest state to blockchain.
        """
        channel = self._get_channel(channel_id)
        
        if not channel.can_force_close:
            raise ValueError("Cannot force close yet")
        
        channel.status = ChannelStatus.DISPUTED
        
        # In real implementation:
        # 1. Publish latest signed state to blockchain
        # 2. Wait for challenge period
        # 3. If no challenge, funds distributed
        
        return channel
    
    def finalize_close(self, channel_id: str, closing_tx_hash: str) -> PaymentChannel:
        """Mark channel as closed after settlement"""
        channel = self._get_channel(channel_id)
        channel.closing_tx_hash = closing_tx_hash
        channel.status = ChannelStatus.CLOSED
        return channel
    
    def get_channel(self, channel_id: str) -> Optional[PaymentChannel]:
        """Get channel by ID"""
        return self._channels.get(channel_id)
    
    def list_channels(
        self,
        my_address: str,
        status: Optional[ChannelStatus] = None
    ) -> List[PaymentChannel]:
        """List channels where I'm a participant"""
        channels = [
            c for c in self._channels.values()
            if c.agent_a_address == my_address or c.agent_b_address == my_address
        ]
        
        if status:
            channels = [c for c in channels if c.status == status]
        
        return channels
    
    def get_balance(self, channel_id: str, my_address: str) -> float:
        """Get my current balance in channel"""
        channel = self._get_channel(channel_id)
        state = channel.current_state
        
        if channel.agent_a_address == my_address:
            return state.balance_a
        elif channel.agent_b_address == my_address:
            return state.balance_b
        else:
            raise ValueError("Not a participant")
    
    def _get_channel(self, channel_id: str) -> PaymentChannel:
        """Internal: get channel or raise"""
        if channel_id not in self._channels:
            raise ValueError(f"Channel {channel_id} not found")
        return self._channels[channel_id]


# HTLC support for multi-hop payments (simplified)

@dataclass
class HTLC:
    """Hash Time Locked Contract for atomic swaps/multi-hop"""
    hash_lock: str          # SHA256 hash of secret
    timeout: float          # Unix timestamp
    amount: float
    sender: str
    receiver: str
    secret: Optional[str] = None  # Revealed to claim
    
    def claim(self, secret: str) -> bool:
        """Claim HTLC by revealing secret"""
        if hashlib.sha256(secret.encode()).hexdigest() == self.hash_lock:
            self.secret = secret
            return True
        return False
    
    def is_expired(self) -> bool:
        """Check if timelock expired"""
        return time.time() > self.timeout
