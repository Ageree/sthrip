"""Shared helpers and service singletons for the Sthrip API."""

import os
import logging
from typing import Optional

from sthrip.db.database import get_db
from sthrip.services.wallet_service import WalletService
from sthrip.services.deposit_monitor import DepositMonitor

logger = logging.getLogger("sthrip")


def get_hub_mode() -> str:
    return os.getenv("HUB_MODE", "onchain")


_wallet_service = None


def get_wallet_service() -> WalletService:
    """Get or create the WalletService singleton."""
    global _wallet_service
    if _wallet_service is None:
        _wallet_service = WalletService.from_env(db_session_factory=get_db)
    return _wallet_service


def create_deposit_monitor(monitor_cls=None) -> Optional[DepositMonitor]:
    """Create and configure DepositMonitor if HUB_MODE=onchain."""
    if get_hub_mode() != "onchain":
        return None

    cls = monitor_cls or DepositMonitor
    monitor = cls(
        wallet_service=get_wallet_service(),
        db_session_factory=get_db,
        min_confirmations=int(os.getenv("MONERO_MIN_CONFIRMATIONS", "10")),
        poll_interval=int(os.getenv("DEPOSIT_POLL_INTERVAL", "30")),
    )
    monitor.load_persisted_height()
    return monitor
