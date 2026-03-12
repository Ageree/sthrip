"""
Network privacy - multiple nodes, rotation, proxy support
"""

import random
from typing import Any, List, Optional, Dict
from dataclasses import dataclass
from enum import Enum


class NodePriority(Enum):
    """Node selection priority"""
    RANDOM = "random"
    FASTEST = "fastest"
    CLOSEST = "closest"
    LEAST_USED = "least_used"


@dataclass
class MoneroNode:
    """Monero node configuration"""
    host: str
    port: int
    rpc_port: int
    is_tor: bool = False
    is_i2p: bool = False
    is_trusted: bool = False
    latency_ms: Optional[int] = None
    use_count: int = 0
    last_used: Optional[float] = None
    
    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"
    
    @property
    def rpc_url(self) -> str:
        return f"http://{self.host}:{self.rpc_port}"


class NodeManager:
    """
    Manages multiple Monero nodes for enhanced privacy.
    Rotates between nodes to prevent single-point analysis.
    """
    
    # Public remote nodes (for testing only - use your own in production!)
    DEFAULT_NODES: List[MoneroNode] = [
        # Community nodes - DO NOT USE FOR PRODUCTION
        # These are examples only
    ]
    
    def __init__(self, nodes: Optional[List[MoneroNode]] = None):
        self.nodes = nodes or []
        self.current_index = 0
        self.failed_nodes: set = set()
    
    def add_node(self, node: MoneroNode) -> None:
        """Add node to pool"""
        self.nodes.append(node)
    
    def get_node(
        self,
        strategy: NodePriority = NodePriority.RANDOM,
        exclude_failed: bool = True
    ) -> MoneroNode:
        """
        Select node based on strategy.
        
        Args:
            strategy: How to select node
            exclude_failed: Skip recently failed nodes
        
        Returns:
            Selected MoneroNode
        """
        available = self.nodes
        if exclude_failed:
            available = [n for n in self.nodes if n.host not in self.failed_nodes]
        
        if not available:
            raise RuntimeError("No available nodes")
        
        if strategy == NodePriority.RANDOM:
            node = random.choice(available)
        
        elif strategy == NodePriority.LEAST_USED:
            node = min(available, key=lambda n: n.use_count)
        
        elif strategy == NodePriority.FASTEST:
            # Filter nodes with known latency
            measured = [n for n in available if n.latency_ms is not None]
            if measured:
                node = min(measured, key=lambda n: n.latency_ms)
            else:
                node = random.choice(available)
        
        else:
            # Round-robin
            node = available[self.current_index % len(available)]
            self.current_index += 1
        
        node.use_count += 1
        return node
    
    def mark_failed(self, node: MoneroNode) -> None:
        """Mark node as temporarily failed"""
        self.failed_nodes.add(node.host)
    
    def mark_recovered(self, node: MoneroNode) -> None:
        """Mark node as recovered"""
        self.failed_nodes.discard(node.host)
    
    def rotate(self) -> MoneroNode:
        """Force rotate to next node"""
        if not self.nodes:
            raise RuntimeError("No nodes configured")
        
        self.current_index = (self.current_index + 1) % len(self.nodes)
        return self.nodes[self.current_index]


class ConnectionPool:
    """
    Pool of connections to different nodes.
    Automatically rotates for each request.
    """
    
    def __init__(self, node_manager: NodeManager, pool_size: int = 3):
        self.node_manager = node_manager
        self.pool_size = pool_size
        self.connections: Dict[str, Any] = {}

    def get_connection(self) -> Any:
        """Get connection from pool, rotating nodes"""
        node = self.node_manager.get_node(NodePriority.RANDOM)
        # Would return actual RPC connection here
        return node
    
    def broadcast_to_all(
        self,
        transaction_hex: str,
        timeout: int = 30
    ) -> List[bool]:
        """
        Broadcast transaction to multiple nodes simultaneously.
        Prevents single node from knowing it's the only recipient.
        """
        results = []
        nodes = self.node_manager.nodes[:self.pool_size]
        
        for node in nodes:
            try:
                # Would actually broadcast here
                results.append(True)
            except Exception:
                results.append(False)
        
        # If majority succeeded, consider it done
        return results


class ProxyRotator:
    """
    Rotate between different proxy servers.
    Can use VPN, SOCKS, or HTTP proxies.
    """
    
    def __init__(self):
        self.proxies: List[str] = []
        self.current = 0
    
    def add_proxy(self, proxy_url: str) -> None:
        """
        Add proxy to rotation.
        
        Format:
        - HTTP: http://user:pass@host:port
        - SOCKS5: socks5://user:pass@host:port
        """
        self.proxies.append(proxy_url)
    
    def get_proxy(self) -> Optional[str]:
        """Get next proxy in rotation"""
        if not self.proxies:
            return None
        
        proxy = self.proxies[self.current]
        self.current = (self.current + 1) % len(self.proxies)
        return proxy
    
    def get_all(self) -> List[str]:
        """Get all configured proxies"""
        return self.proxies.copy()


def generate_node_fingerprint(
    user_agent: str,
    timing_pattern: str,
    fee_preference: str
) -> str:
    """
    Generate fingerprint of node behavior.
    Used to detect if you're using unique settings that identify you.
    """
    data = f"{user_agent}:{timing_pattern}:{fee_preference}"
    import hashlib
    return hashlib.md5(data.encode()).hexdigest()[:8]


def check_fingerprint_uniformity(
    fingerprints: List[str]
) -> float:
    """
    Check how uniform your fingerprints are across transactions.
    Higher uniformity = better privacy.
    
    Returns:
        0.0 to 1.0, where 1.0 means perfectly uniform
    """
    if not fingerprints:
        return 0.0
    
    unique = len(set(fingerprints))
    total = len(fingerprints)
    
    # More unique = less uniform = worse
    uniformity = 1.0 - (unique / total)
    return uniformity
