"""
Peer Discovery for MPC Network

Discovers and maintains connections to peers in the network.
"""

import asyncio
import random
import logging
from typing import List, Set, Dict
from dataclasses import dataclass
import time

from .node import P2PNode

logger = logging.getLogger(__name__)


@dataclass
class PeerInfo:
    """Information about a peer"""
    node_id: str
    address: str
    port: int
    last_seen: float
    rtt: float  # Round-trip time
    reliability: float  # 0-1 score


class PeerDiscovery:
    """
    Peer discovery service.
    
    Maintains list of known peers and discovers new ones.
    """
    
    def __init__(
        self,
        p2p_node: P2PNode,
        bootstrap_nodes: List[str] = None
    ):
        self.p2p_node = p2p_node
        self.bootstrap_nodes = bootstrap_nodes or []
        
        # Known peers (not necessarily connected)
        self.known_peers: Dict[str, PeerInfo] = {}
        self.max_known_peers = 1000
        
        # Connection targets
        self.target_peer_count = 10
        self.min_peer_count = 3
        
    async def start(self) -> None:
        """Start discovery service"""
        logger.info("Starting peer discovery")
        
        # Add bootstrap nodes
        for addr in self.bootstrap_nodes:
            self._add_bootstrap_peer(addr)
        
        # Start discovery loop
        asyncio.create_task(self._discovery_loop())
    
    async def _discovery_loop(self) -> None:
        """Periodic peer discovery"""
        while self.p2p_node._running:
            try:
                # Check if we need more peers
                current_count = self.p2p_node.get_peer_count()
                
                if current_count < self.min_peer_count:
                    await self._discover_more_peers()
                
                # Refresh peer info
                await self._refresh_peer_info()
                
            except Exception as e:
                logger.error(f"Discovery error: {e}")
            
            await asyncio.sleep(60)  # Every minute
    
    async def _discover_more_peers(self) -> None:
        """Discover and connect to more peers"""
        # Try bootstrap nodes first
        for addr in self.bootstrap_nodes:
            success = await self.p2p_node._connect_to_peer(addr)
            if success:
                # Request peer list from bootstrap
                await self._request_peer_list(addr)
        
        # Try known peers
        candidates = [
            peer for peer in self.known_peers.values()
            if peer.node_id not in self.p2p_node.peers
        ]
        
        # Sort by reliability
        candidates.sort(key=lambda p: p.reliability, reverse=True)
        
        for peer in candidates[:5]:  # Try top 5
            address = f"{peer.address}:{peer.port}"
            await self.p2p_node._connect_to_peer(address)
    
    async def _request_peer_list(self, peer_address: str) -> None:
        """Request list of peers from a node"""
        # This would send a message to the peer
        # For now, just log
        logger.info(f"Requesting peer list from {peer_address}")
    
    def _add_bootstrap_peer(self, address: str) -> None:
        """Add bootstrap peer to known peers"""
        # Parse address
        if ":" in address:
            host, port_str = address.rsplit(":", 1)
            port = int(port_str)
        else:
            host = address
            port = 10000
        
        peer_id = f"bootstrap_{address.replace(':', '_')}"
        
        self.known_peers[peer_id] = PeerInfo(
            node_id=peer_id,
            address=host,
            port=port,
            last_seen=0,
            rtt=0.0,
            reliability=1.0
        )
    
    async def _refresh_peer_info(self) -> None:
        """Update peer information"""
        current_peers = self.p2p_node.get_peer_list()
        
        for peer_id in current_peers:
            if peer_id in self.p2p_node.peer_info:
                info = self.p2p_node.peer_info[peer_id]
                
                # Update known peers
                if peer_id not in self.known_peers:
                    addr = info.get("address", ("unknown", 0))
                    self.known_peers[peer_id] = PeerInfo(
                        node_id=peer_id,
                        address=addr[0] if isinstance(addr, tuple) else str(addr),
                        port=info.get("listen_port", 10000),
                        last_seen=info.get("last_seen", time.time()),
                        rtt=0.0,
                        reliability=1.0
                    )
                else:
                    # Update last seen
                    self.known_peers[peer_id].last_seen = info.get("last_seen", time.time())
    
    def get_random_peers(self, count: int) -> List[PeerInfo]:
        """Get random subset of known peers"""
        peers = list(self.known_peers.values())
        if len(peers) <= count:
            return peers
        return random.sample(peers, count)
    
    def get_reliable_peers(self, count: int) -> List[PeerInfo]:
        """Get most reliable peers"""
        peers = sorted(
            self.known_peers.values(),
            key=lambda p: p.reliability,
            reverse=True
        )
        return peers[:count]


class MDNSDiscovery:
    """
    mDNS-based peer discovery for local networks.
    
    Useful for local testing and LAN deployments.
    """
    
    def __init__(self, p2p_node: P2PNode, service_name: str = "_mpc._tcp"):
        self.p2p_node = p2p_node
        self.service_name = service_name
        self.zeroconf = None
        
    async def start(self) -> None:
        """Start mDNS discovery"""
        try:
            from zeroconf import Zeroconf, ServiceInfo
            
            self.zeroconf = Zeroconf()
            
            # Register our service
            info = ServiceInfo(
                self.service_name,
                f"{self.p2p_node.node_id}.{self.service_name}",
                addresses=[b"\x7f\x00\x00\x01"],  # 127.0.0.1
                port=self.p2p_node.listen_port,
                properties={"node_id": self.p2p_node.node_id}
            )
            
            self.zeroconf.register_service(info)
            logger.info(f"mDNS service registered: {self.service_name}")
            
        except ImportError:
            logger.warning("zeroconf not installed, mDNS discovery disabled")
        except Exception as e:
            logger.error(f"mDNS error: {e}")
    
    async def stop(self) -> None:
        """Stop mDNS discovery"""
        if self.zeroconf:
            self.zeroconf.close()


class DHTDiscovery:
    """
    DHT-based peer discovery for global network.
    
    Uses Kademlia DHT for decentralized peer discovery.
    """
    
    def __init__(self, p2p_node: P2PNode, bootstrap_nodes: List[tuple] = None):
        self.p2p_node = p2p_node
        self.bootstrap_nodes = bootstrap_nodes or []
        self.node = None
        
    async def start(self) -> None:
        """Start DHT"""
        try:
            from kademlia.network import Server
            
            self.node = Server()
            await self.node.listen(self.p2p_node.listen_port + 1)
            
            if self.bootstrap_nodes:
                await self.node.bootstrap(self.bootstrap_nodes)
            
            # Store our info in DHT
            await self.node.set(
                f"mpc:node:{self.p2p_node.node_id}",
                f"{self.p2p_node.listen_host}:{self.p2p_node.listen_port}"
            )
            
            logger.info("DHT discovery started")
            
        except ImportError:
            logger.warning("kademlia not installed, DHT discovery disabled")
        except Exception as e:
            logger.error(f"DHT error: {e}")
    
    async def find_peers(self) -> List[str]:
        """Find peers via DHT"""
        # This would query DHT for peer records
        return []
