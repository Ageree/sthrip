"""
Sthrip - Anonymous payments for AI Agents via Monero
"""

from .client import Sthrip
from .types import Payment, PaymentStatus, WalletInfo

# Atomic Swaps (Phase 1: BTC↔XMR)
from .swaps import (
    BitcoinHTLC,
    BitcoinRPCClient,
    BitcoinWatcher,
    MoneroMultisig,
    MoneroWallet,
)

__version__ = "0.1.0"
__all__ = [
    "Sthrip",
    "Payment",
    "PaymentStatus",
    "WalletInfo",
    # Atomic Swaps
    "BitcoinHTLC",
    "BitcoinRPCClient",
    "BitcoinWatcher",
    "MoneroMultisig",
    "MoneroWallet",
]
