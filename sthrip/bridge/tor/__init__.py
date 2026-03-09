"""
Tor Integration for Sthrip

Provides anonymous networking for MPC nodes:
- Hidden services
- Tor proxy support
- Circuit management
- Stream isolation
"""

from .hidden_service import TorHiddenService, HiddenServiceConfig
from .p2p_transport import TorP2PTransport, OnionAddressBook

__all__ = [
    "TorHiddenService",
    "HiddenServiceConfig",
    "TorP2PTransport",
    "OnionAddressBook"
]
