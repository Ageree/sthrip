"""Shared helpers and service singletons for the Sthrip API."""

import logging
import threading
from typing import Optional

from sthrip.config import get_settings
from sthrip.db.database import get_db
from sthrip.services.wallet_service import WalletService
from sthrip.services.deposit_monitor import DepositMonitor

logger = logging.getLogger("sthrip")

_UNKNOWN_IP = "unknown"


def get_client_ip(request) -> str:
    """Return the client IP address from a Starlette Request.

    Always returns a plain ``str`` — never ``None``.  Falls back to
    ``"unknown"`` when the request object is ``None``, ``request.client``
    is ``None``, or ``request.client.host`` is falsy (empty string / None).

    Args:
        request: A Starlette ``Request`` instance, or ``None``.

    Returns:
        The client IP string, or ``"unknown"``.
    """
    if request is None:
        return _UNKNOWN_IP
    client = request.client
    if client is None:
        return _UNKNOWN_IP
    host = client.host
    if not host:
        return _UNKNOWN_IP
    return host


def get_hub_mode() -> str:
    return get_settings().hub_mode


_wallet_lock = threading.Lock()
_wallet_service = None


def get_wallet_service() -> WalletService:
    """Get or create the WalletService singleton (thread-safe)."""
    global _wallet_service
    if _wallet_service is None:
        with _wallet_lock:
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
        min_confirmations=get_settings().monero_min_confirmations,
        poll_interval=get_settings().deposit_poll_interval,
    )
    monitor.load_persisted_height()
    return monitor
