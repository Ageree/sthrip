"""
P2P Network for MPC Nodes

WebSocket-based communication between MPC relayer nodes.
Supports gossip protocol and direct messaging.
"""

from .node import P2PNode, NodeMessage, MessageType
from .gossip import GossipProtocol
from .discovery import PeerDiscovery

__all__ = [
    "P2PNode",
    "NodeMessage", 
    "MessageType",
    "GossipProtocol",
    "PeerDiscovery",
]
