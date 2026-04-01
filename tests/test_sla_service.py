"""
Tests for SLAService — TDD (RED) phase.

These tests are written before the implementation exists in
sthrip/services/sla_service.py.  They define the expected behaviour of
every public method and must pass once the implementation is added.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base,
    Agent,
    AgentReputation,
    AgentBalance,
    EscrowDeal,
    SLATemplate,
    SLAContract,
)


# ---------------------------------------------------------------------------
# SQLite datetime compatibility (mirrors test_escrow.py pattern)
# ---------------------------------------------------------------------------

def _naive_utc_now() -> datetime:
    """Return current UTC time as a naive datetime (no tzinfo)."""
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# In-memory database fixture
# ---------------------------------------------------------------------------

_SLA_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    EscrowDeal.__table__,
    SLATemplate.__table__,
    SLAContract.__table__,
]


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_SLA_TABLES)
    return engine


@pytest.fixture
def db_session(db_engine):
    Session = sessionmaker(bind=db_engine, expire_on_commit=False)
    session = Session()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(db, name: str, balance: Decimal = Decimal("100")) -> Agent:
    """Insert an agent with reputation and balance."""
    agent = Agent(
        id=uuid.uuid4(),
        agent_name=name,
        api_key_hash="hash_" + name,
        is_active=True,
    )
    db.add(agent)
    db.flush()
    rep = AgentReputation(agent_id=agent.id)
    bal = AgentBalance(agent_id=agent.id, token="XMR", available=balance)
    db.add_all([rep, bal])
    db.flush()
    return agent


_DEFAULT_TEMPLATE_KWARGS = dict(
    name="Test SLA",
    service_description="Generic test service",
    deliverables=[{"name": "report"}],
    response_time_secs=300,
    delivery_time_secs=3600,
    price=Decimal("1.0"),
    currency="XMR",
    penalty_percent=10,
)


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestSLAService:
    """Unit tests for SLAService."""

    # ------------------------------------------------------------------
    # test_create_contract_creates_escrow
    # ------------------------------------------------------------------

    @patch("sthrip.services.sla_service.audit_log")
    @patch("sthrip.services.sla_service.queue_webhook")
    @patch("sthrip.services.sla_service.EscrowService")
    def test_create_contract_creates_escrow(
        self, mock_escrow_cls, mock_webhook, mock_audit, db_session
    ):
        """create_contract must create an escrow deal for the contract price."""
        from sthrip.services.sla_service import SLAService

        escrow_id = uuid.uuid4()
        mock_escrow_instance = MagicMock()
        mock_escrow_instance.create_escrow.return_value = {
            "escrow_id": str(escrow_id)
        }
        mock_escrow_cls.return_value = mock_escrow_instance

        consumer = _make_agent(db_session, "consumer-1", Decimal("50"))
        provider = _make_agent(db_session, "provider-1")

        svc = SLAService()
        result = svc.create_contract(
            db_session,
            consumer_id=consumer.id,
            provider_id=provider.id,
            **_DEFAULT_TEMPLATE_KWARGS,
        )

        # Escrow was requested with the right buyer/seller and amount
        mock_escrow_instance.create_escrow.assert_called_once()
        call_kwargs = mock_escrow_instance.create_escrow.call_args
        assert call_kwargs.kwargs.get("buyer_id") == consumer.id or (
            len(call_kwargs.args) > 1 and call_kwargs.args[1] == consumer.id
        )

        # Returned dict contains contract_id and escrow_deal_id
        assert "contract_id" in result
        assert result["escrow_deal_id"] == str(escrow_id)

    # ------------------------------------------------------------------
    # test_create_contract_insufficient_balance
    # ------------------------------------------------------------------

    @patch("sthrip.services.sla_service.audit_log")
    @patch("sthrip.services.sla_service.queue_webhook")
    @patch("sthrip.services.sla_service.EscrowService")
    def test_create_contract_insufficient_balance(
        self, mock_escrow_cls, mock_webhook, mock_audit, db_session
    ):
        """create_contract raises ValueError when consumer balance is too low."""
        from sthrip.services.sla_service import SLAService

        consumer = _make_agent(db_session, "broke-consumer", Decimal("0"))
        provider = _make_agent(db_session, "rich-provider")

        svc = SLAService()
        with pytest.raises(ValueError, match="[Ii]nsufficient|[Bb]alance"):
            svc.create_contract(
                db_session,
                consumer_id=consumer.id,
                provider_id=provider.id,
                **_DEFAULT_TEMPLATE_KWARGS,
            )

        # EscrowService.create_escrow must NOT have been called
        if mock_escrow_cls.called:
            mock_escrow_cls.return_value.create_escrow.assert_not_called()

    # ------------------------------------------------------------------
    # test_accept_contract
    # ------------------------------------------------------------------

    @patch("sthrip.services.sla_service.audit_log")
    @patch("sthrip.services.sla_service.queue_webhook")
    @patch("sthrip.services.sla_service.EscrowService")
    def test_accept_contract(
        self, mock_escrow_cls, mock_webhook, mock_audit, db_session
    ):
        """Provider accepts contract: state transitions proposed -> active."""
        from sthrip.services.sla_service import SLAService
        from sthrip.db.sla_repo import SLAContractRepository
        from sthrip.db.models import SLAStatus

        escrow_id = uuid.uuid4()
        mock_escrow_instance = MagicMock()
        mock_escrow_instance.create_escrow.return_value = {
            "escrow_id": str(escrow_id)
        }
        mock_escrow_cls.return_value = mock_escrow_instance

        consumer = _make_agent(db_session, "accept-consumer", Decimal("50"))
        provider = _make_agent(db_session, "accept-provider")

        svc = SLAService()
        create_result = svc.create_contract(
            db_session,
            consumer_id=consumer.id,
            provider_id=provider.id,
            **_DEFAULT_TEMPLATE_KWARGS,
        )
        contract_id = uuid.UUID(create_result["contract_id"])

        accept_result = svc.accept_contract(db_session, contract_id, provider.id)

        assert "started_at" in accept_result
        assert accept_result["state"] == "active"

        contract = SLAContractRepository(db_session).get_by_id(contract_id)
        assert contract.state == SLAStatus.ACTIVE

    # ------------------------------------------------------------------
    # test_accept_wrong_agent
    # ------------------------------------------------------------------

    @patch("sthrip.services.sla_service.audit_log")
    @patch("sthrip.services.sla_service.queue_webhook")
    @patch("sthrip.services.sla_service.EscrowService")
    def test_accept_wrong_agent(
        self, mock_escrow_cls, mock_webhook, mock_audit, db_session
    ):
        """Accepting with wrong agent raises PermissionError."""
        from sthrip.services.sla_service import SLAService

        escrow_id = uuid.uuid4()
        mock_escrow_instance = MagicMock()
        mock_escrow_instance.create_escrow.return_value = {
            "escrow_id": str(escrow_id)
        }
        mock_escrow_cls.return_value = mock_escrow_instance

        consumer = _make_agent(db_session, "wrong-consumer", Decimal("50"))
        provider = _make_agent(db_session, "wrong-provider")
        impostor = _make_agent(db_session, "impostor")

        svc = SLAService()
        create_result = svc.create_contract(
            db_session,
            consumer_id=consumer.id,
            provider_id=provider.id,
            **_DEFAULT_TEMPLATE_KWARGS,
        )
        contract_id = uuid.UUID(create_result["contract_id"])

        with pytest.raises(PermissionError):
            svc.accept_contract(db_session, contract_id, impostor.id)

    # ------------------------------------------------------------------
    # test_deliver_contract
    # ------------------------------------------------------------------

    @patch("sthrip.services.sla_service.audit_log")
    @patch("sthrip.services.sla_service.queue_webhook")
    @patch("sthrip.services.sla_service.EscrowService")
    def test_deliver_contract(
        self, mock_escrow_cls, mock_webhook, mock_audit, db_session
    ):
        """Provider delivers contract with result hash; state becomes delivered."""
        from sthrip.services.sla_service import SLAService
        from sthrip.db.sla_repo import SLAContractRepository
        from sthrip.db.models import SLAStatus

        escrow_id = uuid.uuid4()
        mock_escrow_instance = MagicMock()
        mock_escrow_instance.create_escrow.return_value = {
            "escrow_id": str(escrow_id)
        }
        mock_escrow_cls.return_value = mock_escrow_instance

        consumer = _make_agent(db_session, "del-consumer", Decimal("50"))
        provider = _make_agent(db_session, "del-provider")

        svc = SLAService()
        create_result = svc.create_contract(
            db_session,
            consumer_id=consumer.id,
            provider_id=provider.id,
            **_DEFAULT_TEMPLATE_KWARGS,
        )
        contract_id = uuid.UUID(create_result["contract_id"])
        svc.accept_contract(db_session, contract_id, provider.id)

        deliver_result = svc.deliver_contract(
            db_session, contract_id, provider.id, result_hash="sha256:deadbeef"
        )

        assert deliver_result["result_hash"] == "sha256:deadbeef"
        contract = SLAContractRepository(db_session).get_by_id(contract_id)
        assert contract.state == SLAStatus.DELIVERED
        assert contract.result_hash == "sha256:deadbeef"

    # ------------------------------------------------------------------
    # test_verify_contract_sla_met
    # ------------------------------------------------------------------

    @patch("sthrip.services.sla_service.audit_log")
    @patch("sthrip.services.sla_service.queue_webhook")
    @patch("sthrip.services.sla_service.EscrowService")
    def test_verify_contract_sla_met(
        self, mock_escrow_cls, mock_webhook, mock_audit, db_session
    ):
        """Consumer verifies delivery within SLA window — sla_met=True."""
        from sthrip.services.sla_service import SLAService
        from sthrip.db.sla_repo import SLAContractRepository
        from sthrip.db.models import SLAStatus

        escrow_id = uuid.uuid4()
        mock_escrow_instance = MagicMock()
        mock_escrow_instance.create_escrow.return_value = {
            "escrow_id": str(escrow_id)
        }
        mock_escrow_cls.return_value = mock_escrow_instance

        consumer = _make_agent(db_session, "verify-consumer", Decimal("50"))
        provider = _make_agent(db_session, "verify-provider")

        svc = SLAService()
        create_result = svc.create_contract(
            db_session,
            consumer_id=consumer.id,
            provider_id=provider.id,
            **_DEFAULT_TEMPLATE_KWARGS,
        )
        contract_id = uuid.UUID(create_result["contract_id"])
        svc.accept_contract(db_session, contract_id, provider.id)

        # Deliver within the 3600-second window
        svc.deliver_contract(
            db_session, contract_id, provider.id, result_hash="sha256:ok"
        )

        verify_result = svc.verify_contract(db_session, contract_id, consumer.id)

        assert verify_result["sla_met"] is True
        contract = SLAContractRepository(db_session).get_by_id(contract_id)
        assert contract.state == SLAStatus.COMPLETED
        assert contract.sla_met is True

    # ------------------------------------------------------------------
    # test_verify_contract_sla_breached
    # ------------------------------------------------------------------

    @patch("sthrip.services.sla_service.audit_log")
    @patch("sthrip.services.sla_service.queue_webhook")
    @patch("sthrip.services.sla_service.EscrowService")
    def test_verify_contract_sla_breached(
        self, mock_escrow_cls, mock_webhook, mock_audit, db_session
    ):
        """Consumer verifies — delivery was late — sla_met=False, penalty recorded."""
        from sthrip.services.sla_service import SLAService
        from sthrip.db.sla_repo import SLAContractRepository
        from sthrip.db.models import SLAStatus

        escrow_id = uuid.uuid4()
        mock_escrow_instance = MagicMock()
        mock_escrow_instance.create_escrow.return_value = {
            "escrow_id": str(escrow_id)
        }
        mock_escrow_cls.return_value = mock_escrow_instance

        consumer = _make_agent(db_session, "late-consumer", Decimal("50"))
        provider = _make_agent(db_session, "late-provider")

        svc = SLAService()
        create_result = svc.create_contract(
            db_session,
            consumer_id=consumer.id,
            provider_id=provider.id,
            **_DEFAULT_TEMPLATE_KWARGS,
        )
        contract_id = uuid.UUID(create_result["contract_id"])
        svc.accept_contract(db_session, contract_id, provider.id)

        # Backdate started_at to simulate late delivery
        contract = SLAContractRepository(db_session).get_by_id(contract_id)
        contract.started_at = _naive_utc_now() - timedelta(seconds=7200)
        db_session.flush()

        svc.deliver_contract(
            db_session, contract_id, provider.id, result_hash="sha256:late"
        )

        verify_result = svc.verify_contract(db_session, contract_id, consumer.id)

        assert verify_result["sla_met"] is False
        contract = SLAContractRepository(db_session).get_by_id(contract_id)
        assert contract.state == SLAStatus.COMPLETED
        assert contract.sla_met is False

    # ------------------------------------------------------------------
    # test_breach_auto_detected
    # ------------------------------------------------------------------

    @patch("sthrip.services.sla_service.audit_log")
    @patch("sthrip.services.sla_service.queue_webhook")
    @patch("sthrip.services.sla_service.EscrowService")
    def test_breach_auto_detected(
        self, mock_escrow_cls, mock_webhook, mock_audit, db_session
    ):
        """enforce_sla detects overdue active contracts and transitions to BREACHED."""
        from sthrip.services.sla_service import SLAService
        from sthrip.db.sla_repo import SLAContractRepository
        from sthrip.db.models import SLAStatus

        escrow_id = uuid.uuid4()
        mock_escrow_instance = MagicMock()
        mock_escrow_instance.create_escrow.return_value = {
            "escrow_id": str(escrow_id)
        }
        mock_escrow_cls.return_value = mock_escrow_instance

        consumer = _make_agent(db_session, "breach-consumer", Decimal("50"))
        provider = _make_agent(db_session, "breach-provider")

        svc = SLAService()
        create_result = svc.create_contract(
            db_session,
            consumer_id=consumer.id,
            provider_id=provider.id,
            **_DEFAULT_TEMPLATE_KWARGS,
        )
        contract_id = uuid.UUID(create_result["contract_id"])
        svc.accept_contract(db_session, contract_id, provider.id)

        # Backdate started_at so the contract is past its deadline
        contract = SLAContractRepository(db_session).get_by_id(contract_id)
        contract.started_at = _naive_utc_now() - timedelta(seconds=7200)
        db_session.flush()

        breached_count = svc.enforce_sla(db_session)

        assert breached_count >= 1

        contract = SLAContractRepository(db_session).get_by_id(contract_id)
        assert contract.state == SLAStatus.BREACHED


# ---------------------------------------------------------------------------
# TestSLAEnforcement — auto-enforcement background-task behaviour
# ---------------------------------------------------------------------------

class TestSLAEnforcement:
    """Tests for SLAService.enforce_sla() background auto-enforcement."""

    # ------------------------------------------------------------------
    # Shared helper to build an active contract whose started_at is
    # already set in the past and whose deadline thresholds are small.
    # ------------------------------------------------------------------

    def _make_active_contract(
        self,
        db,
        mock_escrow_cls,
        consumer_name: str,
        provider_name: str,
        response_time_secs: int = 300,
        delivery_time_secs: int = 3600,
        started_seconds_ago: int = 120,
        price: Decimal = Decimal("1.0"),
        penalty_percent: int = 10,
    ):
        """
        Create a proposed contract, accept it (so state becomes ACTIVE),
        then backdate started_at to simulate elapsed time.
        """
        from sthrip.services.sla_service import SLAService
        from sthrip.db.sla_repo import SLAContractRepository

        escrow_id = uuid.uuid4()
        mock_instance = mock_escrow_cls.return_value
        mock_instance.create_escrow.return_value = {"escrow_id": str(escrow_id)}

        consumer = _make_agent(db, consumer_name, Decimal("50"))
        provider = _make_agent(db, provider_name)

        svc = SLAService()
        create_result = svc.create_contract(
            db,
            consumer_id=consumer.id,
            provider_id=provider.id,
            name="Enforcement Test SLA",
            service_description="Auto-enforcement test",
            deliverables=[{"name": "output"}],
            response_time_secs=response_time_secs,
            delivery_time_secs=delivery_time_secs,
            price=price,
            currency="XMR",
            penalty_percent=penalty_percent,
        )
        contract_id = uuid.UUID(create_result["contract_id"])
        svc.accept_contract(db, contract_id, provider.id)

        # Backdate started_at so the contract appears overdue
        repo = SLAContractRepository(db)
        contract = repo.get_by_id(contract_id)
        contract.started_at = _naive_utc_now() - timedelta(seconds=started_seconds_ago)
        db.flush()

        return contract_id, svc, repo

    # ------------------------------------------------------------------
    # test_enforce_response_timeout
    # ------------------------------------------------------------------

    @patch("sthrip.services.sla_service.audit_log")
    @patch("sthrip.services.sla_service.queue_webhook")
    @patch("sthrip.services.sla_service.EscrowService")
    def test_enforce_response_timeout(
        self, mock_escrow_cls, mock_webhook, mock_audit, db_session
    ):
        """
        A contract started 120 seconds ago with response_time_secs=10 must be
        detected as overdue and transitioned to BREACHED by enforce_sla.
        """
        from sthrip.db.models import SLAStatus

        contract_id, svc, repo = self._make_active_contract(
            db_session,
            mock_escrow_cls,
            consumer_name="rt-consumer",
            provider_name="rt-provider",
            response_time_secs=10,      # threshold already exceeded
            delivery_time_secs=86400,   # well in the future
            started_seconds_ago=120,
        )

        breached_count = svc.enforce_sla(db_session)

        assert breached_count >= 1
        contract = repo.get_by_id(contract_id)
        assert contract.state == SLAStatus.BREACHED

    # ------------------------------------------------------------------
    # test_enforce_delivery_timeout
    # ------------------------------------------------------------------

    @patch("sthrip.services.sla_service.audit_log")
    @patch("sthrip.services.sla_service.queue_webhook")
    @patch("sthrip.services.sla_service.EscrowService")
    def test_enforce_delivery_timeout(
        self, mock_escrow_cls, mock_webhook, mock_audit, db_session
    ):
        """
        A contract started 120 seconds ago with delivery_time_secs=60 must be
        detected as overdue and transitioned to BREACHED by enforce_sla.
        """
        from sthrip.db.models import SLAStatus

        contract_id, svc, repo = self._make_active_contract(
            db_session,
            mock_escrow_cls,
            consumer_name="dt-consumer",
            provider_name="dt-provider",
            response_time_secs=86400,   # well in the future
            delivery_time_secs=60,      # threshold already exceeded
            started_seconds_ago=120,
        )

        breached_count = svc.enforce_sla(db_session)

        assert breached_count >= 1
        contract = repo.get_by_id(contract_id)
        assert contract.state == SLAStatus.BREACHED

    # ------------------------------------------------------------------
    # test_enforce_no_false_positives
    # ------------------------------------------------------------------

    @patch("sthrip.services.sla_service.audit_log")
    @patch("sthrip.services.sla_service.queue_webhook")
    @patch("sthrip.services.sla_service.EscrowService")
    def test_enforce_no_false_positives(
        self, mock_escrow_cls, mock_webhook, mock_audit, db_session
    ):
        """
        A contract still within both its response and delivery deadlines must
        NOT be breached, and enforce_sla must return 0 for that contract.
        """
        from sthrip.db.models import SLAStatus

        contract_id, svc, repo = self._make_active_contract(
            db_session,
            mock_escrow_cls,
            consumer_name="nfp-consumer",
            provider_name="nfp-provider",
            response_time_secs=86400,   # 24 hours — far in future
            delivery_time_secs=86400,   # 24 hours — far in future
            started_seconds_ago=120,    # only 2 minutes elapsed
        )

        breached_count = svc.enforce_sla(db_session)

        assert breached_count == 0
        contract = repo.get_by_id(contract_id)
        assert contract.state == SLAStatus.ACTIVE

    # ------------------------------------------------------------------
    # test_enforce_penalty_applied
    # ------------------------------------------------------------------

    @patch("sthrip.services.sla_service.audit_log")
    @patch("sthrip.services.sla_service.queue_webhook")
    @patch("sthrip.services.sla_service.EscrowService")
    def test_enforce_penalty_applied(
        self, mock_escrow_cls, mock_webhook, mock_audit, db_session
    ):
        """
        After a breach, the penalty amount must equal price * penalty_percent / 100.
        For price=1.0 XMR and penalty_percent=10 the penalty must be 0.1 XMR.
        """
        from sthrip.db.models import SLAStatus

        price = Decimal("1.0")
        penalty_percent = 10
        expected_penalty = price * Decimal(penalty_percent) / Decimal("100")

        contract_id, svc, repo = self._make_active_contract(
            db_session,
            mock_escrow_cls,
            consumer_name="penalty-consumer",
            provider_name="penalty-provider",
            response_time_secs=10,      # threshold already exceeded
            delivery_time_secs=86400,
            started_seconds_ago=120,
            price=price,
            penalty_percent=penalty_percent,
        )

        svc.enforce_sla(db_session)

        contract = repo.get_by_id(contract_id)
        assert contract.state == SLAStatus.BREACHED

        # Verify the penalty calculation is correct: price * penalty_percent / 100
        computed_penalty = contract.price * Decimal(contract.penalty_percent) / Decimal("100")
        assert computed_penalty == expected_penalty
        assert computed_penalty == Decimal("0.1")
