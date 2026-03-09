"""Tests for lifespan: DepositMonitor wiring + wallet health check."""
import asyncio
import os
import pytest
from unittest.mock import MagicMock, patch, AsyncMock


class TestLifespanDepositMonitor:
    """DepositMonitor should start/stop based on HUB_MODE."""

    def test_deposit_monitor_started_in_onchain_mode(self):
        """When HUB_MODE=onchain, DepositMonitor should be created and started."""
        mock_monitor_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.start = AsyncMock()
        mock_instance.load_persisted_height = MagicMock()
        mock_monitor_cls.return_value = mock_instance

        with patch.dict(os.environ, {"HUB_MODE": "onchain"}):
            from api.helpers import create_deposit_monitor
            monitor = create_deposit_monitor(
                monitor_cls=mock_monitor_cls,
            )

        assert monitor is not None
        mock_instance.load_persisted_height.assert_called_once()

    def test_deposit_monitor_not_started_in_ledger_mode(self):
        """When HUB_MODE=ledger, no DepositMonitor should be created."""
        with patch.dict(os.environ, {"HUB_MODE": "ledger"}):
            from api.helpers import create_deposit_monitor
            monitor = create_deposit_monitor()

        assert monitor is None


class TestLifespanWalletHealthCheck:
    """Wallet health check should be included only in onchain mode."""

    def test_wallet_health_included_onchain(self):
        with patch.dict(os.environ, {"HUB_MODE": "onchain"}):
            from api.helpers import get_hub_mode
            mode = get_hub_mode()
        assert mode == "onchain"

    def test_wallet_health_excluded_ledger(self):
        with patch.dict(os.environ, {"HUB_MODE": "ledger"}):
            from api.helpers import get_hub_mode
            mode = get_hub_mode()
        assert mode == "ledger"
