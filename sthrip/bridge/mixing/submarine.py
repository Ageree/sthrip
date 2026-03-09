"""
Submarine Swaps

Атомарные свопы между on-chain и off-chain (Lightning Network)
для дополнительной анонимизации.
"""

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional, Callable
from enum import Enum


class SwapState(Enum):
    """States of submarine swap"""
    INITIATED = "initiated"
    INVOICE_CREATED = "invoice_created"
    CONTRACT_FUNDED = "contract_funded"
    PREIMAGE_REVEALED = "preimage_revealed"
    COMPLETED = "completed"
    REFUNDED = "refunded"
    FAILED = "failed"


@dataclass
class SubmarineSwap:
    """
    Submarine swap parameters
    
    Allows swapping on-chain for off-chain and vice versa
    without trusting counterparty.
    """
    id: str
    direction: str  # 'in' (on->off) or 'out' (off->on)
    
    # On-chain parameters
    onchain_amount: int
    onchain_address: str
    
    # Off-chain parameters
    offchain_amount: int
    payment_hash: bytes
    
    # HTLC parameters
    preimage: Optional[bytes] = None
    hashlock: Optional[bytes] = None
    timeout_blocks: int = 144  # ~24 hours
    
    # State
    state: SwapState = SwapState.INITIATED
    created_at: float = 0
    
    # Callbacks
    on_success: Optional[Callable] = None
    on_fail: Optional[Callable] = None


class SubmarineSwapService:
    """
    Submarine swap service
    
    Provides liquidity for atomic swaps between:
    - On-chain BTC <-> Lightning BTC
    - On-chain ETH <-> Lightning BTC (via wrapped)
    
    Privacy benefits:
    - Breaks chain analysis
    - Uses Lightning for fast, private payments
    - No KYC required
    """
    
    def __init__(
        self,
        lightning_node_url: str,
        onchain_rpc_url: str,
        min_amount: int = 100000,  # 0.001 BTC
        max_amount: int = 100000000  # 1 BTC
    ):
        self.lightning_url = lightning_node_url
        self.onchain_rpc = onchain_rpc_url
        self.min_amount = min_amount
        self.max_amount = max_amount
        
        self.active_swaps: dict = {}
        self.fee_percent = 0.005  # 0.5%
    
    async def create_swap_in(
        self,
        invoice_amount: int,
        refund_address: str
    ) -> SubmarineSwap:
        """
        Create swap: on-chain -> Lightning
        
        User sends on-chain, receives Lightning payment.
        
        Args:
            invoice_amount: Amount to receive on Lightning
            refund_address: Address for refund if fails
            
        Returns:
            SubmarineSwap with funding address
        """
        # Validate amount
        if not self.min_amount <= invoice_amount <= self.max_amount:
            raise ValueError("Amount out of range")
        
        # Calculate on-chain amount (includes fees)
        onchain_amount = int(invoice_amount * (1 + self.fee_percent))
        
        # Generate swap ID
        swap_id = secrets.token_hex(16)
        
        # Generate preimage and hashlock
        preimage = secrets.token_bytes(32)
        hashlock = hashlib.sha256(preimage).digest()
        
        # Create swap
        swap = SubmarineSwap(
            id=swap_id,
            direction='in',
            onchain_amount=onchain_amount,
            onchain_address=refund_address,  # Will be replaced with contract
            offchain_amount=invoice_amount,
            payment_hash=hashlock,
            preimage=preimage,
            hashlock=hashlock,
            created_at=asyncio.get_event_loop().time()
        )
        
        self.active_swaps[swap_id] = swap
        
        # Create HTLC contract address
        swap.onchain_address = await self._create_htlc_contract(swap)
        
        # Start monitoring
        asyncio.create_task(self._monitor_swap_in(swap))
        
        return swap
    
    async def create_swap_out(
        self,
        onchain_address: str,
        onchain_amount: int
    ) -> SubmarineSwap:
        """
        Create swap: Lightning -> on-chain
        
        User pays Lightning invoice, receives on-chain.
        
        Args:
            onchain_address: Address to receive funds
            onchain_amount: Amount to receive on-chain
            
        Returns:
            SubmarineSwap with Lightning invoice
        """
        # Validate amount
        if not self.min_amount <= onchain_amount <= self.max_amount:
            raise ValueError("Amount out of range")
        
        # Calculate Lightning amount (deduct fees)
        offchain_amount = int(onchain_amount * (1 - self.fee_percent))
        
        # Generate swap ID
        swap_id = secrets.token_hex(16)
        
        # Generate hashlock (preimage known only to us initially)
        preimage = secrets.token_bytes(32)
        hashlock = hashlib.sha256(preimage).digest()
        
        # Create swap
        swap = SubmarineSwap(
            id=swap_id,
            direction='out',
            onchain_amount=onchain_amount,
            onchain_address=onchain_address,
            offchain_amount=offchain_amount,
            payment_hash=hashlock,
            preimage=preimage,
            hashlock=hashlock,
            created_at=asyncio.get_event_loop().time()
        )
        
        self.active_swaps[swap_id] = swap
        
        # Create Lightning invoice
        invoice = await self._create_lightning_invoice(swap)
        
        # Start monitoring
        asyncio.create_task(self._monitor_swap_out(swap))
        
        return swap
    
    async def _create_htlc_contract(self, swap: SubmarineSwap) -> str:
        """
        Create HTLC contract for swap
        
        In real impl: deploy Bitcoin HTLC or use existing service
        like Boltz or Loop.
        """
        # Simplified: return address
        return f"2N{swap.id[:30]}"
    
    async def _create_lightning_invoice(self, swap: SubmarineSwap) -> str:
        """Create Lightning invoice"""
        # In real impl: call lightning node API
        return f"lnbc{swap.offchain_amount}n1p{swap.payment_hash.hex()[:50]}"
    
    async def _monitor_swap_in(self, swap: SubmarineSwap):
        """
        Monitor swap in (on-chain -> Lightning)
        
        Steps:
        1. Wait for on-chain funding
        2. Pay Lightning invoice
        3. Reveal preimage to claim on-chain
        """
        try:
            # Wait for on-chain funding
            funded = await self._wait_for_funding(swap)
            if not funded:
                swap.state = SwapState.FAILED
                return
            
            swap.state = SwapState.CONTRACT_FUNDED
            
            # Pay Lightning invoice
            paid = await self._pay_lightning_invoice(swap)
            if paid:
                # Reveal preimage
                await self._claim_onchain(swap)
                swap.state = SwapState.COMPLETED
            else:
                # Refund user
                await self._refund_user(swap)
                swap.state = SwapState.REFUNDED
                
        except Exception as e:
            swap.state = SwapState.FAILED
            if swap.on_fail:
                await swap.on_fail(swap, e)
    
    async def _monitor_swap_out(self, swap: SubmarineSwap):
        """
        Monitor swap out (Lightning -> on-chain)
        
        Steps:
        1. Wait for Lightning payment
        2. Broadcast on-chain transaction with preimage
        """
        try:
            # Wait for Lightning payment
            received = await self._wait_for_lightning_payment(swap)
            if not received:
                swap.state = SwapState.FAILED
                return
            
            swap.state = SwapState.PREIMAGE_REVEALED
            
            # Broadcast on-chain tx
            broadcasted = await self._broadcast_onchain(swap)
            if broadcasted:
                swap.state = SwapState.COMPLETED
            else:
                swap.state = SwapState.FAILED
                
        except Exception as e:
            swap.state = SwapState.FAILED
            if swap.on_fail:
                await swap.on_fail(swap, e)
    
    async def _wait_for_funding(self, swap: SubmarineSwap, timeout: int = 3600) -> bool:
        """Wait for on-chain funding"""
        # In real impl: check blockchain
        await asyncio.sleep(1)
        return True
    
    async def _pay_lightning_invoice(self, swap: SubmarineSwap) -> bool:
        """Pay Lightning invoice"""
        # In real impl: call lightning node
        await asyncio.sleep(0.5)
        return True
    
    async def _claim_onchain(self, swap: SubmarineSwap) -> bool:
        """Claim on-chain funds with preimage"""
        # In real impl: broadcast claim tx
        await asyncio.sleep(0.5)
        return True
    
    async def _refund_user(self, swap: SubmarineSwap) -> bool:
        """Refund user after timeout"""
        # In real impl: wait for timeout, broadcast refund
        await asyncio.sleep(0.5)
        return True
    
    async def _wait_for_lightning_payment(self, swap: SubmarineSwap) -> bool:
        """Wait for Lightning payment"""
        # In real impl: subscribe to invoice events
        await asyncio.sleep(1)
        return True
    
    async def _broadcast_onchain(self, swap: SubmarineSwap) -> bool:
        """Broadcast on-chain transaction"""
        # In real impl: broadcast tx with preimage
        await asyncio.sleep(0.5)
        return True
    
    def get_swap_status(self, swap_id: str) -> Optional[dict]:
        """Get swap status"""
        swap = self.active_swaps.get(swap_id)
        if not swap:
            return None
        
        return {
            'id': swap.id,
            'direction': swap.direction,
            'state': swap.state.value,
            'onchain_amount': swap.onchain_amount,
            'offchain_amount': swap.offchain_amount,
            'payment_hash': swap.payment_hash.hex(),
            'created_at': swap.created_at,
            'funding_address': swap.onchain_address if swap.direction == 'in' else None,
            'invoice': swap.invoice if swap.direction == 'out' else None
        }


class LoopOutService(SubmarineSwapService):
    """
    Lightning Loop Out service
    
    Specialized service for moving funds from
    Lightning to on-chain without closing channels.
    """
    
    async def loop_out(
        self,
        channel_id: str,
        amount: int,
        target_conf: int = 6
    ) -> SubmarineSwap:
        """
        Loop out from channel
        
        Args:
            channel_id: Channel to loop out from
            amount: Amount to loop out
            target_conf: Target confirmations
            
        Returns:
            SubmarineSwap
        """
        # Generate on-chain address
        address = await self._generate_address()
        
        # Create swap
        swap = await self.create_swap_out(address, amount)
        
        # Create route hint for payment
        # This ensures payment comes from specific channel
        
        return swap
    
    async def _generate_address(self) -> str:
        """Generate new on-chain address"""
        # In real impl: get address from wallet
        return f"bc1q{secrets.token_hex(20)}"


class PrivacySwapAggregator:
    """
    Aggregates multiple privacy techniques
    
    Combines:
    - Submarine swaps
    - CoinJoin
    - Time delays
    - Stealth addresses
    """
    
    def __init__(
        self,
        submarine_service: SubmarineSwapService,
        coinjoin_coordinator: 'CoinJoinCoordinator',
        tumbler: 'Tumbler'
    ):
        self.submarine = submarine_service
        self.coinjoin = coinjoin_coordinator
        self.tumbler = tumbler
    
    async def private_send(
        self,
        amount: int,
        destination: str,
        privacy_level: str = "high"
    ) -> dict:
        """
        Send with maximum privacy
        
        Privacy levels:
        - basic: Single submarine swap
        - medium: CoinJoin + submarine
        - high: Tumbler + CoinJoin + submarine + stealth
        
        Args:
            amount: Amount to send
            destination: Final destination
            privacy_level: Privacy level
            
        Returns:
            Transaction details
        """
        if privacy_level == "basic":
            # Simple submarine swap
            swap = await self.submarine.create_swap_in(
                amount, destination
            )
            return {'type': 'submarine', 'swap': swap.id}
        
        elif privacy_level == "medium":
            # CoinJoin then submarine
            # First do CoinJoin
            cj_address = await self._get_cj_address()
            # ... CoinJoin logic
            
            # Then submarine swap
            swap = await self.submarine.create_swap_in(
                amount, destination
            )
            return {'type': 'coinjoin+submarine', 'swap': swap.id}
        
        else:  # high
            # Full pipeline: Tumbler -> CoinJoin -> Submarine -> Stealth
            
            # 1. Submit to tumbler
            tumble_job = await self.tumbler.submit(
                amount, destination
            )
            
            # 2. Each tumble part goes through CoinJoin
            # 3. Then submarine swap
            # 4. Final delivery to stealth address
            
            return {
                'type': 'full_privacy',
                'tumble_job': tumble_job.id,
                'status': 'processing'
            }
    
    async def _get_cj_address(self) -> str:
        """Get address for CoinJoin"""
        # In real impl: generate new address
        return f"bc1q{secrets.token_hex(20)}"
