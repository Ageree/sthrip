"""Tests for the 2-of-3 Monero multisig escrow system.

Tests cover:
  - Multisig escrow creation with 1% upfront fee
  - Round submission and state progression
  - Multisig state query
  - Default mode backward compatibility (hub-held)
  - Cosign and dispute flows
  - Validation errors
"""

import os
import contextlib
import pytest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance,
    HubRoute, FeeCollection, PendingWithdrawal, Transaction,
    EscrowDeal, EscrowMilestone, MultisigEscrow, MultisigRound,
)
from sthrip.services.multisig_coordinator import MultisigCoordinator


# Valid 95-char stagenet XMR address (base58 alphabet, starts with '5')
_VALID_XMR_ADDR = "5" + "a" * 94

# All tables needed for multisig escrow tests
_MULTISIG_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
    PendingWithdrawal.__table__,
    Transaction.__table__,
    EscrowDeal.__table__,
    EscrowMilestone.__table__,
    MultisigEscrow.__table__,
    MultisigRound.__table__,
]

# Modules where get_db must be patched (includes escrow + multisig router deps).
_GET_DB_MODULES = [
    "sthrip.db.database",
    "sthrip.services.agent_registry",
    "sthrip.services.fee_collector",
    "sthrip.services.webhook_service",
    "api.main_v2",
    "api.deps",
    "api.routers.health",
    "api.routers.agents",
    "api.routers.payments",
    "api.routers.balance",
    "api.routers.webhooks",
    "api.routers.escrow",
    "api.routers.multisig_escrow",
]

_RATE_LIMITER_MODULES = [
    "sthrip.services.rate_limiter",
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
]

_AUDIT_LOG_MODULES = [
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
    "api.routers.payments",
    "api.routers.balance",
    "api.routers.admin",
]


# ---------------------------------------------------------------------------
# SQLite timezone compatibility
# ---------------------------------------------------------------------------

def _naive_utc_now() -> datetime:
    """Return current UTC time as a naive datetime (no tzinfo)."""
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ms_engine():
    """In-memory SQLite engine with multisig-related tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_MULTISIG_TEST_TABLES)
    return engine


@pytest.fixture
def ms_session_factory(ms_engine):
    """Session factory bound to the multisig test engine."""
    return sessionmaker(bind=ms_engine, expire_on_commit=False)


@pytest.fixture
def ms_client(ms_engine, ms_session_factory):
    """FastAPI test client with all dependencies mocked, including multisig tables."""

    @contextmanager
    def get_test_db():
        session = ms_session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    mock_limiter = MagicMock()
    mock_limiter.check_rate_limit.return_value = None
    mock_limiter.check_ip_rate_limit.return_value = None
    mock_limiter.check_failed_auth.return_value = None
    mock_limiter.record_failed_auth.return_value = None
    mock_limiter.get_limit_status.return_value = {"requests_remaining": 100}

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy",
        "timestamp": "2026-03-03T00:00:00",
        "checks": {},
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HUB_MODE": "ledger"}))

        for mod in _GET_DB_MODULES:
            stack.enter_context(patch(f"{mod}.get_db", side_effect=get_test_db))
        stack.enter_context(patch("sthrip.db.database.create_tables"))

        for mod in _RATE_LIMITER_MODULES:
            stack.enter_context(
                patch(f"{mod}.get_rate_limiter", return_value=mock_limiter)
            )

        for mod in _AUDIT_LOG_MODULES:
            stack.enter_context(patch(f"{mod}.audit_log"))

        stack.enter_context(
            patch(
                "sthrip.services.monitoring.get_monitor",
                return_value=mock_monitor,
            )
        )
        stack.enter_context(
            patch(
                "sthrip.services.monitoring.setup_default_monitoring",
                return_value=mock_monitor,
            )
        )
        stack.enter_context(
            patch(
                "sthrip.services.webhook_service.get_webhook_service",
                return_value=mock_webhook,
            )
        )
        stack.enter_context(
            patch("sthrip.services.webhook_service.queue_webhook")
        )
        stack.enter_context(
            patch("sthrip.services.escrow_service.audit_log")
        )
        stack.enter_context(
            patch("sthrip.services.escrow_service.queue_webhook")
        )
        stack.enter_context(
            patch("sthrip.services.multisig_coordinator.audit_log")
        )
        stack.enter_context(
            patch("sthrip.services.multisig_coordinator.queue_webhook")
        )
        # SQLite returns naive datetimes
        stack.enter_context(
            patch("sthrip.services.escrow_service._now", side_effect=_naive_utc_now)
        )
        stack.enter_context(
            patch("sthrip.services.multisig_coordinator._now", side_effect=_naive_utc_now)
        )

        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)


def _register_agent(client: TestClient, name: str) -> str:
    """Register an agent and return its API key."""
    r = client.post("/v2/agents/register", json={
        "agent_name": name,
        "xmr_address": _VALID_XMR_ADDR,
    })
    assert r.status_code == 201, f"Registration of '{name}' failed: {r.text}"
    return r.json()["api_key"]


def _deposit(client: TestClient, api_key: str, amount: float) -> None:
    """Deposit funds into an agent's balance."""
    r = client.post(
        "/v2/balance/deposit",
        json={"amount": amount},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code in (200, 201), f"Deposit failed: {r.text}"


def _create_multisig_escrow(
    client: TestClient,
    buyer_key: str,
    seller_name: str,
    amount: float = 10.0,
) -> dict:
    """Create a multisig escrow and return the response dict."""
    r = client.post(
        "/v2/escrow",
        json={
            "seller_agent_name": seller_name,
            "amount": amount,
            "description": "Multisig escrow test",
            "mode": "multisig",
        },
        headers={"Authorization": f"Bearer {buyer_key}"},
    )
    assert r.status_code == 201, f"Multisig create failed: {r.text}"
    return r.json()


# ===========================================================================
# UNIT TESTS — MultisigCoordinator (service layer)
# ===========================================================================

class TestMultisigCoordinatorUnit:
    """Unit tests for the MultisigCoordinator service."""

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_create_collects_fee_upfront(self, _wh, _al, ms_session_factory):
        """Creating a multisig escrow should collect 1% fee upfront."""
        mock_wallet = MagicMock()
        mock_wallet.prepare_multisig.return_value = "hub_info_data"
        coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)

        session = ms_session_factory()
        try:
            # Create buyer and seller agents
            buyer = Agent(agent_name="unit-buyer", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="unit-seller", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            # Create balance for buyer
            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session,
                buyer_id=buyer.id,
                seller_id=seller.id,
                amount=Decimal("10"),
                description="Test deal",
            )
            session.commit()

            # Fee: 1% of 10 = 0.10
            assert result["fee_collected"] == "0.10000000"
            # Funded: 10 - 0.10 = 9.90
            assert result["funded_amount"] == "9.90000000"
            assert result["state"] == "setup_round_1"
            assert result["id"]  # MultisigEscrow ID
            assert result["escrow_deal_id"]  # EscrowDeal ID

            # Buyer balance should be deducted by full amount (10)
            session.refresh(balance)
            assert balance.available == Decimal("90")

        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_submit_round_advances_state(self, _wh, _al, ms_session_factory):
        """When all 3 participants submit round data, state should advance."""
        mock_wallet = MagicMock()
        mock_wallet.prepare_multisig.return_value = "hub_info"
        mock_wallet.make_multisig.return_value = {
            "address": "",
            "multisig_info": "hub_round2_info",
        }
        mock_wallet.exchange_multisig_keys.return_value = {
            "address": "final_address",
            "multisig_info": "hub_round3_info",
        }
        coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="round-buyer", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="round-seller", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session,
                buyer_id=buyer.id,
                seller_id=seller.id,
                amount=Decimal("5"),
                description="Round test",
            )
            session.commit()

            ms_id = UUID(result["id"])

            # Hub already submitted round 1 in create().
            # Submit buyer and seller for round 1.
            r1_buyer = coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="buyer", round_number=1,
                multisig_info="buyer_round1_info",
            )
            session.commit()
            assert r1_buyer["state_advanced"] is False  # 2 of 3

            r1_seller = coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="seller", round_number=1,
                multisig_info="seller_round1_info",
            )
            session.commit()
            assert r1_seller["state_advanced"] is True
            assert r1_seller["state"] == "setup_round_2"

            # Verify make_multisig was called with buyer+seller round-1 infos
            mock_wallet.make_multisig.assert_called_once_with(
                multisig_info=["buyer_round1_info", "seller_round1_info"],
                threshold=2,
                password="",
            )

        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_submit_round_wrong_state_raises(self, _wh, _al, ms_session_factory):
        """Submitting round 2 data when in setup_round_1 should fail."""
        coordinator = MultisigCoordinator()

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="wrong-buyer", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="wrong-seller", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session,
                buyer_id=buyer.id,
                seller_id=seller.id,
                amount=Decimal("5"),
                description="Wrong state test",
            )
            session.commit()

            ms_id = UUID(result["id"])

            with pytest.raises(ValueError, match="Cannot submit round 2"):
                coordinator.submit_round(
                    db=session, escrow_id=ms_id,
                    participant="buyer", round_number=2,
                    multisig_info="data",
                )

        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_dispute_sets_state(self, _wh, _al, ms_session_factory):
        """Disputing an escrow should set state to 'disputed'."""
        coordinator = MultisigCoordinator()

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="disp-buyer", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="disp-seller", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session,
                buyer_id=buyer.id,
                seller_id=seller.id,
                amount=Decimal("5"),
                description="Dispute test",
            )
            session.commit()

            ms_id = UUID(result["id"])
            dispute_result = coordinator.dispute(
                db=session, escrow_id=ms_id,
                disputer="buyer", reason="Seller unresponsive",
            )
            session.commit()

            assert dispute_result["state"] == "disputed"
            assert dispute_result["disputed_by"] == "buyer"
            assert dispute_result["reason"] == "Seller unresponsive"

        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_cosign_same_as_initiator_raises(self, _wh, _al, ms_session_factory):
        """Cosigning with the same participant as the initiator should fail."""
        coordinator = MultisigCoordinator()

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="cosign-buyer", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="cosign-seller", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session,
                buyer_id=buyer.id,
                seller_id=seller.id,
                amount=Decimal("5"),
                description="Cosign test",
            )
            session.commit()

            ms_id = UUID(result["id"])

            # Manually advance state to funded/active so we can test release
            ms_escrow = session.get(MultisigEscrow, ms_id)
            ms_escrow.state = "funded"
            session.commit()

            coordinator.initiate_release(
                db=session, escrow_id=ms_id, initiator="buyer",
            )
            session.commit()

            with pytest.raises(ValueError, match="different from the release initiator"):
                coordinator.cosign_release(
                    db=session, escrow_id=ms_id,
                    signer="buyer", signed_tx="signed_data",
                )

        finally:
            session.close()


# ===========================================================================
# WALLET RPC INTEGRATION TESTS — verify real RPC calls
# ===========================================================================

class TestMultisigWalletRPC:
    """Tests verifying that real wallet RPC methods are called correctly.

    Uses MagicMock for the wallet to assert call signatures without
    requiring a live Monero daemon.
    """

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_prepare_multisig_called_on_create(self, _wh, _al, ms_session_factory):
        """Creating a multisig escrow should call wallet.prepare_multisig()."""
        mock_wallet = MagicMock()
        mock_wallet.prepare_multisig.return_value = "hub_prepare_info_abc"
        coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="rpc-buyer-1", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="rpc-seller-1", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            coordinator.create(
                db=session,
                buyer_id=buyer.id,
                seller_id=seller.id,
                amount=Decimal("10"),
                description="RPC prepare test",
            )
            session.commit()

            mock_wallet.prepare_multisig.assert_called_once()
        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_prepare_multisig_rpc_error_raises_runtime(
        self, _wh, _al, ms_session_factory,
    ):
        """MoneroRPCError from prepare_multisig should raise RuntimeError."""
        from sthrip.swaps.xmr.wallet import MoneroRPCError

        mock_wallet = MagicMock()
        mock_wallet.prepare_multisig.side_effect = MoneroRPCError("connection refused")
        coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="rpc-err-buyer", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="rpc-err-seller", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            with pytest.raises(RuntimeError, match="prepare hub multisig"):
                coordinator.create(
                    db=session,
                    buyer_id=buyer.id,
                    seller_id=seller.id,
                    amount=Decimal("10"),
                    description="RPC error test",
                )
        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_make_multisig_called_after_round1(self, _wh, _al, ms_session_factory):
        """Completing round 1 should call wallet.make_multisig(threshold=2)."""
        mock_wallet = MagicMock()
        mock_wallet.prepare_multisig.return_value = "hub_r1"
        mock_wallet.make_multisig.return_value = {
            "address": "",
            "multisig_info": "hub_r2_info",
        }
        coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="rpc-mk-buyer", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="rpc-mk-seller", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session, buyer_id=buyer.id, seller_id=seller.id,
                amount=Decimal("5"), description="make_multisig test",
            )
            session.commit()
            ms_id = UUID(result["id"])

            # Submit buyer and seller round 1
            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="buyer", round_number=1,
                multisig_info="buyer_r1",
            )
            session.commit()

            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="seller", round_number=1,
                multisig_info="seller_r1",
            )
            session.commit()

            mock_wallet.make_multisig.assert_called_once_with(
                multisig_info=["buyer_r1", "seller_r1"],
                threshold=2,
                password="",
            )
        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_exchange_keys_called_after_round2(self, _wh, _al, ms_session_factory):
        """Completing round 2 should call wallet.exchange_multisig_keys()."""
        mock_wallet = MagicMock()
        mock_wallet.prepare_multisig.return_value = "hub_r1"
        mock_wallet.make_multisig.return_value = {
            "address": "",
            "multisig_info": "hub_r2_info",
        }
        mock_wallet.exchange_multisig_keys.return_value = {
            "address": "",
            "multisig_info": "hub_r3_info",
        }
        coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="rpc-ex-buyer", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="rpc-ex-seller", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session, buyer_id=buyer.id, seller_id=seller.id,
                amount=Decimal("5"), description="exchange keys test",
            )
            session.commit()
            ms_id = UUID(result["id"])

            # Complete round 1 (buyer + seller)
            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="buyer", round_number=1,
                multisig_info="buyer_r1",
            )
            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="seller", round_number=1,
                multisig_info="seller_r1",
            )
            session.commit()

            # Complete round 2 (buyer + seller)
            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="buyer", round_number=2,
                multisig_info="buyer_r2",
            )
            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="seller", round_number=2,
                multisig_info="seller_r2",
            )
            session.commit()

            mock_wallet.exchange_multisig_keys.assert_called_once_with(
                multisig_info=["buyer_r2", "seller_r2"],
                password="",
            )
        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_finalize_address_after_round3(self, _wh, _al, ms_session_factory):
        """Completing round 3 should finalize the multisig address via RPC."""
        mock_wallet = MagicMock()
        mock_wallet.prepare_multisig.return_value = "hub_r1"
        mock_wallet.make_multisig.return_value = {
            "address": "",
            "multisig_info": "hub_r2_info",
        }
        # Round 2 exchange
        mock_wallet.exchange_multisig_keys.side_effect = [
            {"address": "", "multisig_info": "hub_r3_info"},
            # Round 3 finalize — returns the shared address
            {"address": "5" + "b" * 94, "multisig_info": ""},
        ]
        coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="rpc-fin-buyer", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="rpc-fin-seller", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session, buyer_id=buyer.id, seller_id=seller.id,
                amount=Decimal("5"), description="finalize test",
            )
            session.commit()
            ms_id = UUID(result["id"])

            # Round 1
            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="buyer", round_number=1, multisig_info="b_r1",
            )
            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="seller", round_number=1, multisig_info="s_r1",
            )
            session.commit()

            # Round 2
            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="buyer", round_number=2, multisig_info="b_r2",
            )
            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="seller", round_number=2, multisig_info="s_r2",
            )
            session.commit()

            # Round 3
            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="buyer", round_number=3, multisig_info="b_r3",
            )
            r3_result = coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="seller", round_number=3, multisig_info="s_r3",
            )
            session.commit()

            assert r3_result["state"] == "funded"
            assert r3_result["state_advanced"] is True

            # Verify the finalized address was stored
            ms_escrow = session.get(MultisigEscrow, ms_id)
            assert ms_escrow.multisig_address == "5" + "b" * 94
        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_hub_auto_submits_next_round(self, _wh, _al, ms_session_factory):
        """When round 1 completes, hub should auto-submit round 2 data."""
        mock_wallet = MagicMock()
        mock_wallet.prepare_multisig.return_value = "hub_r1"
        mock_wallet.make_multisig.return_value = {
            "address": "",
            "multisig_info": "hub_auto_r2",
        }
        coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="rpc-auto-buyer", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="rpc-auto-seller", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session, buyer_id=buyer.id, seller_id=seller.id,
                amount=Decimal("5"), description="auto-submit test",
            )
            session.commit()
            ms_id = UUID(result["id"])

            # Complete round 1
            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="buyer", round_number=1, multisig_info="b_r1",
            )
            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="seller", round_number=1, multisig_info="s_r1",
            )
            session.commit()

            # After round 1 completes, hub should have auto-submitted
            # round 2 data. Verify by querying round 2 submissions.
            from sthrip.db.multisig_repo import MultisigEscrowRepository
            ms_repo = MultisigEscrowRepository(session)
            r2_count = ms_repo.count_round_submissions(ms_id, 2)
            assert r2_count == 1  # hub auto-submitted

            r2_rounds = ms_repo.get_rounds(ms_id, 2)
            assert len(r2_rounds) == 1
            assert r2_rounds[0].participant == "hub"
            assert r2_rounds[0].multisig_info == "hub_auto_r2"
        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_make_multisig_rpc_error_raises_runtime(
        self, _wh, _al, ms_session_factory,
    ):
        """MoneroRPCError from make_multisig should raise RuntimeError."""
        from sthrip.swaps.xmr.wallet import MoneroRPCError

        mock_wallet = MagicMock()
        mock_wallet.prepare_multisig.return_value = "hub_r1"
        mock_wallet.make_multisig.side_effect = MoneroRPCError("timeout")
        coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="rpc-mk-err-b", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="rpc-mk-err-s", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session, buyer_id=buyer.id, seller_id=seller.id,
                amount=Decimal("5"), description="make err test",
            )
            session.commit()
            ms_id = UUID(result["id"])

            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="buyer", round_number=1, multisig_info="b_r1",
            )
            session.commit()

            with pytest.raises(RuntimeError, match="round 1"):
                coordinator.submit_round(
                    db=session, escrow_id=ms_id,
                    participant="seller", round_number=1, multisig_info="s_r1",
                )
        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_release_tx_calls_export_and_transfer(
        self, _wh, _al, ms_session_factory,
    ):
        """initiate_release should call export_multisig_info then transfer."""
        mock_wallet = MagicMock()
        mock_wallet.prepare_multisig.return_value = "hub_r1"
        mock_wallet.export_multisig_info.return_value = "export_info"
        mock_wallet.transfer.return_value = {
            "tx_hash": "abc123",
            "tx_key": "key456",
            "amount": Decimal("9.9"),
            "fee": Decimal("0.001"),
        }
        coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="rpc-rel-buyer", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(
                agent_name="rpc-rel-seller",
                xmr_address="5" + "c" * 94,
            )
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session, buyer_id=buyer.id, seller_id=seller.id,
                amount=Decimal("10"), description="release test",
            )
            session.commit()
            ms_id = UUID(result["id"])

            # Advance to funded state
            ms_escrow = session.get(MultisigEscrow, ms_id)
            ms_escrow.state = "funded"
            ms_escrow.multisig_address = "5" + "d" * 94
            session.commit()

            release_result = coordinator.initiate_release(
                db=session, escrow_id=ms_id, initiator="buyer",
            )
            session.commit()

            assert release_result["state"] == "releasing"
            assert release_result["partial_tx"] == "abc123"

            mock_wallet.export_multisig_info.assert_called_once()
            mock_wallet.transfer.assert_called_once()

            # Verify transfer destination is the seller's address
            call_args = mock_wallet.transfer.call_args
            destinations = call_args.kwargs.get(
                "destinations", call_args.args[0] if call_args.args else None
            )
            assert len(destinations) == 1
            assert destinations[0].address == "5" + "c" * 94
        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_release_tx_missing_seller_address_raises(
        self, _wh, _al, ms_session_factory,
    ):
        """Release should fail if seller has no XMR address."""
        mock_wallet = MagicMock()
        mock_wallet.prepare_multisig.return_value = "hub_r1"
        mock_wallet.export_multisig_info.return_value = "info"
        coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="rpc-noaddr-b", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="rpc-noaddr-s", xmr_address=None)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session, buyer_id=buyer.id, seller_id=seller.id,
                amount=Decimal("10"), description="no address test",
            )
            session.commit()
            ms_id = UUID(result["id"])

            ms_escrow = session.get(MultisigEscrow, ms_id)
            ms_escrow.state = "funded"
            session.commit()

            with pytest.raises(ValueError, match="seller XMR address is missing"):
                coordinator.initiate_release(
                    db=session, escrow_id=ms_id, initiator="buyer",
                )
        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_broadcast_calls_submit_multisig(self, _wh, _al, ms_session_factory):
        """cosign_release should call wallet.submit_multisig with signed TX."""
        mock_wallet = MagicMock()
        mock_wallet.prepare_multisig.return_value = "hub_r1"
        mock_wallet.submit_multisig.return_value = "final_tx_hash_xyz"
        coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="rpc-bc-buyer", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="rpc-bc-seller", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session, buyer_id=buyer.id, seller_id=seller.id,
                amount=Decimal("10"), description="broadcast test",
            )
            session.commit()
            ms_id = UUID(result["id"])

            # Advance to releasing state
            ms_escrow = session.get(MultisigEscrow, ms_id)
            ms_escrow.state = "releasing"
            ms_escrow.release_initiator = "buyer"
            ms_escrow.release_tx_hex = "partial_hex_data"
            session.commit()

            cosign_result = coordinator.cosign_release(
                db=session, escrow_id=ms_id,
                signer="seller", signed_tx="fully_signed_hex",
            )
            session.commit()

            assert cosign_result["state"] == "completed"
            assert cosign_result["tx_hash"] == "final_tx_hash_xyz"

            mock_wallet.submit_multisig.assert_called_once_with("fully_signed_hex")
        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_broadcast_rpc_error_raises_runtime(
        self, _wh, _al, ms_session_factory,
    ):
        """MoneroRPCError from submit_multisig should raise RuntimeError."""
        from sthrip.swaps.xmr.wallet import MoneroRPCError

        mock_wallet = MagicMock()
        mock_wallet.prepare_multisig.return_value = "hub_r1"
        mock_wallet.submit_multisig.side_effect = MoneroRPCError("network error")
        coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="rpc-bc-err-b", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="rpc-bc-err-s", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session, buyer_id=buyer.id, seller_id=seller.id,
                amount=Decimal("10"), description="broadcast err test",
            )
            session.commit()
            ms_id = UUID(result["id"])

            ms_escrow = session.get(MultisigEscrow, ms_id)
            ms_escrow.state = "releasing"
            ms_escrow.release_initiator = "buyer"
            session.commit()

            with pytest.raises(RuntimeError, match="broadcast multisig"):
                coordinator.cosign_release(
                    db=session, escrow_id=ms_id,
                    signer="seller", signed_tx="signed_hex",
                )
        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_stub_mode_no_wallet_works(self, _wh, _al, ms_session_factory):
        """When wallet_rpc=None, stub behaviour should still work."""
        coordinator = MultisigCoordinator(wallet_rpc=None)

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="stub-buyer", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="stub-seller", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session, buyer_id=buyer.id, seller_id=seller.id,
                amount=Decimal("10"), description="Stub mode test",
            )
            session.commit()

            assert result["state"] == "setup_round_1"
            ms_id = UUID(result["id"])

            # Complete round 1 — stubs should return synthetic data
            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="buyer", round_number=1, multisig_info="b1",
            )
            r1_result = coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="seller", round_number=1, multisig_info="s1",
            )
            session.commit()

            assert r1_result["state"] == "setup_round_2"
            assert r1_result["state_advanced"] is True

            # Complete round 2
            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="buyer", round_number=2, multisig_info="b2",
            )
            r2_result = coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="seller", round_number=2, multisig_info="s2",
            )
            session.commit()
            assert r2_result["state"] == "setup_round_3"

            # Complete round 3
            coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="buyer", round_number=3, multisig_info="b3",
            )
            r3_result = coordinator.submit_round(
                db=session, escrow_id=ms_id,
                participant="seller", round_number=3, multisig_info="s3",
            )
            session.commit()
            assert r3_result["state"] == "funded"

            # Verify address is a stub
            ms_escrow = session.get(MultisigEscrow, ms_id)
            assert ms_escrow.multisig_address.startswith("multisig_address_")

            # Initiate and cosign release in stub mode
            ms_escrow.state = "funded"
            session.commit()

            release = coordinator.initiate_release(
                db=session, escrow_id=ms_id, initiator="hub",
            )
            session.commit()
            assert release["partial_tx"].startswith("partial_tx_")

            cosign = coordinator.cosign_release(
                db=session, escrow_id=ms_id,
                signer="seller", signed_tx="stub_signed",
            )
            session.commit()
            assert cosign["state"] == "completed"
            assert cosign["tx_hash"].startswith("tx_hash_")
        finally:
            session.close()

    @patch("sthrip.services.multisig_coordinator.audit_log")
    @patch("sthrip.services.multisig_coordinator.queue_webhook")
    def test_finalize_fallback_to_finalize_multisig(
        self, _wh, _al, ms_session_factory,
    ):
        """If exchange_multisig_keys returns no address, fall back to finalize_multisig."""
        mock_wallet = MagicMock()
        mock_wallet.prepare_multisig.return_value = "hub_r1"
        mock_wallet.make_multisig.return_value = {
            "address": "",
            "multisig_info": "hub_r2",
        }
        mock_wallet.exchange_multisig_keys.side_effect = [
            # Round 2 call
            {"address": "", "multisig_info": "hub_r3"},
            # Round 3 finalize attempt — no address returned
            {"address": "", "multisig_info": ""},
        ]
        mock_wallet.finalize_multisig.return_value = "5" + "f" * 94
        coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)

        session = ms_session_factory()
        try:
            buyer = Agent(agent_name="rpc-fb-buyer", xmr_address=_VALID_XMR_ADDR)
            seller = Agent(agent_name="rpc-fb-seller", xmr_address=_VALID_XMR_ADDR)
            session.add_all([buyer, seller])
            session.flush()

            balance = AgentBalance(
                agent_id=buyer.id, token="XMR",
                available=Decimal("100"), total_deposited=Decimal("100"),
            )
            session.add(balance)
            session.flush()

            result = coordinator.create(
                db=session, buyer_id=buyer.id, seller_id=seller.id,
                amount=Decimal("5"), description="fallback test",
            )
            session.commit()
            ms_id = UUID(result["id"])

            # Complete all 3 rounds
            for rnd in (1, 2, 3):
                coordinator.submit_round(
                    db=session, escrow_id=ms_id,
                    participant="buyer", round_number=rnd, multisig_info=f"b{rnd}",
                )
                coordinator.submit_round(
                    db=session, escrow_id=ms_id,
                    participant="seller", round_number=rnd, multisig_info=f"s{rnd}",
                )
                session.commit()

            ms_escrow = session.get(MultisigEscrow, ms_id)
            assert ms_escrow.multisig_address == "5" + "f" * 94

            # finalize_multisig should have been called as fallback
            mock_wallet.finalize_multisig.assert_called_once()
        finally:
            session.close()


# ===========================================================================
# INTEGRATION TESTS — API endpoints
# ===========================================================================

class TestMultisigEscrowAPI:
    """Integration tests for multisig escrow API endpoints."""

    def test_create_multisig_escrow_collects_fee_upfront(self, ms_client):
        """POST /v2/escrow with mode=multisig should collect 1% fee upfront."""
        buyer_key = _register_agent(ms_client, "ms-buyer-1")
        _register_agent(ms_client, "ms-seller-1")
        _deposit(ms_client, buyer_key, 100.0)

        result = _create_multisig_escrow(ms_client, buyer_key, "ms-seller-1", 10.0)

        assert result["mode"] == "multisig"
        assert result["state"] == "setup_round_1"
        assert result["fee_collected"] == "0.10000000"
        assert result["funded_amount"] == "9.90000000"
        assert result["escrow_id"]  # EscrowDeal ID
        assert result["multisig_escrow_id"]  # MultisigEscrow ID

    def test_multisig_round_progression(self, ms_client):
        """Submit 3 round-1 entries and verify state advances to setup_round_2."""
        buyer_key = _register_agent(ms_client, "ms-buyer-2")
        seller_key = _register_agent(ms_client, "ms-seller-2")
        _deposit(ms_client, buyer_key, 100.0)

        result = _create_multisig_escrow(ms_client, buyer_key, "ms-seller-2", 5.0)
        ms_id = result["multisig_escrow_id"]

        # Hub already submitted round 1 in create.
        # Submit buyer round 1.
        r1 = ms_client.post(
            f"/v2/escrow/{ms_id}/round",
            json={
                "participant": "buyer",
                "round_number": 1,
                "multisig_info": "buyer_round1_data",
            },
            headers={"Authorization": f"Bearer {buyer_key}"},
        )
        assert r1.status_code == 200
        assert r1.json()["state_advanced"] is False

        # Submit seller round 1.
        r2 = ms_client.post(
            f"/v2/escrow/{ms_id}/round",
            json={
                "participant": "seller",
                "round_number": 1,
                "multisig_info": "seller_round1_data",
            },
            headers={"Authorization": f"Bearer {seller_key}"},
        )
        assert r2.status_code == 200
        body = r2.json()
        assert body["state_advanced"] is True
        assert body["state"] == "setup_round_2"
        assert body["submissions_count"] == 3

    def test_get_multisig_state(self, ms_client):
        """GET /v2/escrow/{id}/multisig-state returns current state."""
        buyer_key = _register_agent(ms_client, "ms-buyer-3")
        _register_agent(ms_client, "ms-seller-3")
        _deposit(ms_client, buyer_key, 100.0)

        result = _create_multisig_escrow(ms_client, buyer_key, "ms-seller-3", 8.0)
        ms_id = result["multisig_escrow_id"]

        r = ms_client.get(
            f"/v2/escrow/{ms_id}/multisig-state",
            headers={"Authorization": f"Bearer {buyer_key}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "setup_round_1"
        assert body["fee_collected"] == "0.08000000"
        assert body["funded_amount"] == "7.92000000"

    def test_default_mode_is_hub_held(self, ms_client):
        """POST /v2/escrow without mode should default to hub-held."""
        buyer_key = _register_agent(ms_client, "ms-buyer-4")
        _register_agent(ms_client, "ms-seller-4")
        _deposit(ms_client, buyer_key, 100.0)

        r = ms_client.post(
            "/v2/escrow",
            json={
                "seller_agent_name": "ms-seller-4",
                "amount": 5.0,
                "description": "Hub-held default test",
            },
            headers={"Authorization": f"Bearer {buyer_key}"},
        )
        assert r.status_code == 201
        body = r.json()
        # Hub-held mode returns escrow_id and status, no multisig fields
        assert "escrow_id" in body
        assert "status" in body
        assert body.get("mode") is None  # hub-held doesn't include mode
        assert "multisig_escrow_id" not in body

    def test_hub_held_explicit_mode(self, ms_client):
        """POST /v2/escrow with mode=hub-held should work normally."""
        buyer_key = _register_agent(ms_client, "ms-buyer-5")
        _register_agent(ms_client, "ms-seller-5")
        _deposit(ms_client, buyer_key, 100.0)

        r = ms_client.post(
            "/v2/escrow",
            json={
                "seller_agent_name": "ms-seller-5",
                "amount": 5.0,
                "description": "Explicit hub-held test",
                "mode": "hub-held",
            },
            headers={"Authorization": f"Bearer {buyer_key}"},
        )
        assert r.status_code == 201
        body = r.json()
        assert "escrow_id" in body
        assert "status" in body
        assert "multisig_escrow_id" not in body

    def test_dispute_endpoint(self, ms_client):
        """POST /v2/escrow/{id}/dispute should set state to disputed."""
        buyer_key = _register_agent(ms_client, "ms-buyer-6")
        _register_agent(ms_client, "ms-seller-6")
        _deposit(ms_client, buyer_key, 100.0)

        result = _create_multisig_escrow(ms_client, buyer_key, "ms-seller-6", 5.0)
        ms_id = result["multisig_escrow_id"]

        r = ms_client.post(
            f"/v2/escrow/{ms_id}/dispute",
            json={
                "disputer": "buyer",
                "reason": "Seller not responding",
            },
            headers={"Authorization": f"Bearer {buyer_key}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "disputed"
        assert body["disputed_by"] == "buyer"
        assert body["reason"] == "Seller not responding"

    def test_invalid_round_number(self, ms_client):
        """Submitting round 0 or 4 should fail validation."""
        buyer_key = _register_agent(ms_client, "ms-buyer-7")
        _register_agent(ms_client, "ms-seller-7")
        _deposit(ms_client, buyer_key, 100.0)

        result = _create_multisig_escrow(ms_client, buyer_key, "ms-seller-7", 5.0)
        ms_id = result["multisig_escrow_id"]

        r = ms_client.post(
            f"/v2/escrow/{ms_id}/round",
            json={
                "participant": "buyer",
                "round_number": 0,
                "multisig_info": "data",
            },
            headers={"Authorization": f"Bearer {buyer_key}"},
        )
        assert r.status_code == 422  # Pydantic validation

    def test_invalid_participant(self, ms_client):
        """Submitting with invalid participant should fail validation."""
        buyer_key = _register_agent(ms_client, "ms-buyer-8")
        _register_agent(ms_client, "ms-seller-8")
        _deposit(ms_client, buyer_key, 100.0)

        result = _create_multisig_escrow(ms_client, buyer_key, "ms-seller-8", 5.0)
        ms_id = result["multisig_escrow_id"]

        r = ms_client.post(
            f"/v2/escrow/{ms_id}/round",
            json={
                "participant": "attacker",
                "round_number": 1,
                "multisig_info": "data",
            },
            headers={"Authorization": f"Bearer {buyer_key}"},
        )
        assert r.status_code == 422  # Pydantic pattern validation

    def test_multisig_with_milestones_rejected(self, ms_client):
        """Multisig mode should reject milestones (not supported yet)."""
        buyer_key = _register_agent(ms_client, "ms-buyer-9")
        _register_agent(ms_client, "ms-seller-9")
        _deposit(ms_client, buyer_key, 100.0)

        r = ms_client.post(
            "/v2/escrow",
            json={
                "seller_agent_name": "ms-seller-9",
                "amount": 10.0,
                "description": "Milestones + multisig",
                "mode": "multisig",
                "milestones": [
                    {
                        "description": "Phase 1",
                        "amount": 5.0,
                        "delivery_timeout_hours": 48,
                        "review_timeout_hours": 24,
                    },
                    {
                        "description": "Phase 2",
                        "amount": 5.0,
                        "delivery_timeout_hours": 48,
                        "review_timeout_hours": 24,
                    },
                ],
            },
            headers={"Authorization": f"Bearer {buyer_key}"},
        )
        assert r.status_code == 422  # Pydantic model validator

    def test_nonexistent_escrow_state(self, ms_client):
        """GET /v2/escrow/{nonexistent}/multisig-state should 404."""
        buyer_key = _register_agent(ms_client, "ms-buyer-10")

        r = ms_client.get(
            "/v2/escrow/00000000-0000-0000-0000-000000000001/multisig-state",
            headers={"Authorization": f"Bearer {buyer_key}"},
        )
        assert r.status_code == 404

    def test_cannot_self_escrow_multisig(self, ms_client):
        """Cannot create multisig escrow with yourself."""
        buyer_key = _register_agent(ms_client, "ms-self-escrow")
        _deposit(ms_client, buyer_key, 100.0)

        r = ms_client.post(
            "/v2/escrow",
            json={
                "seller_agent_name": "ms-self-escrow",
                "amount": 5.0,
                "description": "Self escrow",
                "mode": "multisig",
            },
            headers={"Authorization": f"Bearer {buyer_key}"},
        )
        assert r.status_code == 400
        assert "yourself" in r.json()["detail"].lower()
