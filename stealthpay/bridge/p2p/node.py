"""
P2P Node for MPC Network

WebSocket-based peer-to-peer communication between MPC nodes.
"""

import asyncio
import json
import logging
import secrets
import time
from typing import Dict, List, Optional, Callable, Set
from dataclasses import dataclass, asdict
from enum import Enum
import websockets
from websockets.server import serve
from websockets.client import connect

logger = logging.getLogger(__name__)


class MessageType(Enum):
    """Types of P2P messages"""
    # Discovery
    PING = "ping"
    PONG = "pong"
    PEER_LIST = "peer_list"
    
    # Signing
    SIGN_COMMIT = "sign_commit"      # Phase 1: Commit to nonce
    SIGN_CHALLENGE = "sign_challenge" # Broadcast challenge
    SIGN_SHARE = "sign_share"         # Phase 2: Signature share
    
    # Bridge
    BRIDGE_REQUEST = "bridge_request"
    BRIDGE_APPROVE = "bridge_approve"
    BRIDGE_REJECT = "bridge_reject"
    
    # Consensus
    CONSENSUS_PROPOSE = "consensus_propose"
    CONSENSUS_VOTE = "consensus_vote"
    
    # General
    BROADCAST = "broadcast"
    DIRECT = "direct"


@dataclass
class NodeMessage:
    """Message format for P2P communication"""
    msg_id: str
    msg_type: str
    sender_id: str
    recipient_id: Optional[str]  # None for broadcast
    payload: dict
    timestamp: float
    signature: Optional[str] = None  # Optional message signature
    
    def to_json(self) -> str:
        return json.dumps({
            "msg_id": self.msg_id,
            "msg_type": self.msg_type,
            "sender_id": self.sender_id,
            "recipient_id": self.recipient_id,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "signature": self.signature
        })
    
    @classmethod
    def from_json(cls, data: str) -> "NodeMessage":
        obj = json.loads(data)
        return cls(**obj)


class P2PNode:
    """
    P2P Node for MPC network communication.
    
    Features:
    - WebSocket server for incoming connections
    - WebSocket clients for outgoing connections
    - Automatic reconnection
    - Message routing (broadcast and direct)
    - Peer discovery and maintenance
    """
    
    def __init__(
        self,
        node_id: str,
        listen_host: str = "0.0.0.0",
        listen_port: int = 10000,
        bootstrap_peers: List[str] = None
    ):
        self.node_id = node_id
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.bootstrap_peers = bootstrap_peers or []
        
        # Connection management
        self.peers: Dict[str, websockets.WebSocketClientProtocol] = {}
        self.peer_info: Dict[str, dict] = {}  # Metadata about peers
        self.server = None
        
        # Message handling
        self.message_handlers: Dict[MessageType, List[Callable]] = {
            msg_type: [] for msg_type in MessageType
        }
        self.received_msgs: Set[str] = set()  # Deduplication
        
        # State
        self._running = False
        self._shutdown_event = asyncio.Event()
        
    async def start(self) -> None:
        """Start the P2P node"""
        logger.info(f"Starting P2P node {self.node_id} on {self.listen_host}:{self.listen_port}")
        
        self._running = True
        
        # Start WebSocket server
        self.server = await serve(
            self._handle_connection,
            self.listen_host,
            self.listen_port
        )
        
        logger.info(f"WebSocket server started on port {self.listen_port}")
        
        # Connect to bootstrap peers
        for peer_addr in self.bootstrap_peers:
            asyncio.create_task(self._connect_to_peer(peer_addr))
        
        # Start maintenance tasks
        asyncio.create_task(self._maintenance_loop())
        asyncio.create_task(self._heartbeat_loop())
    
    async def stop(self) -> None:
        """Stop the P2P node"""
        logger.info(f"Stopping P2P node {self.node_id}")
        
        self._running = False
        self._shutdown_event.set()
        
        # Close all peer connections
        close_tasks = []
        for peer_id, ws in list(self.peers.items()):
            close_tasks.append(asyncio.create_task(ws.close()))
        
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)
        
        # Stop server
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        
        logger.info("P2P node stopped")
    
    async def _handle_connection(
        self,
        websocket: websockets.WebSocketServerProtocol,
        path: str
    ) -> None:
        """Handle incoming WebSocket connection"""
        peer_id = None
        
        try:
            # Wait for identification message
            raw_msg = await websocket.recv()
            msg = NodeMessage.from_json(raw_msg)
            
            if msg.msg_type != MessageType.PING.value:
                logger.warning(f"Expected PING, got {msg.msg_type}")
                return
            
            peer_id = msg.sender_id
            
            # Check for duplicate connections
            if peer_id in self.peers:
                logger.warning(f"Duplicate connection from {peer_id}")
                await self.peers[peer_id].close()
            
            # Store connection
            self.peers[peer_id] = websocket
            self.peer_info[peer_id] = {
                "connected_at": time.time(),
                "last_seen": time.time(),
                "address": websocket.remote_address
            }
            
            # Send PONG
            pong_msg = NodeMessage(
                msg_id=secrets.token_hex(16),
                msg_type=MessageType.PONG.value,
                sender_id=self.node_id,
                recipient_id=peer_id,
                payload={"listen_port": self.listen_port},
                timestamp=time.time()
            )
            await websocket.send(pong_msg.to_json())
            
            logger.info(f"Peer connected: {peer_id}")
            
            # Handle messages
            async for raw_msg in websocket:
                await self._process_message(raw_msg, peer_id)
                
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Connection closed: {peer_id}")
        except Exception as e:
            logger.error(f"Error handling connection: {e}")
        finally:
            if peer_id and peer_id in self.peers:
                del self.peers[peer_id]
                logger.info(f"Peer disconnected: {peer_id}")
    
    async def _connect_to_peer(self, address: str) -> bool:
        """Connect to a peer at given address"""
        try:
            # Address format: "host:port"
            if ":" not in address:
                address = f"{address}:10000"
            
            uri = f"ws://{address}"
            
            logger.info(f"Connecting to peer: {uri}")
            
            websocket = await connect(uri)
            
            # Send PING with our ID
            ping_msg = NodeMessage(
                msg_id=secrets.token_hex(16),
                msg_type=MessageType.PING.value,
                sender_id=self.node_id,
                recipient_id=None,
                payload={"listen_port": self.listen_port},
                timestamp=time.time()
            )
            await websocket.send(ping_msg.to_json())
            
            # Wait for PONG
            raw_msg = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            msg = NodeMessage.from_json(raw_msg)
            
            if msg.msg_type != MessageType.PONG.value:
                logger.warning(f"Expected PONG, got {msg.msg_type}")
                await websocket.close()
                return False
            
            peer_id = msg.sender_id
            
            # Store connection
            self.peers[peer_id] = websocket
            self.peer_info[peer_id] = {
                "connected_at": time.time(),
                "last_seen": time.time(),
                "address": address,
                "listen_port": msg.payload.get("listen_port", 10000)
            }
            
            logger.info(f"Connected to peer: {peer_id}")
            
            # Start message handler for this connection
            asyncio.create_task(self._handle_peer_messages(peer_id, websocket))
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect to {address}: {e}")
            return False
    
    async def _handle_peer_messages(
        self,
        peer_id: str,
        websocket: websockets.WebSocketClientProtocol
    ) -> None:
        """Handle messages from a connected peer"""
        try:
            async for raw_msg in websocket:
                await self._process_message(raw_msg, peer_id)
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Connection to {peer_id} closed")
        except Exception as e:
            logger.error(f"Error handling messages from {peer_id}: {e}")
        finally:
            if peer_id in self.peers:
                del self.peers[peer_id]
    
    async def _process_message(self, raw_msg: str, from_peer: str) -> None:
        """Process an incoming message"""
        try:
            msg = NodeMessage.from_json(raw_msg)
            
            # Update peer info
            if from_peer in self.peer_info:
                self.peer_info[from_peer]["last_seen"] = time.time()
            
            # Deduplication
            if msg.msg_id in self.received_msgs:
                return
            self.received_msgs.add(msg.msg_id)
            
            # Limit deduplication set size
            if len(self.received_msgs) > 10000:
                self.received_msgs = set(list(self.received_msgs)[-5000:])
            
            # Route message
            msg_type = MessageType(msg.msg_type)
            
            # Handle special message types
            if msg_type == MessageType.BROADCAST and msg.recipient_id is None:
                # Gossip: forward to other peers
                await self._gossip_message(raw_msg, exclude=[from_peer])
            
            # Call registered handlers
            for handler in self.message_handlers.get(msg_type, []):
                try:
                    asyncio.create_task(handler(msg, from_peer))
                except Exception as e:
                    logger.error(f"Handler error: {e}")
                    
        except Exception as e:
            logger.error(f"Error processing message: {e}")
    
    async def broadcast(self, msg_type: MessageType, payload: dict) -> int:
        """
        Broadcast message to all connected peers.
        
        Returns number of peers message was sent to.
        """
        msg = NodeMessage(
            msg_id=secrets.token_hex(16),
            msg_type=msg_type.value,
            sender_id=self.node_id,
            recipient_id=None,
            payload=payload,
            timestamp=time.time()
        )
        
        return await self._gossip_message(msg.to_json())
    
    async def send_direct(
        self,
        recipient_id: str,
        msg_type: MessageType,
        payload: dict
    ) -> bool:
        """Send direct message to specific peer"""
        if recipient_id not in self.peers:
            logger.warning(f"Peer not connected: {recipient_id}")
            return False
        
        msg = NodeMessage(
            msg_id=secrets.token_hex(16),
            msg_type=msg_type.value,
            sender_id=self.node_id,
            recipient_id=recipient_id,
            payload=payload,
            timestamp=time.time()
        )
        
        try:
            await self.peers[recipient_id].send(msg.to_json())
            return True
        except Exception as e:
            logger.error(f"Failed to send to {recipient_id}: {e}")
            return False
    
    async def _gossip_message(
        self,
        raw_msg: str,
        exclude: List[str] = None
    ) -> int:
        """Gossip message to peers (exclude some)"""
        exclude = exclude or []
        sent = 0
        
        for peer_id, ws in list(self.peers.items()):
            if peer_id in exclude:
                continue
            
            try:
                await ws.send(raw_msg)
                sent += 1
            except Exception as e:
                logger.error(f"Failed to gossip to {peer_id}: {e}")
        
        return sent
    
    def on_message(
        self,
        msg_type: MessageType
    ) -> Callable:
        """Decorator to register message handler"""
        def decorator(func: Callable) -> Callable:
            self.message_handlers[msg_type].append(func)
            return func
        return decorator
    
    async def _maintenance_loop(self) -> None:
        """Periodic maintenance tasks"""
        while self._running:
            try:
                # Reconnect to bootstrap peers if needed
                for peer_addr in self.bootstrap_peers:
                    # Extract peer_id from address if possible
                    # For now, just check if we need more connections
                    pass
                
                # Clean up old message IDs
                if len(self.received_msgs) > 5000:
                    self.received_msgs = set(list(self.received_msgs)[-2500:])
                
            except Exception as e:
                logger.error(f"Maintenance error: {e}")
            
            await asyncio.sleep(30)
    
    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats to peers"""
        while self._running:
            try:
                # Send PING to all peers
                for peer_id in list(self.peers.keys()):
                    ping_msg = NodeMessage(
                        msg_id=secrets.token_hex(8),
                        msg_type=MessageType.PING.value,
                        sender_id=self.node_id,
                        recipient_id=peer_id,
                        payload={"heartbeat": True},
                        timestamp=time.time()
                    )
                    try:
                        await self.peers[peer_id].send(ping_msg.to_json())
                    except Exception:
                        # Peer disconnected
                        if peer_id in self.peers:
                            del self.peers[peer_id]
                
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
            
            await asyncio.sleep(60)  # Every minute
    
    def get_peer_count(self) -> int:
        """Get number of connected peers"""
        return len(self.peers)
    
    def get_peer_list(self) -> List[str]:
        """Get list of connected peer IDs"""
        return list(self.peers.keys())
    
    def get_status(self) -> dict:
        """Get node status"""
        return {
            "node_id": self.node_id,
            "listen_address": f"{self.listen_host}:{self.listen_port}",
            "peer_count": self.get_peer_count(),
            "peers": self.get_peer_list(),
            "running": self._running
        }
