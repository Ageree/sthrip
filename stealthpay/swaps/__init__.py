"""
StealthPay Atomic Swaps Module

Реализация атомарных свопов между различными криптовалютами.
Phase 1: BTC ↔ XMR через HTLC + 2-of-2 multi-sig
"""

from .btc.htlc import BitcoinHTLC
from .btc.rpc_client import BitcoinRPCClient
from .btc.watcher import BitcoinWatcher
from .xmr.multisig import MoneroMultisig
from .xmr.wallet import MoneroWallet

__all__ = [
    "BitcoinHTLC",
    "BitcoinRPCClient",
    "BitcoinWatcher",
    "MoneroMultisig",
    "MoneroWallet",
]
