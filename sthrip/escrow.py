"""
Multi-signature escrow for anonymous agent-to-agent deals.
2-of-3 multisig: Buyer + Seller + Arbiter

How it works:
1. Buyer, Seller, Arbiter exchange public keys (offline or via secure channel)
2. They create 2-of-3 multisig wallet together
3. Buyer deposits funds into multisig address
4. If deal succeeds: Buyer + Seller sign → funds to Seller
5. If dispute: Arbiter decides + one party signs
6. If timeout: Automatic refund to Buyer
"""

import time
import hashlib
from typing import List, Dict, Optional, Tuple, Literal
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from .wallet import MoneroWalletRPC
from .types import PaymentStatus


class EscrowStatus(Enum):
    """Escrow deal status"""
    PENDING = "pending"           # Waiting for deposit
    FUNDED = "funded"             # Deposit received
    DELIVERED = "delivered"       # Seller claims delivery
    COMPLETED = "completed"       # Released to seller
    DISPUTED = "disputed"         # Under arbitration
    REFUNDED = "refunded"         # Returned to buyer
    EXPIRED = "expired"           # Auto-refund after timeout


class EscrowAction(Enum):
    """Actions that can be taken on escrow"""
    FUND = "fund"                 # Buyer deposits
    MARK_DELIVERED = "deliver"    # Seller marks as done
    RELEASE = "release"           # Buyer confirms + releases
    DISPUTE = "dispute"           # Open dispute
    ARBITRATE = "arbitrate"       # Arbiter decides
    REFUND = "refund"             # Return to buyer


@dataclass
class EscrowParticipant:
    """Participant in escrow deal"""
    role: Literal["buyer", "seller", "arbiter"]
    address: str          # Monero address (public key)
    signature: Optional[str] = None  # Signature for current action


@dataclass
class EscrowDeal:
    """Escrow deal between agents"""
    id: str
    status: EscrowStatus
    
    # Participants
    buyer: EscrowParticipant
    seller: EscrowParticipant
    arbiter: EscrowParticipant
    
    # Deal terms
    amount: float          # XMR amount
    description: str       # What is being bought
    
    # Timing
    created_at: datetime
    timeout_hours: int = 48  # Auto-refund after this
    funded_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    # Multisig
    multisig_address: Optional[str] = None
    deposit_tx_hash: Optional[str] = None
    release_tx_hash: Optional[str] = None
    
    # Dispute
    disputed_by: Optional[str] = None
    dispute_reason: Optional[str] = None
    arbiter_decision: Optional[Literal["release", "refund"]] = None
    
    @property
    def is_expired(self) -> bool:
        """Check if escrow should auto-expire"""
        if self.status not in [EscrowStatus.PENDING, EscrowStatus.FUNDED]:
            return False
        deadline = self.created_at + timedelta(hours=self.timeout_hours)
        return datetime.now(timezone.utc) > deadline
    
    @property
    def time_remaining(self) -> timedelta:
        """Time until auto-expiry"""
        if self.status not in [EscrowStatus.PENDING, EscrowStatus.FUNDED]:
            return timedelta(0)
        deadline = self.created_at + timedelta(hours=self.timeout_hours)
        remaining = deadline - datetime.now(timezone.utc)
        return max(remaining, timedelta(0))
    
    def can_release(self) -> bool:
        """Can funds be released to seller?"""
        return self.status in [EscrowStatus.FUNDED, EscrowStatus.DELIVERED]
    
    def can_refund(self) -> bool:
        """Can funds be refunded to buyer?"""
        return self.status in [EscrowStatus.PENDING, EscrowStatus.FUNDED, EscrowStatus.DISPUTED]


class EscrowManager:
    """
    Manages 2-of-3 multisig escrows for agent deals.
    
    IMPORTANT: This is a simplified version. Real implementation requires
    full multisig coordination protocol (exchange of multisig info, signers, etc.)
    """
    
    def __init__(self, wallet_rpc: MoneroWalletRPC):
        self.wallet = wallet_rpc
        self._deals: Dict[str, EscrowDeal] = {}
    
    def create_deal(
        self,
        buyer_address: str,
        seller_address: str,
        arbiter_address: str,
        amount: float,
        description: str,
        timeout_hours: int = 48
    ) -> EscrowDeal:
        """
        Create new escrow deal.
        
        This generates a unique deal ID and sets up the structure.
        In production, you'd also need to set up the actual multisig wallet.
        """
        # Generate unique deal ID
        deal_id = hashlib.sha256(
            f"{buyer_address}:{seller_address}:{amount}:{time.time()}".encode()
        ).hexdigest()[:16]
        
        deal = EscrowDeal(
            id=deal_id,
            status=EscrowStatus.PENDING,
            buyer=EscrowParticipant(role="buyer", address=buyer_address),
            seller=EscrowParticipant(role="seller", address=seller_address),
            arbiter=EscrowParticipant(role="arbiter", address=arbiter_address),
            amount=amount,
            description=description,
            created_at=datetime.now(timezone.utc),
            timeout_hours=timeout_hours
        )
        
        self._deals[deal_id] = deal
        return deal
    
    def fund_deal(self, deal_id: str, tx_hash: str, multisig_address: str) -> EscrowDeal:
        """
        Mark deal as funded when buyer deposits to multisig address.
        In real implementation, you'd verify the transaction on-chain.
        """
        if deal_id not in self._deals:
            raise ValueError(f"Deal {deal_id} not found")
        
        deal = self._deals[deal_id]
        
        if deal.status != EscrowStatus.PENDING:
            raise ValueError(f"Cannot fund deal in status {deal.status}")
        
        deal.status = EscrowStatus.FUNDED
        deal.multisig_address = multisig_address
        deal.deposit_tx_hash = tx_hash
        deal.funded_at = datetime.now(timezone.utc)
        
        return deal
    
    def mark_delivered(self, deal_id: str) -> EscrowDeal:
        """Seller marks work as delivered"""
        deal = self._get_deal(deal_id)
        
        if deal.status != EscrowStatus.FUNDED:
            raise ValueError(f"Cannot mark delivered in status {deal.status}")
        
        deal.status = EscrowStatus.DELIVERED
        return deal
    
    def release(self, deal_id: str, buyer_signature: str) -> EscrowDeal:
        """
        Buyer releases funds to seller.
        Requires 2 signatures in real implementation (buyer + seller, or buyer + arbiter)
        """
        deal = self._get_deal(deal_id)
        
        if not deal.can_release():
            raise ValueError(f"Cannot release in status {deal.status}")
        
        deal.buyer.signature = buyer_signature
        deal.status = EscrowStatus.COMPLETED
        deal.completed_at = datetime.now(timezone.utc)
        
        # In real implementation: create and broadcast release transaction
        
        return deal
    
    def open_dispute(self, deal_id: str, reason: str, opened_by: str) -> EscrowDeal:
        """Open dispute (buyer or seller can do this)"""
        deal = self._get_deal(deal_id)
        
        if deal.status not in [EscrowStatus.FUNDED, EscrowStatus.DELIVERED]:
            raise ValueError(f"Cannot dispute in status {deal.status}")
        
        deal.status = EscrowStatus.DISPUTED
        deal.disputed_by = opened_by
        deal.dispute_reason = reason
        
        return deal
    
    def arbitrate(
        self,
        deal_id: str,
        decision: Literal["release", "refund"],
        arbiter_signature: str
    ) -> EscrowDeal:
        """
        Arbiter makes decision on disputed deal.
        Decision + one party signature = execution.
        """
        deal = self._get_deal(deal_id)
        
        if deal.status != EscrowStatus.DISPUTED:
            raise ValueError(f"Cannot arbitrate in status {deal.status}")
        
        deal.arbiter_decision = decision
        deal.arbiter.signature = arbiter_signature
        
        if decision == "release":
            deal.status = EscrowStatus.COMPLETED
            deal.completed_at = datetime.now(timezone.utc)
        else:  # refund
            deal.status = EscrowStatus.REFUNDED
        
        return deal
    
    def request_refund(self, deal_id: str) -> EscrowDeal:
        """
        Buyer requests refund (if deal expired or seller agrees).
        In 2-of-3: buyer + arbiter can refund without seller.
        """
        deal = self._get_deal(deal_id)
        
        if deal.is_expired:
            # Auto-refund on expiry
            deal.status = EscrowStatus.EXPIRED
            return deal
        
        if not deal.can_refund():
            raise ValueError(f"Cannot refund in status {deal.status}")
        
        # Requires arbiter signature in disputed state
        # Or seller cooperation in normal state
        
        return deal
    
    def get_deal(self, deal_id: str) -> Optional[EscrowDeal]:
        """Get deal by ID"""
        return self._deals.get(deal_id)
    
    def list_deals(
        self,
        address: Optional[str] = None,
        status: Optional[EscrowStatus] = None
    ) -> List[EscrowDeal]:
        """List all deals, optionally filtered"""
        deals = list(self._deals.values())
        
        if address:
            deals = [
                d for d in deals
                if d.buyer.address == address 
                or d.seller.address == address
                or d.arbiter.address == address
            ]
        
        if status:
            deals = [d for d in deals if d.status == status]
        
        return deals
    
    def _get_deal(self, deal_id: str) -> EscrowDeal:
        """Internal: get deal or raise"""
        if deal_id not in self._deals:
            raise ValueError(f"Deal {deal_id} not found")
        return self._deals[deal_id]
    
    # === Multisig Wallet Setup (Simplified) ===
    
    def prepare_multisig_info(self) -> Dict:
        """
        Prepare multisig wallet info.
        In real implementation, this coordinates with monero-wallet-rpc multisig commands.
        """
        # Real flow would be:
        # 1. Each party: make_multisig("2", [other_pubkey1, other_pubkey2])
        # 2. Exchange multisig_info strings
        # 3. Each party: exchange_multisig_keys(multisig_info_others)
        # 4. Finalize wallet
        
        return {
            "threshold": 2,
            "total_signers": 3,
            "note": "In production, use monero-wallet-rpc multisig commands"
        }


# Convenience functions for client integration

def create_escrow_address(buyer_key: str, seller_key: str, arbiter_key: str) -> str:
    """
    Create 2-of-3 multisig address from three public keys.
    This is a placeholder - real implementation uses Monero multisig.
    """
    # Monero multisig address derivation is complex
    # It requires exchanging and combining multisig info
    combined = hashlib.sha256(
        f"2/3:{buyer_key}:{seller_key}:{arbiter_key}".encode()
    ).hexdigest()
    return f"4M{combined[:94]}"  # Fake multisig format
