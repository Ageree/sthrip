"""
Bridge Coordinator

High-level coordinator for ETH↔XMR bridge operations.
Manages user-facing API for cross-chain transfers.
"""

import asyncio
from typing import Optional, Dict, Any, List
from decimal import Decimal
from dataclasses import dataclass
from enum import Enum
import secrets
import logging
import time

from ..contracts.eth_bridge import EthereumBridgeContract, BridgeLock
from .mpc_node import MPCRelayerNode


logger = logging.getLogger(__name__)


class BridgeDirection(Enum):
    """Direction of bridge transfer"""
    ETH_TO_XMR = "eth_to_xmr"
    XMR_TO_ETH = "xmr_to_eth"


class BridgeTransferStatus(Enum):
    """Status of bridge transfer"""
    PENDING = "pending"
    ETH_LOCKED = "eth_locked"
    XMR_SENT = "xmr_sent"
    ETH_CLAIMED = "eth_claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"


@dataclass
class BridgeTransfer:
    """Bridge transfer request"""
    transfer_id: str
    direction: BridgeDirection
    eth_amount: Decimal
    xmr_amount: Decimal
    eth_address: Optional[str]  # For XMR->ETH
    xmr_address: Optional[str]  # For ETH->XMR
    
    # Status
    status: BridgeTransferStatus
    
    # Transactions
    eth_lock_tx: Optional[str] = None
    xmr_send_tx: Optional[str] = None
    eth_claim_tx: Optional[str] = None
    
    # Timestamps
    created_at: float = 0
    updated_at: float = 0
    
    def __post_init__(self):
        if self.created_at == 0:
            self.created_at = time.time()
        if self.updated_at == 0:
            self.updated_at = time.time()


class BridgeFeeCalculator:
    """Calculate bridge fees"""
    
    def __init__(
        self,
        base_fee_eth: Decimal = Decimal("0.001"),
        base_fee_xmr: Decimal = Decimal("0.01"),
        percentage_fee: Decimal = Decimal("0.001")  # 0.1%
    ):
        self.base_fee_eth = base_fee_eth
        self.base_fee_xmr = base_fee_xmr
        self.percentage_fee = percentage_fee
    
    def calculate_eth_to_xmr(
        self,
        eth_amount: Decimal,
        xmr_price_ratio: Decimal = Decimal("10")  # 1 ETH = 10 XMR
    ) -> tuple[Decimal, Decimal]:
        """
        Calculate fees for ETH->XMR transfer.
        
        Returns:
            (xmr_to_send, fee)
        """
        # Convert ETH to XMR
        xmr_amount = eth_amount * xmr_price_ratio
        
        # Calculate fee
        percentage = xmr_amount * self.percentage_fee
        fee = max(self.base_fee_xmr, percentage)
        
        xmr_to_send = xmr_amount - fee
        
        return xmr_to_send, fee
    
    def calculate_xmr_to_eth(
        self,
        xmr_amount: Decimal,
        eth_price_ratio: Decimal = Decimal("0.1")  # 1 XMR = 0.1 ETH
    ) -> tuple[Decimal, Decimal]:
        """
        Calculate fees for XMR->ETH transfer.
        
        Returns:
            (eth_to_send, fee)
        """
        # Convert XMR to ETH
        eth_amount = xmr_amount * eth_price_ratio
        
        # Calculate fee
        percentage = eth_amount * self.percentage_fee
        fee = max(self.base_fee_eth, percentage)
        
        eth_to_send = eth_amount - fee
        
        return eth_to_send, fee


class BridgeCoordinator:
    """
    High-level coordinator for ETH↔XMR bridge.
    
    Provides simple API for users:
    - Bridge ETH to XMR
    - Bridge XMR to ETH
    - Check transfer status
    """
    
    def __init__(
        self,
        eth_bridge: EthereumBridgeContract,
        mpc_nodes: List[MPCRelayerNode],
        fee_calculator: Optional[BridgeFeeCalculator] = None
    ):
        self.eth_bridge = eth_bridge
        self.mpc_nodes = mpc_nodes
        self.fee_calculator = fee_calculator or BridgeFeeCalculator()
        
        # Active transfers
        self.transfers: Dict[str, BridgeTransfer] = {}
        
        # Monitoring
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
    
    async def start(self) -> None:
        """Start the bridge coordinator"""
        logger.info("Starting Bridge Coordinator...")
        
        # Start MPC nodes
        for node in self.mpc_nodes:
            await node.start()
        
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        
        logger.info("Bridge Coordinator started")
    
    async def stop(self) -> None:
        """Stop the bridge coordinator"""
        self._running = False
        
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        
        # Stop MPC nodes
        for node in self.mpc_nodes:
            await node.stop()
        
        logger.info("Bridge Coordinator stopped")
    
    async def bridge_eth_to_xmr(
        self,
        eth_amount: Decimal,
        xmr_address: str,
        sender_eth_address: str,
        duration_hours: int = 24
    ) -> BridgeTransfer:
        """
        Bridge ETH to XMR.
        
        Args:
            eth_amount: Amount of ETH to bridge
            xmr_address: Monero address to receive XMR
            sender_eth_address: Ethereum sender address
            duration_hours: Lock duration in hours
            
        Returns:
            BridgeTransfer object
        """
        # Calculate amounts
        xmr_to_send, fee = self.fee_calculator.calculate_eth_to_xmr(eth_amount)
        
        logger.info(f"Bridging {eth_amount} ETH -> {xmr_to_send} XMR")
        logger.info(f"Fee: {fee} XMR")
        
        # Create transfer record
        transfer = BridgeTransfer(
            transfer_id=secrets.token_hex(16),
            direction=BridgeDirection.ETH_TO_XMR,
            eth_amount=eth_amount,
            xmr_amount=xmr_to_send,
            eth_address=sender_eth_address,
            xmr_address=xmr_address,
            status=BridgeTransferStatus.PENDING
        )
        
        self.transfers[transfer.transfer_id] = transfer
        
        # Lock ETH in bridge contract
        duration_seconds = duration_hours * 3600
        
        try:
            lock_id = self.eth_bridge.lock(
                xmr_address=xmr_address,
                eth_amount=eth_amount,
                duration_seconds=duration_seconds,
                sender_address=sender_eth_address
            )
            
            transfer.eth_lock_tx = lock_id
            transfer.status = BridgeTransferStatus.ETH_LOCKED
            transfer.updated_at = time.time()
            
            logger.info(f"ETH locked: {lock_id}")
            
        except Exception as e:
            logger.error(f"Failed to lock ETH: {e}")
            transfer.status = BridgeTransferStatus.FAILED
            raise
        
        return transfer
    
    async def bridge_xmr_to_eth(
        self,
        xmr_amount: Decimal,
        eth_address: str,
        sender_xmr_address: str
    ) -> BridgeTransfer:
        """
        Bridge XMR to ETH.
        
        Args:
            xmr_amount: Amount of XMR to bridge
            eth_address: Ethereum address to receive ETH
            sender_xmr_address: Monero sender address
            
        Returns:
            BridgeTransfer object
        """
        # Calculate amounts
        eth_to_send, fee = self.fee_calculator.calculate_xmr_to_eth(xmr_amount)
        
        logger.info(f"Bridging {xmr_amount} XMR -> {eth_to_send} ETH")
        logger.info(f"Fee: {fee} ETH")
        
        # Create transfer record
        transfer = BridgeTransfer(
            transfer_id=secrets.token_hex(16),
            direction=BridgeDirection.XMR_TO_ETH,
            eth_amount=eth_to_send,
            xmr_amount=xmr_amount,
            eth_address=eth_address,
            xmr_address=sender_xmr_address,
            status=BridgeTransferStatus.PENDING
        )
        
        self.transfers[transfer.transfer_id] = transfer
        
        # For XMR->ETH, user sends XMR to MPC multisig
        # Then MPC nodes mint/claim ETH
        
        logger.info(f"Please send {xmr_amount} XMR to MPC address")
        logger.info(f"You will receive {eth_to_send} ETH at {eth_address}")
        
        return transfer
    
    async def get_transfer_status(
        self,
        transfer_id: str
    ) -> Optional[BridgeTransfer]:
        """Get transfer status"""
        return self.transfers.get(transfer_id)
    
    async def list_transfers(
        self,
        status: Optional[BridgeTransferStatus] = None
    ) -> List[BridgeTransfer]:
        """List transfers, optionally filtered by status"""
        transfers = list(self.transfers.values())
        
        if status:
            transfers = [t for t in transfers if t.status == status]
        
        return transfers
    
    async def _monitor_loop(self) -> None:
        """Monitor transfer status"""
        while self._running:
            try:
                for transfer in list(self.transfers.values()):
                    await self._update_transfer_status(transfer)
                    
            except Exception as e:
                logger.error(f"Monitor error: {e}")
            
            await asyncio.sleep(30)
    
    async def _update_transfer_status(self, transfer: BridgeTransfer) -> None:
        """Update transfer status from blockchain"""
        if transfer.direction == BridgeDirection.ETH_TO_XMR:
            await self._update_eth_to_xmr_status(transfer)
        else:
            await self._update_xmr_to_eth_status(transfer)
    
    async def _update_eth_to_xmr_status(
        self,
        transfer: BridgeTransfer
    ) -> None:
        """Update ETH->XMR transfer status"""
        if transfer.status == BridgeTransferStatus.ETH_LOCKED:
            # Check if XMR was sent
            # This would query MPC nodes for XMR tx status
            
            # For now, check if any MPC node has processed this
            for node in self.mpc_nodes:
                if transfer.eth_lock_tx in node.pending_requests:
                    # Still pending
                    return
                if transfer.eth_lock_tx in node.signed_requests:
                    # XMR sent, ETH claimed
                    transfer.status = BridgeTransferStatus.COMPLETED
                    transfer.updated_at = time.time()
                    return
    
    async def _update_xmr_to_eth_status(
        self,
        transfer: BridgeTransfer
    ) -> None:
        """Update XMR->ETH transfer status"""
        # Check if XMR received
        # Then check if ETH sent
        pass
    
    def get_stats(self) -> Dict[str, Any]:
        """Get bridge statistics"""
        total_transfers = len(self.transfers)
        completed = sum(1 for t in self.transfers.values() 
                       if t.status == BridgeTransferStatus.COMPLETED)
        pending = sum(1 for t in self.transfers.values() 
                     if t.status == BridgeTransferStatus.PENDING)
        failed = sum(1 for t in self.transfers.values() 
                    if t.status == BridgeTransferStatus.FAILED)
        
        total_eth_volume = sum(
            t.eth_amount for t in self.transfers.values()
            if t.status == BridgeTransferStatus.COMPLETED
        )
        
        total_xmr_volume = sum(
            t.xmr_amount for t in self.transfers.values()
            if t.status == BridgeTransferStatus.COMPLETED
        )
        
        return {
            "total_transfers": total_transfers,
            "completed": completed,
            "pending": pending,
            "failed": failed,
            "total_eth_volume": str(total_eth_volume),
            "total_xmr_volume": str(total_xmr_volume),
            "mpc_nodes_online": sum(
                1 for n in self.mpc_nodes 
                if n.status.value == "online"
            ),
        }


# Convenience functions for quick bridging
async def quick_bridge_eth_to_xmr(
    eth_amount: float,
    xmr_address: str,
    eth_private_key: str,
    bridge_contract_address: str,
    eth_rpc_url: str = "http://localhost:8545"
) -> str:
    """
    Quick bridge ETH to XMR.
    
    Args:
        eth_amount: Amount of ETH to bridge
        xmr_address: Monero address to receive
        eth_private_key: Ethereum private key
        bridge_contract_address: Bridge contract address
        eth_rpc_url: Ethereum RPC URL
        
    Returns:
        transfer_id: Bridge transfer ID
    """
    # Create bridge contract connection
    bridge = EthereumBridgeContract(
        web3_provider=eth_rpc_url,
        contract_address=bridge_contract_address,
        private_key=eth_private_key
    )
    
    # Create coordinator (simplified - no MPC nodes for quick bridge)
    coordinator = BridgeCoordinator(bridge, [])
    
    # Execute bridge
    transfer = await coordinator.bridge_eth_to_xmr(
        eth_amount=Decimal(str(eth_amount)),
        xmr_address=xmr_address,
        sender_eth_address=bridge._get_account_address(),
        duration_hours=24
    )
    
    return transfer.transfer_id


async def quick_bridge_xmr_to_eth(
    xmr_amount: float,
    eth_address: str,
    xmr_wallet_host: str = "localhost",
    xmr_wallet_port: int = 18082
) -> str:
    """
    Quick bridge XMR to ETH.
    
    Args:
        xmr_amount: Amount of XMR to bridge
        eth_address: Ethereum address to receive
        xmr_wallet_host: Monero wallet RPC host
        xmr_wallet_port: Monero wallet RPC port
        
    Returns:
        transfer_id: Bridge transfer ID
    """
    from stealthpay.swaps.xmr.wallet import MoneroWallet
    
    # Create XMR wallet connection
    xmr_wallet = MoneroWallet(
        host=xmr_wallet_host,
        port=xmr_wallet_port
    )
    
    # Get sender address
    sender_address = xmr_wallet.get_address()
    
    # This is simplified - real implementation needs MPC coordination
    logger.info(f"Please send {xmr_amount} XMR to bridge address")
    logger.info(f"ETH will be sent to {eth_address}")
    
    return secrets.token_hex(16)
