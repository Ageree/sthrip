"""
Tests for split repository modules and backward-compatible re-exports.

TDD Red phase: these tests fail until the new per-entity modules exist.
"""

import pytest


# ---------------------------------------------------------------------------
# 1. New module imports — each class lives in its own file
# ---------------------------------------------------------------------------

class TestAgentRepoModule:
    def test_import_agent_repository(self):
        from sthrip.db.agent_repo import AgentRepository
        assert AgentRepository is not None

    def test_import_get_hmac_secret(self):
        from sthrip.db.agent_repo import _get_hmac_secret
        assert callable(_get_hmac_secret)

    def test_agent_repository_has_expected_methods(self):
        from sthrip.db.agent_repo import AgentRepository
        for method in (
            "create_agent",
            "get_by_api_key",
            "get_by_id",
            "get_by_name",
            "list_agents",
            "update_last_seen",
            "get_webhook_secret",
            "update_wallet_addresses",
        ):
            assert hasattr(AgentRepository, method), f"Missing method: {method}"


class TestTransactionRepoModule:
    def test_import_transaction_repository(self):
        from sthrip.db.transaction_repo import TransactionRepository
        assert TransactionRepository is not None

    def test_transaction_repository_has_expected_methods(self):
        from sthrip.db.transaction_repo import TransactionRepository
        for method in (
            "create",
            "get_by_hash",
            "list_by_agent",
            "count_by_agent",
            "confirm_transaction",
            "get_volume_by_agent",
        ):
            assert hasattr(TransactionRepository, method), f"Missing method: {method}"


class TestEscrowRepoModule:
    def test_import_escrow_repository(self):
        from sthrip.db.escrow_repo import EscrowRepository
        assert EscrowRepository is not None

    def test_escrow_repository_has_expected_methods(self):
        from sthrip.db.escrow_repo import EscrowRepository
        for method in (
            "create",
            "get_by_id",
            "get_by_hash",
            "list_by_agent",
            "fund_deal",
            "mark_delivered",
            "release",
            "open_dispute",
            "arbitrate",
        ):
            assert hasattr(EscrowRepository, method), f"Missing method: {method}"


class TestChannelRepoModule:
    def test_import_channel_repository(self):
        from sthrip.db.channel_repo import ChannelRepository
        assert ChannelRepository is not None

    def test_channel_repository_has_expected_methods(self):
        from sthrip.db.channel_repo import ChannelRepository
        for method in (
            "create",
            "get_by_id",
            "get_by_hash",
            "list_by_agent",
            "fund_channel",
            "update_state",
            "close_channel",
        ):
            assert hasattr(ChannelRepository, method), f"Missing method: {method}"


class TestWebhookRepoModule:
    def test_import_webhook_repository(self):
        from sthrip.db.webhook_repo import WebhookRepository
        assert WebhookRepository is not None

    def test_webhook_repository_has_expected_methods(self):
        from sthrip.db.webhook_repo import WebhookRepository
        for method in (
            "create_event",
            "get_by_id",
            "get_by_id_for_update",
            "get_pending_events",
            "mark_delivered",
            "mark_failed",
            "schedule_retry",
        ):
            assert hasattr(WebhookRepository, method), f"Missing method: {method}"


class TestReputationRepoModule:
    def test_import_reputation_repository(self):
        from sthrip.db.reputation_repo import ReputationRepository
        assert ReputationRepository is not None

    def test_reputation_repository_has_expected_methods(self):
        from sthrip.db.reputation_repo import ReputationRepository
        for method in (
            "get_by_agent",
            "record_transaction",
            "record_dispute",
            "get_leaderboard",
        ):
            assert hasattr(ReputationRepository, method), f"Missing method: {method}"


class TestBalanceRepoModule:
    def test_import_balance_repository(self):
        from sthrip.db.balance_repo import BalanceRepository
        assert BalanceRepository is not None

    def test_balance_repository_has_expected_methods(self):
        from sthrip.db.balance_repo import BalanceRepository
        for method in (
            "get_or_create",
            "get_available",
            "deposit",
            "deduct",
            "credit",
            "add_pending",
            "clear_pending_on_confirm",
            "set_deposit_address",
        ):
            assert hasattr(BalanceRepository, method), f"Missing method: {method}"


class TestPendingWithdrawalRepoModule:
    def test_import_pending_withdrawal_repository(self):
        from sthrip.db.pending_withdrawal_repo import PendingWithdrawalRepository
        assert PendingWithdrawalRepository is not None

    def test_pending_withdrawal_repository_has_expected_methods(self):
        from sthrip.db.pending_withdrawal_repo import PendingWithdrawalRepository
        for method in (
            "create",
            "get_by_id",
            "get_stale_pending",
            "get_pending",
            "mark_completed",
            "mark_failed",
            "mark_needs_review",
        ):
            assert hasattr(PendingWithdrawalRepository, method), f"Missing method: {method}"


class TestSystemStateRepoModule:
    def test_import_system_state_repository(self):
        from sthrip.db.system_state_repo import SystemStateRepository
        assert SystemStateRepository is not None

    def test_system_state_repository_has_expected_methods(self):
        from sthrip.db.system_state_repo import SystemStateRepository
        for method in ("get", "set"):
            assert hasattr(SystemStateRepository, method), f"Missing method: {method}"


class TestRepoBaseModule:
    def test_import_max_query_limit(self):
        from sthrip.db._repo_base import _MAX_QUERY_LIMIT
        assert isinstance(_MAX_QUERY_LIMIT, int)
        assert _MAX_QUERY_LIMIT > 0

    def test_max_query_limit_value(self):
        from sthrip.db._repo_base import _MAX_QUERY_LIMIT
        assert _MAX_QUERY_LIMIT == 500


# ---------------------------------------------------------------------------
# 2. Backward-compatibility: original sthrip.db.repository still exports all
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """sthrip.db.repository must keep exporting everything."""

    def test_agent_repository_re_exported(self):
        from sthrip.db.repository import AgentRepository
        assert AgentRepository is not None

    def test_transaction_repository_re_exported(self):
        from sthrip.db.repository import TransactionRepository
        assert TransactionRepository is not None

    def test_escrow_repository_re_exported(self):
        from sthrip.db.repository import EscrowRepository
        assert EscrowRepository is not None

    def test_channel_repository_re_exported(self):
        from sthrip.db.repository import ChannelRepository
        assert ChannelRepository is not None

    def test_webhook_repository_re_exported(self):
        from sthrip.db.repository import WebhookRepository
        assert WebhookRepository is not None

    def test_reputation_repository_re_exported(self):
        from sthrip.db.repository import ReputationRepository
        assert ReputationRepository is not None

    def test_balance_repository_re_exported(self):
        from sthrip.db.repository import BalanceRepository
        assert BalanceRepository is not None

    def test_pending_withdrawal_repository_re_exported(self):
        from sthrip.db.repository import PendingWithdrawalRepository
        assert PendingWithdrawalRepository is not None

    def test_system_state_repository_re_exported(self):
        from sthrip.db.repository import SystemStateRepository
        assert SystemStateRepository is not None

    def test_max_query_limit_re_exported(self):
        from sthrip.db.repository import _MAX_QUERY_LIMIT
        assert _MAX_QUERY_LIMIT == 500

    def test_get_hmac_secret_re_exported(self):
        from sthrip.db.repository import _get_hmac_secret
        assert callable(_get_hmac_secret)


# ---------------------------------------------------------------------------
# 3. Identity: new module and repository.py reference the SAME class object
# ---------------------------------------------------------------------------

class TestClassIdentity:
    """Classes imported from new modules must be the exact same objects
    as those re-exported from sthrip.db.repository (not copies)."""

    def test_agent_repository_same_object(self):
        from sthrip.db.agent_repo import AgentRepository as New
        from sthrip.db.repository import AgentRepository as Old
        assert New is Old

    def test_transaction_repository_same_object(self):
        from sthrip.db.transaction_repo import TransactionRepository as New
        from sthrip.db.repository import TransactionRepository as Old
        assert New is Old

    def test_escrow_repository_same_object(self):
        from sthrip.db.escrow_repo import EscrowRepository as New
        from sthrip.db.repository import EscrowRepository as Old
        assert New is Old

    def test_channel_repository_same_object(self):
        from sthrip.db.channel_repo import ChannelRepository as New
        from sthrip.db.repository import ChannelRepository as Old
        assert New is Old

    def test_webhook_repository_same_object(self):
        from sthrip.db.webhook_repo import WebhookRepository as New
        from sthrip.db.repository import WebhookRepository as Old
        assert New is Old

    def test_reputation_repository_same_object(self):
        from sthrip.db.reputation_repo import ReputationRepository as New
        from sthrip.db.repository import ReputationRepository as Old
        assert New is Old

    def test_balance_repository_same_object(self):
        from sthrip.db.balance_repo import BalanceRepository as New
        from sthrip.db.repository import BalanceRepository as Old
        assert New is Old

    def test_pending_withdrawal_repository_same_object(self):
        from sthrip.db.pending_withdrawal_repo import PendingWithdrawalRepository as New
        from sthrip.db.repository import PendingWithdrawalRepository as Old
        assert New is Old

    def test_system_state_repository_same_object(self):
        from sthrip.db.system_state_repo import SystemStateRepository as New
        from sthrip.db.repository import SystemStateRepository as Old
        assert New is Old
