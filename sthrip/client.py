"""
Main Sthrip client for AI Agents
"""

import random
import time
from typing import Optional, List, Dict, Callable
from datetime import datetime, timezone
from decimal import Decimal

from .types import Payment, PaymentStatus, WalletInfo, StealthAddress
from .wallet import MoneroWalletRPC, WalletRPCError
from .stealth import StealthAddressManager
from .escrow import EscrowManager, EscrowDeal, EscrowStatus
from .channels import ChannelManager, PaymentChannel, ChannelState, ChannelStatus
from .privacy import PrivacyConfig, PrivacyEnhancer, TransactionScheduler


class Sthrip:
    """
    Anonymous payment client for AI Agents via Monero.
    
    Features:
    - Zero-knowledge payments (sender, receiver, amount hidden)
    - One-time stealth addresses for each transaction
    - No KYC, no registration, no identity
    - Perfect for agent-to-agent payments
    
    Example:
        >>> from sthrip import Sthrip
        >>> 
        >>> # Connect to your Monero wallet
        >>> agent = Sthrip(
        ...     rpc_host="127.0.0.1",
        ...     rpc_port=18082,
        ...     rpc_user="agent",
        ...     rpc_pass="secret"
        ... )
        >>> 
        >>> # Check balance
        >>> info = agent.get_info()
        >>> print(f"Balance: {info.balance} XMR")
        >>> 
        >>> # Create stealth address for receiving payment
        >>> stealth = agent.create_stealth_address(purpose="api-payment")
        >>> print(f"Pay me here: {stealth.address}")
        >>> 
        >>> # Send anonymous payment
        >>> tx = agent.pay(
        ...     to_address="44...",
        ...     amount=0.1,
        ...     memo="Data purchase"
        ... )
    """
    
    def __init__(
        self,
        rpc_host: str = "127.0.0.1",
        rpc_port: int = 18082,
        rpc_user: Optional[str] = None,
        rpc_pass: Optional[str] = None,
        account_index: int = 0
    ):
        """
        Initialize Sthrip client.
        
        Requires running monero-wallet-rpc:
        monero-wallet-rpc --wallet-file agent_wallet --rpc-bind-port 18082
        
        Args:
            rpc_host: Wallet RPC host
            rpc_port: Wallet RPC port
            rpc_user: RPC username (optional)
            rpc_pass: RPC password (optional)
            account_index: Account index in wallet
        """
        self.wallet = MoneroWalletRPC(
            host=rpc_host,
            port=rpc_port,
            user=rpc_user,
            password=rpc_pass
        )
        self.account_index = account_index
        self.stealth = StealthAddressManager(self.wallet, account_index)
        self.escrow = EscrowManager(self.wallet)
        self.channels = ChannelManager()
        
        # Privacy enhancements
        self.privacy = PrivacyEnhancer()
        self.scheduler = TransactionScheduler()
        
        # Verify connection
        try:
            self.wallet.get_height()
        except WalletRPCError as e:
            raise ConnectionError(
                f"Cannot connect to Monero wallet RPC. {e}\n"
                "Make sure monero-wallet-rpc is running."
            )
    
    @classmethod
    def from_env(cls) -> "Sthrip":
        """Create client from centralized settings (reads environment variables via get_settings)."""
        from sthrip.config import get_settings
        settings = get_settings()
        return cls(
            rpc_host=settings.monero_rpc_host,
            rpc_port=settings.monero_rpc_port,
            rpc_user=settings.monero_rpc_user or None,
            rpc_pass=settings.monero_rpc_pass or None,
        )
    
    # === Wallet Info ===
    
    def get_info(self) -> WalletInfo:
        """Get wallet information including balance"""
        # Get balance
        balance_result = self.wallet.get_balance(self.account_index)
        balance = Decimal(balance_result["balance"]) / Decimal("1000000000000")
        unlocked = Decimal(balance_result["unlocked_balance"]) / Decimal("1000000000000")
        
        # Get address
        addr_result = self.wallet.get_address(self.account_index)
        primary = addr_result["address"]
        
        # Get height
        height = self.wallet.get_height()
        
        return WalletInfo(
            address=primary,
            primary_address=primary,
            balance=float(balance),
            unlocked_balance=float(unlocked),
            height=height
        )
    
    @property
    def balance(self) -> float:
        """Current balance in XMR"""
        info = self.get_info()
        return info.balance
    
    @property
    def address(self) -> str:
        """Primary wallet address"""
        info = self.wallet.get_address(self.account_index)
        return info["address"]
    
    # === Stealth Addresses ===
    
    def create_stealth_address(
        self,
        label: Optional[str] = None,
        purpose: Optional[str] = None
    ) -> StealthAddress:
        """
        Create one-time stealth address for receiving payment.
        Use this for every incoming payment to maximize privacy.
        """
        return self.stealth.generate(label=label, purpose=purpose)
    
    def create_stealth_batch(self, count: int) -> List[StealthAddress]:
        """Create multiple stealth addresses at once"""
        return self.stealth.generate_batch(count, prefix="payment")
    
    # === Payments ===
    
    def pay(
        self,
        to_address: str,
        amount: float,
        memo: Optional[str] = None,
        priority: int = 2,
        mixin: Optional[int] = None,  # Now optional - will randomize
        privacy_level: str = "high"   # low/medium/high/paranoid
    ) -> Payment:
        """
        Send anonymous payment.
        
        Args:
            to_address: Recipient's Monero address
            amount: Amount in XMR
            memo: Optional note (only you can see it)
            priority: Transaction priority (0-4, 2=normal)
            mixin: Ring size for privacy (higher = better privacy)
        
        Returns:
            Payment object with transaction details
        """
        if amount <= 0:
            raise ValueError("Amount must be positive")
        
        # Privacy enhancements
        if mixin is None:
            mixin = self.privacy.get_optimal_mixin()
        
        # Obfuscate amount slightly for fingerprint resistance
        obfuscated_amount = self.privacy.obfuscate_amount(amount)
        
        # Calculate timing delay
        delay = self.privacy.calculate_delay()
        if delay > 0:
            time.sleep(delay)  # In production: use scheduler
        
        # Send transaction
        result = self.wallet.transfer(
            destination=to_address,
            amount=obfuscated_amount,
            priority=priority,
            mixin=mixin
        )
        
        tx_hash = result["tx_hash"]
        fee_atomic = result["fee"]
        fee = float(Decimal(fee_atomic) / Decimal("1000000000000"))
        
        return Payment(
            tx_hash=tx_hash,
            amount=obfuscated_amount,  # Actual sent amount
            from_address=None,  # Hidden in Monero
            to_address=to_address,
            status=PaymentStatus.PENDING,
            confirmations=0,
            fee=fee,
            timestamp=datetime.now(timezone.utc),
            memo=memo
        )
    
    def churn(
        self,
        amount: float,
        rounds: int = 3,
        delay_hours: float = 1.0
    ) -> List[Payment]:
        """
        Break transaction chain by sending to self multiple times.
        Each round uses fresh stealth address and randomized timing.
        
        Args:
            amount: Amount to churn
            rounds: Number of self-transfers (3 recommended)
            delay_hours: Delay between rounds
        
        Returns:
            List of churn transactions
        """
        payments = []
        current_amount = amount
        
        for i in range(rounds):
            # Create fresh stealth address for this round
            stealth = self.create_stealth_address(purpose=f"churn-round-{i+1}")
            
            # Randomize amount slightly
            variance = random.uniform(-0.001, 0.001)
            current_amount = max(0, current_amount + variance)
            
            # Send with max privacy
            payment = self.pay(
                to_address=stealth.address,
                amount=current_amount,
                mixin=self.privacy.config.max_mixin if hasattr(self.privacy, 'config') else 20,
                privacy_level="paranoid"
            )
            payments.append(payment)
            
            # Wait between rounds (except last)
            if i < rounds - 1:
                time.sleep(delay_hours * 3600)
        
        return payments
    
    def get_payment(self, tx_hash: str) -> Optional[Payment]:
        """Get payment details by transaction hash"""
        try:
            result = self.wallet.get_transfer_by_txid(tx_hash)
            transfer = result["transfer"]
            return self._transfer_to_payment(transfer)
        except Exception:
            return None
    
    def get_payments(
        self,
        incoming: bool = True,
        outgoing: bool = True,
        limit: Optional[int] = None
    ) -> List[Payment]:
        """Get payment history"""
        result = self.wallet.get_transfers(
            incoming=incoming,
            outgoing=outgoing,
            pending=True,
            failed=False,
            pool=True
        )
        
        payments = []
        for key in ["in", "out", "pending", "pool"]:
            if key in result:
                for transfer in result[key]:
                    payment = self._transfer_to_payment(transfer)
                    payments.append(payment)
        
        # Sort by timestamp descending
        payments.sort(key=lambda p: p.timestamp, reverse=True)
        
        if limit:
            payments = payments[:limit]
        
        return payments
    
    def wait_for_confirmation(
        self,
        tx_hash: str,
        confirmations: int = 10,
        timeout: int = 600,
        poll_interval: int = 10
    ) -> Payment:
        """
        Wait for transaction to be confirmed.
        
        Args:
            tx_hash: Transaction to wait for
            confirmations: Required confirmations
            timeout: Max seconds to wait
            poll_interval: Seconds between checks
        
        Returns:
            Confirmed Payment
        """
        start = time.time()
        while time.time() - start < timeout:
            payment = self.get_payment(tx_hash)
            if payment and payment.confirmations >= confirmations:
                return payment
            time.sleep(poll_interval)
        
        raise TimeoutError(f"Transaction {tx_hash} not confirmed within {timeout}s")
    
    # === Private ===
    
    def _transfer_to_payment(self, transfer: Dict) -> Payment:
        """Convert RPC transfer to Payment object"""
        amount_atomic = transfer.get("amount", 0)
        amount = float(Decimal(amount_atomic).abs() / Decimal("1000000000000"))

        fee_atomic = transfer.get("fee", 0)
        fee = float(Decimal(fee_atomic) / Decimal("1000000000000"))

        is_outgoing = transfer.get("type") == "out"

        timestamp = datetime.fromtimestamp(transfer.get("timestamp", 0), tz=timezone.utc)
        
        confirmations = transfer.get("confirmations", 0)
        status = PaymentStatus.CONFIRMED if confirmations >= 10 else PaymentStatus.PENDING
        
        return Payment(
            tx_hash=transfer["txid"],
            amount=amount,
            from_address=transfer.get("address") if not is_outgoing else None,
            to_address=transfer.get("address") if is_outgoing else transfer.get("address"),
            status=status,
            confirmations=confirmations,
            fee=fee,
            timestamp=timestamp
        )
    
    # === Escrow ===
    
    def create_escrow(
        self,
        seller_address: str,
        arbiter_address: str,
        amount: float,
        description: str,
        timeout_hours: int = 48
    ) -> EscrowDeal:
        """
        Create 2-of-3 multisig escrow deal.
        
        You are the buyer. You + Seller + Arbiter = 3 participants.
        Need 2 signatures to move funds.
        
        Args:
            seller_address: Seller's Monero address
            arbiter_address: Neutral arbiter's address
            amount: Amount in XMR to escrow
            description: What is being purchased
            timeout_hours: Auto-refund timeout
        
        Returns:
            EscrowDeal object
        """
        return self.escrow.create_deal(
            buyer_address=self.address,
            seller_address=seller_address,
            arbiter_address=arbiter_address,
            amount=amount,
            description=description,
            timeout_hours=timeout_hours
        )
    
    def fund_escrow(self, deal_id: str, multisig_address: str) -> EscrowDeal:
        """
        Fund escrow deal by depositing to multisig address.
        In real implementation, this would create the actual transaction.
        """
        # Escrow is disabled in current version (hub routing only)
        tx_hash = "placeholder_tx_hash"
        return self.escrow.fund_deal(deal_id, tx_hash, multisig_address)
    
    def release_escrow(self, deal_id: str) -> EscrowDeal:
        """
        Release funds to seller (as buyer).
        Requires your signature + seller signature (or arbiter in dispute).
        """
        # Escrow is disabled in current version (hub routing only)
        signature = "placeholder_sig"
        return self.escrow.release(deal_id, signature)
    
    def dispute_escrow(self, deal_id: str, reason: str) -> EscrowDeal:
        """Open dispute on escrow deal"""
        return self.escrow.open_dispute(deal_id, reason, opened_by=self.address)
    
    def get_escrow(self, deal_id: str) -> Optional[EscrowDeal]:
        """Get escrow deal by ID"""
        return self.escrow.get_deal(deal_id)
    
    def list_escrows(self, status: Optional[EscrowStatus] = None) -> List[EscrowDeal]:
        """List your escrow deals"""
        return self.escrow.list_deals(address=self.address, status=status)
    
    # === Payment Channels ===
    
    def open_channel(
        self,
        counterparty_address: str,
        capacity: float,
        their_capacity: float = 0.0
    ) -> PaymentChannel:
        """
        Open payment channel for instant micropayments.
        
        Args:
            counterparty_address: Other agent's address
            capacity: Your XMR to lock in channel
            their_capacity: Their XMR (usually 0 for simple channel)
        
        Returns:
            PaymentChannel object
        """
        return self.channels.propose_channel(
            my_address=self.address,
            counterparty_address=counterparty_address,
            my_capacity=capacity,
            their_capacity=their_capacity
        )
    
    def channel_pay(self, channel_id: str, amount: float) -> ChannelState:
        """
        Pay through channel instantly (off-chain).
        Zero fees, instant confirmation!
        """
        return self.channels.pay(
            channel_id=channel_id,
            amount=amount,
            direction="a_to_b"  # Assuming we're agent_a
        )
    
    def receive_channel_payment(self, channel_id: str, amount: float) -> ChannelState:
        """Receive payment through channel (as counterparty)"""
        return self.channels.pay(
            channel_id=channel_id,
            amount=amount,
            direction="b_to_a"
        )
    
    def close_channel(self, channel_id: str, cooperative: bool = True) -> PaymentChannel:
        """Close payment channel and settle on-chain"""
        if cooperative:
            return self.channels.close_cooperative(channel_id, "my_sig")
        else:
            return self.channels.close_force(channel_id, "my_sig")
    
    def get_channel(self, channel_id: str) -> Optional[PaymentChannel]:
        """Get channel details"""
        return self.channels.get_channel(channel_id)
    
    def list_channels(self, status: Optional[ChannelStatus] = None) -> List[PaymentChannel]:
        """List your payment channels"""
        return self.channels.list_channels(self.address, status)
    
    def get_channel_balance(self, channel_id: str) -> float:
        """Get your balance in specific channel"""
        return self.channels.get_balance(channel_id, self.address)
