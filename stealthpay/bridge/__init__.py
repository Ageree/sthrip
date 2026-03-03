"""
Cross-Chain Bridge Module: ETH↔XMR

Phase 2: MPC-based bridge for trustless cross-chain transfers.
"""

from .contracts.eth_bridge import EthereumBridgeContract
from .relayers.mpc_node import MPCRelayerNode
from .relayers.coordinator import BridgeCoordinator

__all__ = [
    "EthereumBridgeContract",
    "MPCRelayerNode",
    "BridgeCoordinator",
]
