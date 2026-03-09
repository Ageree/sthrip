"""Tests for WalletService and piconero conversion — TDD RED phase"""
import pytest
from decimal import Decimal
from uuid import uuid4
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance,
    AgentTier, RateLimitTier, PrivacyLevel,
)
from sthrip.db.repository import BalanceRepository


# ═══════════════════════════════════════════════════════════════════════════════
# PICONERO CONVERSION
# ═══════════════════════════════════════════════════════════════════════════════

class TestPiconeroConversion:
    """Test XMR <-> piconero (atomic units) conversion."""

    def test_xmr_to_piconero_whole_number(self):
        from sthrip.services.wallet_service import xmr_to_piconero
        assert xmr_to_piconero(Decimal("1")) == 1_000_000_000_000

    def test_xmr_to_piconero_fractional(self):
        from sthrip.services.wallet_service import xmr_to_piconero
        assert xmr_to_piconero(Decimal("0.5")) == 500_000_000_000

    def test_xmr_to_piconero_small_amount(self):
        from sthrip.services.wallet_service import xmr_to_piconero
        assert xmr_to_piconero(Decimal("0.000000000001")) == 1

    def test_xmr_to_piconero_zero(self):
        from sthrip.services.wallet_service import xmr_to_piconero
        assert xmr_to_piconero(Decimal("0")) == 0

    def test_piconero_to_xmr_whole(self):
        from sthrip.services.wallet_service import piconero_to_xmr
        assert piconero_to_xmr(1_000_000_000_000) == Decimal("1")

    def test_piconero_to_xmr_fractional(self):
        from sthrip.services.wallet_service import piconero_to_xmr
        assert piconero_to_xmr(500_000_000_000) == Decimal("0.5")

    def test_piconero_to_xmr_one_piconero(self):
        from sthrip.services.wallet_service import piconero_to_xmr
        assert piconero_to_xmr(1) == Decimal("0.000000000001")

    def test_piconero_to_xmr_zero(self):
        from sthrip.services.wallet_service import piconero_to_xmr
        assert piconero_to_xmr(0) == Decimal("0")

    def test_roundtrip_conversion(self):
        from sthrip.services.wallet_service import xmr_to_piconero, piconero_to_xmr
        original = Decimal("3.141592653589")
        assert piconero_to_xmr(xmr_to_piconero(original)) == original

    def test_xmr_to_piconero_negative_raises(self):
        from sthrip.services.wallet_service import xmr_to_piconero
        with pytest.raises(ValueError, match="negative"):
            xmr_to_piconero(Decimal("-1"))


# ═══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════════════

_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
]


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=_TEST_TABLES)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def agent(db_session):
    agent = Agent(
        agent_name="test-wallet-agent",
        api_key_hash="testhash",
        tier=AgentTier.FREE,
        rate_limit_tier=RateLimitTier.STANDARD,
        privacy_level=PrivacyLevel.MEDIUM,
        is_active=True,
        xmr_address="test_xmr_address_123",
    )
    db_session.add(agent)
    db_session.flush()
    return agent


@pytest.fixture
def mock_wallet_rpc():
    """Mock MoneroWalletRPC for testing without a running daemon."""
    rpc = MagicMock()
    rpc.create_address.return_value = {
        "address": "5FakeSubaddress123abc",
        "address_index": 1,
    }
    rpc.get_balance.return_value = {
        "balance": 5_000_000_000_000,
        "unlocked_balance": 4_500_000_000_000,
    }
    rpc.get_height.return_value = 100_000
    rpc.get_address.return_value = {
        "address": "5FakePrimaryAddress",
        "addresses": [
            {"address_index": 0, "address": "5FakePrimaryAddress"},
            {"address_index": 1, "address": "5FakeSubaddress123abc"},
        ],
    }
    rpc.transfer.return_value = {
        "tx_hash": "abc123def456",
        "fee": 50_000_000,
        "amount": 1_000_000_000_000,
    }
    rpc.incoming_transfers.return_value = [
        {
            "tx_hash": "tx_incoming_001",
            "amount": 2_000_000_000_000,
            "block_height": 99_990,
            "subaddr_index": {"major": 0, "minor": 1},
            "key_image": "fake_key_image_001",
            "spent": False,
            "unlocked": True,
        },
    ]
    rpc.get_transfers.return_value = {
        "in": [
            {
                "txid": "tx_incoming_001",
                "amount": 2_000_000_000_000,
                "confirmations": 15,
                "height": 99_990,
                "subaddr_index": {"major": 0, "minor": 1},
                "address": "5FakeSubaddress123abc",
            },
        ],
    }
    return rpc


@pytest.fixture
def wallet_service(mock_wallet_rpc, db_session):
    from contextlib import contextmanager
    from sthrip.services.wallet_service import WalletService

    @contextmanager
    def _test_db():
        yield db_session

    return WalletService(
        wallet_rpc=mock_wallet_rpc,
        db_session_factory=_test_db,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# WALLET SERVICE — DEPOSIT ADDRESS
# ═══════════════════════════════════════════════════════════════════════════════

class TestWalletServiceDepositAddress:
    """Test deposit address generation and caching."""

    def test_creates_new_deposit_address(self, wallet_service, mock_wallet_rpc, agent, db_session):
        address = wallet_service.get_or_create_deposit_address(agent.id)
        assert address == "5FakeSubaddress123abc"
        mock_wallet_rpc.create_address.assert_called_once_with(
            account_index=0, label=str(agent.id)
        )

    def test_saves_deposit_address_to_db(self, wallet_service, agent, db_session):
        wallet_service.get_or_create_deposit_address(agent.id)
        repo = BalanceRepository(db_session)
        balance = repo.get_or_create(agent.id)
        assert balance.deposit_address == "5FakeSubaddress123abc"

    def test_returns_existing_address_from_db(self, wallet_service, mock_wallet_rpc, agent, db_session):
        repo = BalanceRepository(db_session)
        repo.set_deposit_address(agent.id, "5ExistingAddress")
        db_session.flush()

        address = wallet_service.get_or_create_deposit_address(agent.id)
        assert address == "5ExistingAddress"
        mock_wallet_rpc.create_address.assert_not_called()

    def test_different_agents_get_different_addresses(self, wallet_service, mock_wallet_rpc, db_session):
        agent1 = Agent(
            agent_name="agent-1",
            api_key_hash="hash1",
            tier=AgentTier.FREE,
            rate_limit_tier=RateLimitTier.STANDARD,
            privacy_level=PrivacyLevel.MEDIUM,
            is_active=True,
        )
        agent2 = Agent(
            agent_name="agent-2",
            api_key_hash="hash2",
            tier=AgentTier.FREE,
            rate_limit_tier=RateLimitTier.STANDARD,
            privacy_level=PrivacyLevel.MEDIUM,
            is_active=True,
        )
        db_session.add_all([agent1, agent2])
        db_session.flush()

        mock_wallet_rpc.create_address.side_effect = [
            {"address": "5Addr_Agent1", "address_index": 1},
            {"address": "5Addr_Agent2", "address_index": 2},
        ]

        addr1 = wallet_service.get_or_create_deposit_address(agent1.id)
        addr2 = wallet_service.get_or_create_deposit_address(agent2.id)
        assert addr1 != addr2


# ═══════════════════════════════════════════════════════════════════════════════
# WALLET SERVICE — WITHDRAWAL
# ═══════════════════════════════════════════════════════════════════════════════

class TestWalletServiceWithdrawal:
    """Test XMR withdrawal via wallet RPC."""

    def test_send_withdrawal_calls_rpc(self, wallet_service, mock_wallet_rpc):
        result = wallet_service.send_withdrawal("5DestAddr", Decimal("1.5"))
        mock_wallet_rpc.transfer.assert_called_once()
        call_args = mock_wallet_rpc.transfer.call_args
        assert call_args[1]["destination"] == "5DestAddr"

    def test_send_withdrawal_returns_tx_info(self, wallet_service):
        result = wallet_service.send_withdrawal("5DestAddr", Decimal("1.5"))
        assert "tx_hash" in result
        assert "fee" in result
        assert "amount" in result

    def test_send_withdrawal_converts_to_piconero(self, wallet_service, mock_wallet_rpc):
        wallet_service.send_withdrawal("5DestAddr", Decimal("2.5"))
        call_args = mock_wallet_rpc.transfer.call_args
        assert call_args[1]["amount"] == 2_500_000_000_000

    def test_send_withdrawal_fee_in_xmr(self, wallet_service):
        result = wallet_service.send_withdrawal("5DestAddr", Decimal("1"))
        # fee from mock is 50_000_000 piconero = 0.00005 XMR
        assert result["fee"] == Decimal("0.00005")

    def test_send_withdrawal_rpc_error_propagates(self, wallet_service, mock_wallet_rpc):
        from sthrip.wallet import WalletRPCError
        mock_wallet_rpc.transfer.side_effect = WalletRPCError("Transfer failed")
        with pytest.raises(WalletRPCError, match="Transfer failed"):
            wallet_service.send_withdrawal("5DestAddr", Decimal("1"))


# ═══════════════════════════════════════════════════════════════════════════════
# WALLET SERVICE — INCOMING TRANSFERS
# ═══════════════════════════════════════════════════════════════════════════════

class TestWalletServiceIncomingTransfers:
    """Test incoming transfer retrieval."""

    def test_get_incoming_transfers(self, wallet_service):
        transfers = wallet_service.get_incoming_transfers()
        assert len(transfers) == 1

    def test_incoming_transfer_amounts_in_xmr(self, wallet_service):
        transfers = wallet_service.get_incoming_transfers()
        assert transfers[0]["amount"] == Decimal("2")

    def test_incoming_transfer_confirmations(self, wallet_service):
        transfers = wallet_service.get_incoming_transfers()
        # height=99990, current_height=100000 -> 10 confirmations
        assert transfers[0]["confirmations"] == 10

    def test_incoming_transfers_with_min_height(self, wallet_service, mock_wallet_rpc):
        wallet_service.get_incoming_transfers(min_height=99_995)
        mock_wallet_rpc.incoming_transfers.assert_called_once()
        # Transfer at height 99990 < min_height 99995, should be filtered out
        transfers = wallet_service.get_incoming_transfers(min_height=99_995)
        assert transfers == []

    def test_incoming_transfers_empty(self, wallet_service, mock_wallet_rpc):
        mock_wallet_rpc.incoming_transfers.return_value = []
        transfers = wallet_service.get_incoming_transfers()
        assert transfers == []

    def test_incoming_transfers_skips_primary_address(self, wallet_service, mock_wallet_rpc):
        mock_wallet_rpc.incoming_transfers.return_value = [
            {
                "tx_hash": "tx_primary_001",
                "amount": 1_000_000_000_000,
                "block_height": 99_990,
                "subaddr_index": {"major": 0, "minor": 0},
                "key_image": "fake",
                "spent": False,
                "unlocked": True,
            },
        ]
        transfers = wallet_service.get_incoming_transfers()
        assert transfers == []


# ═══════════════════════════════════════════════════════════════════════════════
# WALLET SERVICE — WALLET INFO
# ═══════════════════════════════════════════════════════════════════════════════

class TestWalletServiceInfo:
    """Test wallet info retrieval for health check / admin."""

    def test_get_wallet_info(self, wallet_service):
        info = wallet_service.get_wallet_info()
        assert "balance" in info
        assert "unlocked_balance" in info
        assert "address" in info

    def test_wallet_info_balance_in_xmr(self, wallet_service):
        info = wallet_service.get_wallet_info()
        assert info["balance"] == Decimal("5")
        assert info["unlocked_balance"] == Decimal("4.5")
