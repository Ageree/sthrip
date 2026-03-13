"""Tests for Phase 8 MEDIUM data & code quality fixes (MED-2 thru MED-12)."""

import hashlib
import json
import socket
import time
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sthrip.db.models import (
    Agent, AgentBalance, AgentReputation, Base, Transaction, SystemState,
    AgentTier, RateLimitTier, PrivacyLevel, TransactionStatus,
    FeeCollection, FeeCollectionStatus,
    WebhookEvent, WebhookStatus,
)
from sthrip.db.repository import (
    BalanceRepository, TransactionRepository, SystemStateRepository,
    WebhookRepository,
)


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED FIXTURES
# ═══════════════════════════════════════════════════════════════════════════════

_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    Transaction.__table__,
    SystemState.__table__,
    FeeCollection.__table__,
    WebhookEvent.__table__,
]


@pytest.fixture
def db_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=_TEST_TABLES)
    return engine


@pytest.fixture
def db_session_factory(db_engine):
    from contextlib import contextmanager
    _maker = sessionmaker(bind=db_engine)

    @contextmanager
    def _factory():
        session = _maker()
        try:
            yield session
        finally:
            session.close()

    return _factory


@pytest.fixture
def db_session(db_session_factory):
    with db_session_factory() as session:
        yield session


@pytest.fixture
def agent(db_session):
    agent = Agent(
        agent_name="phase8-test-agent",
        api_key_hash="phase8hash",
        tier=AgentTier.FREE,
        rate_limit_tier=RateLimitTier.STANDARD,
        privacy_level=PrivacyLevel.MEDIUM,
        is_active=True,
    )
    db_session.add(agent)
    db_session.flush()
    repo = BalanceRepository(db_session)
    repo.set_deposit_address(agent.id, "5Phase8Subaddr001")
    db_session.flush()
    return agent


# ═══════════════════════════════════════════════════════════════════════════════
# MED-2: Consolidated _do_poll (deposit_monitor.py)
# ═══════════════════════════════════════════════════════════════════════════════


class TestConsolidatedDoPoll:
    """MED-2: _do_poll should delegate to _do_poll_with_session."""

    def test_do_poll_delegates_to_do_poll_with_session(
        self, db_session_factory, agent, db_session,
    ):
        """_do_poll should create a session and call _do_poll_with_session."""
        from sthrip.services.deposit_monitor import DepositMonitor

        mock_wallet = MagicMock()
        mock_wallet.get_incoming_transfers.return_value = [
            {
                "txid": "tx_consolidated_test",
                "amount": Decimal("1.0"),
                "confirmations": 15,
                "height": 100_000,
                "address": "5Phase8Subaddr001",
            },
        ]

        monitor = DepositMonitor(
            wallet_service=mock_wallet,
            db_session_factory=db_session_factory,
            min_confirmations=10,
        )
        # Call _do_poll directly (the redis lock path)
        monitor._do_poll()

        # Verify the transfer was processed
        db_session.expire_all()
        tx_repo = TransactionRepository(db_session)
        tx = tx_repo.get_by_hash("tx_consolidated_test")
        assert tx is not None
        assert tx.status == TransactionStatus.CONFIRMED

    def test_do_poll_with_session_handles_commit_and_rollback(
        self, db_session_factory, agent, db_session,
    ):
        """_do_poll_with_session should commit on success, rollback on error."""
        from sthrip.services.deposit_monitor import DepositMonitor

        mock_wallet = MagicMock()
        mock_wallet.get_incoming_transfers.return_value = [
            {
                "txid": "tx_session_test",
                "amount": Decimal("2.0"),
                "confirmations": 15,
                "height": 100_001,
                "address": "5Phase8Subaddr001",
            },
        ]

        monitor = DepositMonitor(
            wallet_service=mock_wallet,
            db_session_factory=db_session_factory,
            min_confirmations=10,
        )

        # Use _do_poll_with_session directly
        with db_session_factory() as session:
            monitor._do_poll_with_session(session)

        # Verify commit happened
        db_session.expire_all()
        tx = TransactionRepository(db_session).get_by_hash("tx_session_test")
        assert tx is not None

    def test_do_poll_no_duplicate_transfer_fetching(self, db_session_factory):
        """_do_poll should only fetch transfers once (via _do_poll_with_session)."""
        from sthrip.services.deposit_monitor import DepositMonitor

        mock_wallet = MagicMock()
        mock_wallet.get_incoming_transfers.return_value = []

        monitor = DepositMonitor(
            wallet_service=mock_wallet,
            db_session_factory=db_session_factory,
            min_confirmations=10,
        )
        monitor._do_poll()

        # get_incoming_transfers should be called exactly once
        assert mock_wallet.get_incoming_transfers.call_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# MED-4: Fee collector bulk query (fee_collector.py)
# ═══════════════════════════════════════════════════════════════════════════════


class TestFeeCollectorBulkQuery:
    """MED-4: withdraw_fees should use bulk query instead of N+1."""

    @patch("sthrip.services.fee_collector.get_db")
    def test_withdraw_uses_bulk_update(self, mock_get_db):
        """withdraw_fees should use FOR UPDATE locking on matched rows."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        fee1 = MagicMock(amount=Decimal("0.1"))
        fee2 = MagicMock(amount=Decimal("0.1"))
        fee3 = MagicMock(amount=Decimal("0.1"))
        mock_db.query.return_value.filter.return_value.with_for_update.return_value.all.return_value = [fee1, fee2, fee3]

        from sthrip.services.fee_collector import FeeCollector
        collector = FeeCollector()
        result = collector.withdraw_fees(["f1", "f2", "f3"], "tx_bulk")

        assert result["tx_hash"] == "tx_bulk"
        assert result["withdrawn_fees"] == 3

    @patch("sthrip.services.fee_collector.get_db")
    def test_withdraw_returns_correct_total(self, mock_get_db):
        """Total amount should come from locked rows sum."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        fee1 = MagicMock(amount=Decimal("1.0"))
        fee2 = MagicMock(amount=Decimal("0.5"))
        mock_db.query.return_value.filter.return_value.with_for_update.return_value.all.return_value = [fee1, fee2]

        from sthrip.services.fee_collector import FeeCollector
        collector = FeeCollector()
        result = collector.withdraw_fees(["f1", "f2"], "tx_total")

        assert result["total_amount"] == "1.5"


# ═══════════════════════════════════════════════════════════════════════════════
# MED-5: SSRF DNS failure behavior (url_validator.py)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSSRFDnsFailureBehavior:
    """MED-5: block_on_dns_failure parameter for validate_url_target."""

    def test_dns_failure_allowed_by_default(self):
        """Default behavior: DNS failure should not raise."""
        from sthrip.services.url_validator import _check_hostname_safe
        with patch("sthrip.services.url_validator.socket.getaddrinfo",
                    side_effect=socket.gaierror("DNS fail")):
            _check_hostname_safe("nonexistent.test")  # Should not raise

    def test_dns_failure_blocked_when_requested(self):
        """When block_on_dns_failure=True, DNS failure should raise SSRFBlockedError."""
        from sthrip.services.url_validator import _check_hostname_safe, SSRFBlockedError
        with patch("sthrip.services.url_validator.socket.getaddrinfo",
                    side_effect=socket.gaierror("DNS fail")):
            with pytest.raises(SSRFBlockedError, match="DNS resolution failed"):
                _check_hostname_safe("nonexistent.test", block_on_dns_failure=True)

    def test_validate_url_target_passes_block_on_dns_failure(self):
        """validate_url_target should pass block_on_dns_failure to _check_hostname_safe."""
        from sthrip.services.url_validator import validate_url_target, SSRFBlockedError
        with patch("sthrip.services.url_validator.socket.getaddrinfo",
                    side_effect=socket.gaierror("DNS fail")):
            # Default: should not raise
            validate_url_target("https://nonexistent.test/hook", enforce_https=True)

            # With block: should raise
            with pytest.raises(SSRFBlockedError, match="DNS resolution failed"):
                validate_url_target(
                    "https://nonexistent.test/hook",
                    enforce_https=True,
                    block_on_dns_failure=True,
                )


# ═══════════════════════════════════════════════════════════════════════════════
# MED-6: Agent name uniqueness TOCTOU (agent_registry.py)
# ═══════════════════════════════════════════════════════════════════════════════


class TestAgentNameUniqueness:
    """MED-6: register_agent should catch IntegrityError on flush."""

    @patch("sthrip.services.agent_registry.get_db")
    def test_integrity_error_raises_value_error(self, mock_get_db):
        """IntegrityError on flush should be caught and re-raised as ValueError.

        CRIT-4 fix: register_agent now uses db.flush() instead of db.commit()
        so that IntegrityError is detected early while the context manager
        remains the single authoritative commit point.
        """
        from sqlalchemy.exc import IntegrityError
        from sthrip.services.agent_registry import AgentRegistry

        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        # No existing agent found (TOCTOU window)
        mock_db.query.return_value.filter.return_value.first.return_value = None

        # Mock repo creation
        with patch("sthrip.services.agent_registry.AgentRepository") as MockRepo:
            mock_agent = MagicMock()
            mock_agent.id = uuid4()
            mock_agent.tier = "free"
            mock_agent.created_at = datetime.now(timezone.utc)
            mock_creds = {"api_key": "sk_test", "webhook_secret": "whsec_test"}
            MockRepo.return_value.create_agent.return_value = (mock_agent, mock_creds)

            # Simulate IntegrityError on flush (CRIT-4: was commit, now flush)
            mock_db.flush.side_effect = IntegrityError("", {}, None)

            registry = AgentRegistry()
            with pytest.raises(ValueError):
                registry.register_agent("duplicate-name")

            # Verify rollback was called
            mock_db.rollback.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# MED-11: Idempotency key hashing (idempotency.py)
# ═══════════════════════════════════════════════════════════════════════════════


class TestIdempotencyKeyHashing:
    """MED-11: Idempotency keys should be hashed with SHA-256."""

    def test_key_uses_sha256_hash(self):
        """_key should hash the idempotency_key component with SHA-256."""
        import sthrip.services.idempotency as idempotency_mod

        orig = idempotency_mod.REDIS_AVAILABLE
        try:
            idempotency_mod.REDIS_AVAILABLE = False
            store = idempotency_mod.IdempotencyStore()
        finally:
            idempotency_mod.REDIS_AVAILABLE = orig

        key = store._key("agent1", "/pay", "my-key-123")
        expected_hash = hashlib.sha256("my-key-123".encode()).hexdigest()
        assert expected_hash in key
        assert "my-key-123" not in key  # Raw key should NOT appear

    def test_key_includes_agent_and_endpoint(self):
        """Key should still include agent_id and endpoint for scoping."""
        import sthrip.services.idempotency as idempotency_mod

        orig = idempotency_mod.REDIS_AVAILABLE
        try:
            idempotency_mod.REDIS_AVAILABLE = False
            store = idempotency_mod.IdempotencyStore()
        finally:
            idempotency_mod.REDIS_AVAILABLE = orig

        key = store._key("agent1", "/pay", "k1")
        assert key.startswith("idempotency:agent1:/pay:")

    def test_different_keys_produce_different_hashes(self):
        """Different idempotency keys should produce different hashed keys."""
        import sthrip.services.idempotency as idempotency_mod

        orig = idempotency_mod.REDIS_AVAILABLE
        try:
            idempotency_mod.REDIS_AVAILABLE = False
            store = idempotency_mod.IdempotencyStore()
        finally:
            idempotency_mod.REDIS_AVAILABLE = orig

        k1 = store._key("a", "/pay", "key-1")
        k2 = store._key("a", "/pay", "key-2")
        assert k1 != k2


# ═══════════════════════════════════════════════════════════════════════════════
# MED-12: Redact webhook response body (repository.py)
# ═══════════════════════════════════════════════════════════════════════════════


class TestWebhookResponseRedaction:
    """MED-12: Webhook response body should be redacted in storage."""

    def test_mark_delivered_redacts_response_body(self, db_session, agent):
        """mark_delivered should store status code only, not full response body."""
        repo = WebhookRepository(db_session)
        event = repo.create_event(
            agent_id=agent.id,
            event_type="payment.confirmed",
            payload={"amount": "1.0"},
        )
        db_session.flush()

        repo.mark_delivered(
            event_id=event.id,
            response_code=200,
            response_body='{"secret": "should-not-be-stored", "data": "sensitive"}',
        )
        db_session.flush()
        db_session.expire_all()

        updated = repo.get_by_id(event.id)
        assert updated.last_response_body == "status=200"
        assert "secret" not in updated.last_response_body
        assert "sensitive" not in updated.last_response_body
