"""
Gossip Protocol for MPC Network

Implements epidemic broadcast for efficient message propagation.
"""

import asyncio
import random
import logging
from typing import Dict, Set, Optional
from dataclasses import dataclass
import time

from .node import P2PNode, NodeMessage, MessageType

logger = logging.getLogger(__name__)


@dataclass
class GossipMessage:
    """Message with metadata for gossip protocol"""
    msg_id: str
    data: str
    timestamp: float
    hops: int = 0
    received_from: Set[str] = None
    
    def __post_init__(self):
        if self.received_from is None:
            self.received_from = set()


class GossipProtocol:
    """
    Epidemic gossip protocol for message propagation.
    
    Ensures messages reach all nodes with high probability
    even in unreliable networks.
    """
    
    def __init__(
        self,
        p2p_node: P2PNode,
        fanout: int = 3,
        max_hops: int = 10
    ):
        self.p2p_node = p2p_node
        self.fanout = fanout  # Number of peers to forward to
        self.max_hops = max_hops
        
        # Message cache for deduplication
        self.seen_messages: Dict[str, GossipMessage] = {}
        self.max_cache_size = 10000
        
    async def broadcast(self, data: str, msg_id: Optional[str] = None) -> None:
        """
        Broadcast message using gossip protocol.
        
        Instead of flooding all peers, randomly select fanout peers.
        """
        msg_id = msg_id or self._generate_msg_id()
        
        gossip_msg = GossipMessage(
            msg_id=msg_id,
            data=data,
            timestamp=time.time(),
            hops=0
        )
        
        # Store in cache
        self.seen_messages[msg_id] = gossip_msg
        
        # Forward to random subset of peers
        await self._forward(gossip_msg)
    
    async def handle_incoming(
        self,
        msg: NodeMessage,
        from_peer: str
    ) -> bool:
        """
        Handle incoming gossip message.
        
        Returns True if message is new and should be processed.
        """
        msg_id = msg.msg_id
        
        # Check if already seen
        if msg_id in self.seen_messages:
            # Record that we received from this peer
            self.seen_messages[msg_id].received_from.add(from_peer)
            return False
        
        # New message
        gossip_msg = GossipMessage(
            msg_id=msg_id,
            data=msg.payload.get("data", ""),
            timestamp=msg.timestamp,
            hops=msg.payload.get("hops", 0) + 1,
            received_from={from_peer}
        )
        
        self.seen_messages[msg_id] = gossip_msg
        
        # Clean cache if needed
        self._clean_cache()
        
        # Forward if hops < max
        if gossip_msg.hops < self.max_hops:
            asyncio.create_task(self._forward(gossip_msg, exclude=[from_peer]))
        
        return True
    
    async def _forward(
        self,
        gossip_msg: GossipMessage,
        exclude: Optional[Set[str]] = None
    ) -> None:
        """Forward message to random subset of peers"""
        exclude = exclude or set()
        
        peers = [
            pid for pid in self.p2p_node.peers.keys()
            if pid not in exclude and pid not in gossip_msg.received_from
        ]
        
        if not peers:
            return
        
        # Select random fanout peers
        if len(peers) <= self.fanout:
            selected = peers
        else:
            selected = random.sample(peers, self.fanout)
        
        # Create payload
        payload = {
            "data": gossip_msg.data,
            "hops": gossip_msg.hops,
            "original_msg_id": gossip_msg.msg_id
        }
        
        # Send to selected peers
        for peer_id in selected:
            success = await self.p2p_node.send_direct(
                peer_id,
                MessageType.BROADCAST,
                payload
            )
            if success:
                gossip_msg.received_from.add(peer_id)
    
    def _generate_msg_id(self) -> str:
        """Generate unique message ID"""
        import secrets
        return secrets.token_hex(16)
    
    def _clean_cache(self) -> None:
        """Remove old messages from cache"""
        if len(self.seen_messages) > self.max_cache_size:
            # Remove oldest 50%
            sorted_msgs = sorted(
                self.seen_messages.items(),
                key=lambda x: x[1].timestamp
            )
            to_remove = len(sorted_msgs) // 2
            for msg_id, _ in sorted_msgs[:to_remove]:
                del self.seen_messages[msg_id]


class PlumTreeProtocol:
    """
    PlumTree: hybrid gossip protocol (epidemic + lazy push).
    
    More efficient than pure gossip for large networks.
    """
    
    def __init__(self, p2p_node: P2PNode):
        self.p2p_node = p2p_node
        self.eager_peers: Set[str] = set()  # Fast push
        self.lazy_peers: Set[str] = set()   # Lazy push (digest only)
        
        self.message_log: Dict[str, dict] = {}
        
    async def broadcast(self, data: str) -> None:
        """Broadcast using eager push to eager peers"""
        # Send full message to eager peers
        for peer_id in self.eager_peers:
            if peer_id in self.p2p_node.peers:
                await self.p2p_node.send_direct(
                    peer_id,
                    MessageType.BROADCAST,
                    {"data": data, "type": "eager"}
                )
        
        # Send digest to lazy peers
        digest = self._compute_digest(data)
        for peer_id in self.lazy_peers:
            if peer_id in self.p2p_node.peers:
                await self.p2p_node.send_direct(
                    peer_id,
                    MessageType.BROADCAST,
                    {"digest": digest, "type": "lazy"}
                )
    
    def _compute_digest(self, data: str) -> str:
        """Compute message digest"""
        import hashlib
        return hashlib.sha256(data.encode()).hexdigest()[:16]
    
    def optimize_topology(self) -> None:
        """Optimize eager/lazy partition based on churn"""
        # This would analyze network and adjust partitions
        pass
