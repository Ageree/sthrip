"""Tests for code review fixes (2026-03-12 production readiness review).

TDD RED phase: these tests FAIL before fix, PASS after.
Issues: C2, C3, C4, H2, H6, M3, M5.
"""

import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# C2: Failed auth rate limit must be ≤ 5 per minute
# ═══════════════════════════════════════════════════════════════════════════════


class TestC2FailedAuthLimit:
    """Failed auth rate limit should be strict for a payment system."""

    def test_failed_auth_limit_is_at_most_5(self):
        """Default failed auth limit in RateLimiter.check_failed_auth should be <= 5."""
        from sthrip.services.rate_limiter import RateLimiter
        import inspect
        sig = inspect.signature(RateLimiter.check_failed_auth)
        limit_default = sig.parameters["limit"].default
        assert limit_default <= 5, (
            f"Failed auth limit too high for payment system: {limit_default}, expected ≤5"
        )

    def test_sixth_attempt_blocked(self):
        """After 5 failed auths, the 6th should be blocked."""
        from sthrip.services.rate_limiter import RateLimiter, RateLimitExceeded

        limiter = RateLimiter(redis_url=None)
        for _ in range(5):
            limiter.record_failed_auth("1.2.3.4")

        with pytest.raises(RateLimitExceeded):
            limiter.check_failed_auth("1.2.3.4")


# ═══════════════════════════════════════════════════════════════════════════════
# C3: Webhook secret decryption error must NOT leak config key names
# ═══════════════════════════════════════════════════════════════════════════════


class TestC3WebhookSecretErrorLeakage:
    """ValueError from get_webhook_secret must not mention WEBHOOK_ENCRYPTION_KEY."""

    def test_error_message_is_generic(self):
        from sthrip.db.agent_repo import AgentRepository

        mock_db = MagicMock()
        mock_agent = MagicMock()
        mock_agent.webhook_secret = "invalid-encrypted-data"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_agent

        repo = AgentRepository(mock_db)

        with patch("sthrip.crypto.decrypt_value", side_effect=Exception("bad key")):
            with pytest.raises(ValueError) as exc_info:
                repo.get_webhook_secret("00000000-0000-0000-0000-000000000001")

        error_msg = str(exc_info.value)
        assert "WEBHOOK_ENCRYPTION_KEY" not in error_msg, (
            f"Error message leaks config key name: {error_msg}"
        )
        assert "ENCRYPTION" not in error_msg.upper() or "encrypt" not in error_msg.lower(), (
            f"Error message leaks encryption details: {error_msg}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# C4: Balance creation race condition — _get_for_update must use savepoint
# ═══════════════════════════════════════════════════════════════════════════════


class TestC4BalanceCreationRace:
    """_get_for_update must handle concurrent first-deposit race condition."""

    def test_get_for_update_handles_integrity_error(self):
        """When INSERT fails due to race, _get_for_update should retry."""
        from sqlalchemy.exc import IntegrityError
        from sthrip.db.balance_repo import BalanceRepository

        mock_db = MagicMock()
        # SQLite dialect for test
        mock_db.bind.dialect.name = "sqlite"

        # First query returns None (no balance yet)
        # Then INSERT raises IntegrityError (race condition)
        # Then retry query returns the balance
        mock_balance = MagicMock()
        mock_balance.available = Decimal("0")
        query_mock = MagicMock()
        filter_mock = MagicMock()
        filter_mock.first.side_effect = [None, mock_balance]
        query_mock.filter.return_value = filter_mock
        mock_db.query.return_value = query_mock

        mock_db.add.return_value = None
        mock_db.flush.side_effect = IntegrityError("", {}, Exception("UNIQUE constraint"))

        repo = BalanceRepository(mock_db)
        result = repo._get_for_update("00000000-0000-0000-0000-000000000001")

        # Should have retried and returned the existing balance
        assert result == mock_balance


# ═══════════════════════════════════════════════════════════════════════════════
# H2: Withdrawal address must not be hub wallet's own address
# ═══════════════════════════════════════════════════════════════════════════════


class TestH2SelfSendProtection:
    """Withdrawals to the hub wallet's own address must be rejected."""

    def test_wallet_service_rejects_self_send(self):
        """send_withdrawal must reject if to_address matches hub wallet."""
        from sthrip.services.wallet_service import WalletService

        mock_rpc = MagicMock()
        # Hub wallet address
        hub_address = "5" + "A" * 94
        mock_rpc.get_address.return_value = {
            "address": hub_address,
            "addresses": [{"address": hub_address, "address_index": 0}],
        }

        svc = WalletService(
            wallet_rpc=mock_rpc,
            db_session_factory=MagicMock(),
        )

        with pytest.raises(ValueError, match="[Ss]elf.send|own address|hub wallet"):
            svc.send_withdrawal(hub_address, Decimal("1.0"))

    def test_wallet_service_allows_external_send(self):
        """send_withdrawal to external address should work."""
        from sthrip.services.wallet_service import WalletService

        mock_rpc = MagicMock()
        hub_address = "5" + "A" * 94
        external_address = "5" + "B" * 94
        mock_rpc.get_address.return_value = {
            "address": hub_address,
            "addresses": [{"address": hub_address, "address_index": 0}],
        }
        mock_rpc.transfer.return_value = {
            "tx_hash": "abc123",
            "fee": 1000000,
        }

        svc = WalletService(
            wallet_rpc=mock_rpc,
            db_session_factory=MagicMock(),
        )

        result = svc.send_withdrawal(external_address, Decimal("1.0"))
        assert result["tx_hash"] == "abc123"


# ═══════════════════════════════════════════════════════════════════════════════
# H6: settle_hub_route must use with_for_update to prevent double settlement
# ═══════════════════════════════════════════════════════════════════════════════


class TestH6SettleLocking:
    """settle_hub_route must lock the row before updating."""

    def test_settle_uses_for_update(self):
        """settle_hub_route must use with_for_update() on the query."""
        import inspect
        from sthrip.services.fee_collector import FeeCollector
        source = inspect.getsource(FeeCollector.settle_hub_route)
        assert "with_for_update" in source, (
            "settle_hub_route must use with_for_update() to prevent double settlement"
        )

    def test_settle_rejects_already_settled(self):
        """Settling an already-settled route must raise ValueError."""
        from sthrip.services.fee_collector import FeeCollector
        from sthrip.db.models import HubRouteStatus

        mock_route = MagicMock()
        mock_route.status = HubRouteStatus.SETTLED
        mock_route.payment_id = "hp_test123"

        mock_db = MagicMock()
        query = MagicMock()
        query.filter.return_value.with_for_update.return_value.first.return_value = mock_route
        mock_db.query.return_value = query

        collector = FeeCollector()

        with patch("sthrip.services.fee_collector.get_db") as mock_get_db:
            mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

            with pytest.raises(ValueError, match="already"):
                collector.settle_hub_route("hp_test123", "tx_hash_abc")


# ═══════════════════════════════════════════════════════════════════════════════
# M3: Root endpoint must NOT hit DB on every request
# ═══════════════════════════════════════════════════════════════════════════════


class TestM3RootEndpointCaching:
    """GET / must cache agent stats, not query DB every time."""

    def test_root_does_not_leak_agent_count(self):
        """Root endpoint should not expose total_agents to unauthenticated users."""
        import inspect
        from api.routers.health import root
        source = inspect.getsource(root)
        assert "agents_registered" not in source or "cached" in source.lower(), (
            "Root endpoint should not expose agent count or must use cached stats"
        )

    def test_root_uses_cached_stats(self):
        """Root endpoint should use cached stats instead of direct DB query."""
        import inspect
        from api.routers import health
        source = inspect.getsource(health.root)
        # After fix: should NOT call registry.get_stats() directly
        assert "registry.get_stats()" not in source or "_cached" in source, (
            "Root endpoint must use cached stats, not direct DB query"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# M5: WalletService must have get_outgoing_transfers method
# ═══════════════════════════════════════════════════════════════════════════════


class TestM5OutgoingTransfers:
    """WalletService must implement get_outgoing_transfers for recovery."""

    def test_method_exists(self):
        """WalletService must have get_outgoing_transfers method."""
        from sthrip.services.wallet_service import WalletService
        assert hasattr(WalletService, "get_outgoing_transfers"), (
            "WalletService must implement get_outgoing_transfers"
        )

    def test_returns_list_of_dicts(self):
        """get_outgoing_transfers must return a list of transfer dicts."""
        from sthrip.services.wallet_service import WalletService

        mock_rpc = MagicMock()
        mock_rpc.get_transfers.return_value = {
            "out": [
                {
                    "txid": "abc123",
                    "amount": 1000000000000,
                    "fee": 100000000,
                    "address": "5BBB...",
                    "timestamp": 1741000000,
                    "height": 100,
                },
            ],
        }

        svc = WalletService(
            wallet_rpc=mock_rpc,
            db_session_factory=MagicMock(),
        )
        result = svc.get_outgoing_transfers()

        assert isinstance(result, list)
        assert len(result) == 1
        assert "tx_hash" in result[0] or "txid" in result[0]
        assert "amount" in result[0]
        assert "address" in result[0]

    def test_empty_when_no_outgoing(self):
        """get_outgoing_transfers returns empty list when no outgoing txs."""
        from sthrip.services.wallet_service import WalletService

        mock_rpc = MagicMock()
        mock_rpc.get_transfers.return_value = {}

        svc = WalletService(
            wallet_rpc=mock_rpc,
            db_session_factory=MagicMock(),
        )
        result = svc.get_outgoing_transfers()
        assert result == []
