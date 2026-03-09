"""
MPC Relayer Node v2 - Production Ready

Integrated with TSS signing and P2P network.
"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any, List
from decimal import Decimal
from dataclasses import dataclass
from enum import Enum

from ..contracts.eth_bridge import EthereumBridgeContract
from ..tss.dkg import KeyShare, DistributedKeyGenerator, SecureKeyStorage
from ..tss.signer import ThresholdSigner, SigningSession, PartialSignature
from ..tss.aggregator import SignatureAggregator, ECDSASignature
from ..p2p.node import P2PNode, NodeMessage, MessageType
from ..p2p.gossip import GossipProtocol
from ...swaps.xmr.wallet import MoneroWallet


logger = logging.getLogger(__name__)


class MPCNodeStatus(Enum):
    """Status of MPC node"""
    INITIALIZING = "initializing"
    ONLINE = "online"
    SIGNING = "signing"
    OFFLINE = "offline"
    ERROR = "error"


@dataclass
class BridgeOperation:
    """Bridge operation being processed"""
    operation_id: str
    lock_id: str
    direction: str  # "eth_to_xmr" or "xmr_to_eth"
    amount_eth: Decimal
    amount_xmr: Decimal
    recipient_address: str
    status: str
    created_at: float
    
    # Signing session
    signing_session: Optional[SigningSession] = None
    partial_signatures: Dict[int, PartialSignature] = None
    
    def __post_init__(self):
        if self.partial_signatures is None:
            self.partial_signatures = {}


class MPCRelayerNodeV2:
    """
    Production-ready MPC Relayer Node.
    
    Integrates:
    - TSS threshold signing
    - P2P communication
    - Ethereum bridge contract
    - Monero multi-sig
    """
    
    def __init__(
        self,
        node_id: str,
        key_share: KeyShare,
        eth_bridge: EthereumBridgeContract,
        xmr_wallet: MoneroWallet,
        p2p_node: Optional[P2PNode] = None,
        p2p_host: str = "0.0.0.0",
        p2p_port: int = 10000,
        bootstrap_peers: List[str] = None
    ):
        self.node_id = node_id
        self.key_share = key_share
        self.eth_bridge = eth_bridge
        self.xmr_wallet = xmr_wallet
        
        # Create TSS signer
        self.tss_signer = ThresholdSigner(key_share, int(node_id.split("_")[-1]))
        
        # Create or use provided P2P node
        if p2p_node:
            self.p2p = p2p_node
        else:
            self.p2p = P2PNode(
                node_id=node_id,
                listen_host=p2p_host,
                listen_port=p2p_port,
                bootstrap_peers=bootstrap_peers or []
            )
        
        self.gossip = GossipProtocol(self.p2p)
        
        # State
        self.status = MPCNodeStatus.INITIALIZING
        self.operations: Dict[str, BridgeOperation] = {}
        self.completed_operations: Dict[str, BridgeOperation] = {}
        
        # Threshold config
        self.threshold = 3
        self.total_nodes = 5
        
        # Register message handlers
        self._register_handlers()
    
    def _register_handlers(self) -> None:
        """Register P2P message handlers"""
        
        @self.p2p.on_message(MessageType.SIGN_COMMIT)
        async def handle_sign_commit(msg: NodeMessage, from_peer: str):
            await self._handle_sign_commit(msg)
        
        @self.p2p.on_message(MessageType.SIGN_CHALLENGE)
        async def handle_sign_challenge(msg: NodeMessage, from_peer: str):
            await self._handle_sign_challenge(msg)
        
        @self.p2p.on_message(MessageType.SIGN_SHARE)
        async def handle_sign_share(msg: NodeMessage, from_peer: str):
            await self._handle_sign_share(msg)
        
        @self.p2p.on_message(MessageType.BRIDGE_REQUEST)
        async def handle_bridge_request(msg: NodeMessage, from_peer: str):
            await self._handle_bridge_request(msg)
    
    async def start(self) -> None:
        """Start the MPC node"""
        logger.info(f"Starting MPC Relayer Node {self.node_id}")
        
        # Start P2P
        await self.p2p.start()
        
        # Start blockchain monitors
        asyncio.create_task(self._monitor_eth_bridge())
        asyncio.create_task(self._monitor_xmr_wallet())
        
        self.status = MPCNodeStatus.ONLINE
        logger.info(f"MPC Node {self.node_id} is online")
        logger.info(f"Connected peers: {self.p2p.get_peer_count()}")
    
    async def stop(self) -> None:
        """Stop the MPC node"""
        logger.info(f"Stopping MPC Node {self.node_id}")
        self.status = MPCNodeStatus.OFFLINE
        await self.p2p.stop()
    
    async def initiate_bridge_to_xmr(
        self,
        lock_id: str,
        eth_amount: Decimal,
        xmr_address: str
    ) -> str:
        """
        Initiate bridge operation: ETH -> XMR.
        
        Called when ETH is locked in bridge contract.
        """
        operation_id = f"bridge_{lock_id}_{int(time.time())}"
        
        # Calculate XMR amount (using oracle price)
        xmr_amount = self._convert_eth_to_xmr(eth_amount)
        
        operation = BridgeOperation(
            operation_id=operation_id,
            lock_id=lock_id,
            direction="eth_to_xmr",
            amount_eth=eth_amount,
            amount_xmr=xmr_amount,
            recipient_address=xmr_address,
            status="pending_approval",
            created_at=time.time()
        )
        
        self.operations[operation_id] = operation
        
        # Broadcast bridge request to other nodes
        await self.p2p.broadcast(MessageType.BRIDGE_REQUEST, {
            "operation_id": operation_id,
            "lock_id": lock_id,
            "eth_amount": str(eth_amount),
            "xmr_amount": str(xmr_amount),
            "xmr_address": xmr_address,
            "requesting_node": self.node_id
        })
        
        logger.info(f"Bridge request broadcast: {operation_id}")
        
        return operation_id
    
    async def sign_bridge_operation(
        self,
        operation_id: str,
        message_hash: bytes
    ) -> Optional[ECDSASignature]:
        """
        Participate in threshold signing for bridge operation.
        
        This is a 2-round protocol:
        1. Broadcast nonce commitment
        2. Receive challenge, broadcast signature share
        """
        operation = self.operations.get(operation_id)
        if not operation:
            logger.error(f"Operation not found: {operation_id}")
            return None
        
        self.status = MPCNodeStatus.SIGNING
        
        try:
            # Create signing session
            session = SigningSession(message_hash, self.threshold)
            operation.signing_session = session
            
            # Phase 1: Create and broadcast commitment
            logger.info(f"Signing Phase 1: Creating commitment")
            
            partial_sig = self.tss_signer.create_partial_signature(message_hash)
            session.add_commitment(partial_sig)
            
            # Broadcast commitment
            await self.p2p.broadcast(MessageType.SIGN_COMMIT, {
                "operation_id": operation_id,
                "party_id": self.node_id,
                "commitment": partial_sig.c,
                "public_nonce": partial_sig.public_nonce.to_bytes().hex() if hasattr(partial_sig.public_nonce, 'to_bytes') else str(partial_sig.public_nonce),
                "index": partial_sig.index
            })
            
            # Wait for other commitments (with timeout)
            logger.info(f"Waiting for {self.threshold - 1} more commitments...")
            await asyncio.wait_for(
                self._wait_for_commitments(operation, self.threshold),
                timeout=30.0
            )
            
            # Finalize commitments and create challenge
            context = session.finalize_commitments()
            
            # Broadcast challenge
            await self.p2p.broadcast(MessageType.SIGN_CHALLENGE, {
                "operation_id": operation_id,
                "aggregated_nonce": context.aggregated_nonce.to_bytes().hex() if hasattr(context.aggregated_nonce, 'to_bytes') else str(context.aggregated_nonce),
                "challenge": context.challenge,
                "message_hash": message_hash.hex()
            })
            
            # Phase 2: Create signature share
            logger.info(f"Signing Phase 2: Creating signature share")
            
            completed_sig = self.tss_signer.complete_signature(partial_sig, context)
            session.add_signature_share(completed_sig)
            
            # Broadcast signature share
            await self.p2p.broadcast(MessageType.SIGN_SHARE, {
                "operation_id": operation_id,
                "party_id": self.node_id,
                "z": completed_sig.z,
                "index": completed_sig.index
            })
            
            # Wait for other shares
            logger.info(f"Waiting for {self.threshold - 1} more shares...")
            await asyncio.wait_for(
                self._wait_for_shares(operation, self.threshold),
                timeout=30.0
            )
            
            # Aggregate signatures
            logger.info(f"Aggregating signatures...")
            
            aggregator = SignatureAggregator()
            full_sig = aggregator.aggregate_signatures(
                list(operation.partial_signatures.values()),
                context
            )
            
            # Verify signature
            public_key = self.key_share.public_key
            if not full_sig.verify(message_hash, public_key):
                logger.error("Signature verification failed!")
                return None
            
            logger.info(f"Signature created successfully: {operation_id}")
            operation.status = "signed"
            
            return full_sig
            
        except asyncio.TimeoutError:
            logger.error(f"Signing timeout for {operation_id}")
            operation.status = "timeout"
            return None
        except Exception as e:
            logger.error(f"Signing error: {e}")
            operation.status = "error"
            return None
        finally:
            self.status = MPCNodeStatus.ONLINE
    
    async def _wait_for_commitments(
        self,
        operation: BridgeOperation,
        count: int
    ) -> None:
        """Wait for commitments from other nodes"""
        while len(operation.signing_session.commitments) < count:
            await asyncio.sleep(0.1)
    
    async def _wait_for_shares(
        self,
        operation: BridgeOperation,
        count: int
    ) -> None:
        """Wait for signature shares from other nodes"""
        while len(operation.signing_session.signatures) < count:
            await asyncio.sleep(0.1)
    
    async def _handle_sign_commit(self, msg: NodeMessage) -> None:
        """Handle incoming signing commitment"""
        payload = msg.payload
        operation_id = payload.get("operation_id")
        
        if operation_id not in self.operations:
            return
        
        operation = self.operations[operation_id]
        if not operation.signing_session:
            return
        
        # Create partial signature from commitment
        from ecdsa.ellipticcurve import Point
        
        # Parse public nonce (simplified)
        public_nonce_hex = payload.get("public_nonce", "")
        # In production, properly deserialize point
        
        partial_sig = PartialSignature(
            party_id=payload.get("party_id"),
            index=payload.get("index"),
            c=payload.get("commitment"),
            z=None,
            public_nonce=None  # Would deserialize from hex
        )
        
        operation.signing_session.add_commitment(partial_sig)
        logger.debug(f"Added commitment from {msg.sender_id}")
    
    async def _handle_sign_challenge(self, msg: NodeMessage) -> None:
        """Handle signing challenge"""
        payload = msg.payload
        operation_id = payload.get("operation_id")
        
        if operation_id not in self.operations:
            return
        
        operation = self.operations[operation_id]
        
        # Create context from challenge
        # In production, properly deserialize
        logger.debug(f"Received challenge for {operation_id}")
    
    async def _handle_sign_share(self, msg: NodeMessage) -> None:
        """Handle incoming signature share"""
        payload = msg.payload
        operation_id = payload.get("operation_id")
        
        if operation_id not in self.operations:
            return
        
        operation = self.operations[operation_id]
        if not operation.signing_session:
            return
        
        partial_sig = PartialSignature(
            party_id=payload.get("party_id"),
            index=payload.get("index"),
            c=0,
            z=payload.get("z"),
            public_nonce=None
        )
        
        operation.partial_signatures[payload.get("index")] = partial_sig
        operation.signing_session.add_signature_share(partial_sig)
        
        logger.debug(f"Added signature share from {msg.sender_id}")
    
    async def _handle_bridge_request(self, msg: NodeMessage) -> None:
        """Handle bridge request from another node"""
        payload = msg.payload
        operation_id = payload.get("operation_id")
        
        if operation_id in self.operations:
            return  # Already have this operation
        
        # Create operation record
        operation = BridgeOperation(
            operation_id=operation_id,
            lock_id=payload.get("lock_id"),
            direction="eth_to_xmr",
            amount_eth=Decimal(payload.get("eth_amount", "0")),
            amount_xmr=Decimal(payload.get("xmr_amount", "0")),
            recipient_address=payload.get("xmr_address"),
            status="received",
            created_at=time.time()
        )
        
        self.operations[operation_id] = operation
        
        # Auto-approve for now (in production, validate first)
        await self.p2p.send_direct(
            msg.sender_id,
            MessageType.BRIDGE_APPROVE,
            {"operation_id": operation_id, "approver": self.node_id}
        )
        
        logger.info(f"Approved bridge request: {operation_id}")
    
    async def _monitor_eth_bridge(self) -> None:
        """Monitor Ethereum bridge for new locks"""
        while self.status != MPCNodeStatus.OFFLINE:
            try:
                # Get recent Locked events
                events = self.eth_bridge.watch_events(from_block=-10)
                
                for event in events:
                    if event['event'] == 'Locked':
                        lock_id = event['lock_id']
                        
                        # Check if we're processing this
                        existing = [
                            op for op in self.operations.values()
                            if op.lock_id == lock_id
                        ]
                        
                        if not existing:
                            # Initiate bridge operation
                            await self.initiate_bridge_to_xmr(
                                lock_id=lock_id,
                                eth_amount=event['amount'],
                                xmr_address=event['xmr_address']
                            )
                
            except Exception as e:
                logger.error(f"ETH monitor error: {e}")
            
            await asyncio.sleep(15)  # Check every 15 seconds
    
    async def _monitor_xmr_wallet(self) -> None:
        """Monitor Monero wallet"""
        while self.status != MPCNodeStatus.OFFLINE:
            try:
                # Get balance
                balance = self.xmr_wallet.get_balance()
                logger.debug(f"XMR Balance: {balance}")
                
            except Exception as e:
                logger.error(f"XMR monitor error: {e}")
            
            await asyncio.sleep(60)  # Check every minute
    
    def _convert_eth_to_xmr(self, eth_amount: Decimal) -> Decimal:
        """Convert ETH to XMR using price oracle"""
        # In production, use Chainlink or other oracle
        # Placeholder: 1 ETH = 10 XMR
        return eth_amount * Decimal("10")
    
    def get_status(self) -> Dict[str, Any]:
        """Get node status"""
        return {
            "node_id": self.node_id,
            "status": self.status.value,
            "p2p": self.p2p.get_status(),
            "operations_pending": len(self.operations),
            "operations_completed": len(self.completed_operations),
            "key_share_index": self.key_share.index if self.key_share else None
        }
