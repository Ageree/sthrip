"""Tests for SLA template and contract repositories."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance,
    SLATemplate, SLAContract, EscrowDeal,
)


@pytest.fixture
def db_session():
    """In-memory SQLite session with SLA tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        Agent.__table__,
        AgentReputation.__table__,
        AgentBalance.__table__,
        EscrowDeal.__table__,
        SLATemplate.__table__,
        SLAContract.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    yield session
    session.close()


def _make_agent(db_session, name: str) -> Agent:
    """Create a test agent with balance and reputation."""
    agent = Agent(
        id=uuid.uuid4(),
        agent_name=name,
        api_key_hash="hash_" + name,
        is_active=True,
    )
    db_session.add(agent)
    db_session.flush()
    rep = AgentReputation(agent_id=agent.id)
    bal = AgentBalance(agent_id=agent.id, available=Decimal("100"))
    db_session.add_all([rep, bal])
    db_session.flush()
    return agent


class TestSLATemplateRepo:
    """Tests for SLATemplateRepository CRUD."""

    def test_create_template(self, db_session):
        from sthrip.db.sla_repo import SLATemplateRepository
        provider = _make_agent(db_session, "provider-1")
        repo = SLATemplateRepository(db_session)

        tmpl = repo.create(
            provider_id=provider.id,
            name="Market Analysis Report",
            service_description="Detailed market analysis",
            deliverables=[{"name": "report", "format": "json"}],
            response_time_secs=300,
            delivery_time_secs=3600,
            base_price=Decimal("0.5"),
            currency="XMR",
            penalty_percent=10,
        )

        assert tmpl.id is not None
        assert tmpl.name == "Market Analysis Report"
        assert tmpl.base_price == Decimal("0.5")
        assert tmpl.is_active is True

    def test_list_by_provider(self, db_session):
        from sthrip.db.sla_repo import SLATemplateRepository
        provider_a = _make_agent(db_session, "prov-a")
        provider_b = _make_agent(db_session, "prov-b")
        repo = SLATemplateRepository(db_session)

        repo.create(provider_a.id, "Svc 1", "desc", [], 60, 120, Decimal("1"), "XMR", 10)
        repo.create(provider_a.id, "Svc 2", "desc", [], 60, 120, Decimal("2"), "XMR", 5)
        repo.create(provider_b.id, "Svc 3", "desc", [], 60, 120, Decimal("3"), "XMR", 10)

        items_a, count_a = repo.list_by_provider(provider_a.id)
        items_b, count_b = repo.list_by_provider(provider_b.id)

        assert count_a == 2
        assert count_b == 1
        assert all(t.provider_id == provider_a.id for t in items_a)

    def test_get_by_id(self, db_session):
        from sthrip.db.sla_repo import SLATemplateRepository
        provider = _make_agent(db_session, "prov-get")
        repo = SLATemplateRepository(db_session)

        tmpl = repo.create(provider.id, "Get Test", "desc", [], 60, 120, Decimal("1"), "XMR", 10)
        found = repo.get_by_id(tmpl.id)

        assert found is not None
        assert found.id == tmpl.id
        assert found.name == "Get Test"

    def test_deactivate(self, db_session):
        from sthrip.db.sla_repo import SLATemplateRepository
        provider = _make_agent(db_session, "prov-deact")
        repo = SLATemplateRepository(db_session)

        tmpl = repo.create(provider.id, "Deact Test", "desc", [], 60, 120, Decimal("1"), "XMR", 10)
        rows = repo.deactivate(tmpl.id)

        assert rows == 1
        updated = repo.get_by_id(tmpl.id)
        assert updated.is_active is False

        # Deactivated templates should not appear in active-only listing
        items, count = repo.list_by_provider(provider.id, active_only=True)
        assert count == 0


def _make_template(db_session, provider_id, name="Test Svc") -> SLATemplate:
    """Helper to create an SLA template."""
    from sthrip.db.sla_repo import SLATemplateRepository
    repo = SLATemplateRepository(db_session)
    return repo.create(provider_id, name, "desc", [], 300, 3600, Decimal("0.5"), "XMR", 10)


class TestSLAContractRepo:
    """Tests for SLAContractRepository CRUD and state transitions."""

    def test_create_contract(self, db_session):
        from sthrip.db.sla_repo import SLAContractRepository
        provider = _make_agent(db_session, "c-prov")
        consumer = _make_agent(db_session, "c-cons")
        repo = SLAContractRepository(db_session)

        contract = repo.create(
            provider_id=provider.id,
            consumer_id=consumer.id,
            template_id=None,
            service_description="Custom service",
            deliverables=[],
            response_time_secs=300,
            delivery_time_secs=3600,
            price=Decimal("1.0"),
            currency="XMR",
            penalty_percent=10,
            escrow_deal_id=None,
        )
        assert contract.id is not None
        assert contract.state.value == "proposed"
        assert contract.price == Decimal("1.0")

    def test_create_from_template(self, db_session):
        from sthrip.db.sla_repo import SLAContractRepository
        provider = _make_agent(db_session, "ct-prov")
        consumer = _make_agent(db_session, "ct-cons")
        tmpl = _make_template(db_session, provider.id)
        repo = SLAContractRepository(db_session)

        contract = repo.create(
            provider_id=provider.id,
            consumer_id=consumer.id,
            template_id=tmpl.id,
            service_description=tmpl.service_description,
            deliverables=tmpl.deliverables,
            response_time_secs=tmpl.response_time_secs,
            delivery_time_secs=tmpl.delivery_time_secs,
            price=tmpl.base_price,
            currency=tmpl.currency,
            penalty_percent=tmpl.penalty_percent,
            escrow_deal_id=None,
        )
        assert contract.template_id == tmpl.id

    def test_accept(self, db_session):
        from sthrip.db.sla_repo import SLAContractRepository
        provider = _make_agent(db_session, "a-prov")
        consumer = _make_agent(db_session, "a-cons")
        repo = SLAContractRepository(db_session)

        contract = repo.create(
            provider.id, consumer.id, None, "svc", [], 300, 3600,
            Decimal("1"), "XMR", 10, None,
        )
        rows = repo.accept(contract.id)
        assert rows == 1

        updated = repo.get_by_id(contract.id)
        assert updated.state.value == "accepted"

    def test_activate(self, db_session):
        from sthrip.db.sla_repo import SLAContractRepository
        provider = _make_agent(db_session, "act-prov")
        consumer = _make_agent(db_session, "act-cons")
        repo = SLAContractRepository(db_session)

        contract = repo.create(
            provider.id, consumer.id, None, "svc", [], 300, 3600,
            Decimal("1"), "XMR", 10, None,
        )
        repo.accept(contract.id)
        rows = repo.activate(contract.id)
        assert rows == 1

        updated = repo.get_by_id(contract.id)
        assert updated.state.value == "active"
        assert updated.started_at is not None

    def test_deliver(self, db_session):
        from sthrip.db.sla_repo import SLAContractRepository
        provider = _make_agent(db_session, "del-prov")
        consumer = _make_agent(db_session, "del-cons")
        repo = SLAContractRepository(db_session)

        contract = repo.create(
            provider.id, consumer.id, None, "svc", [], 300, 3600,
            Decimal("1"), "XMR", 10, None,
        )
        repo.accept(contract.id)
        repo.activate(contract.id)
        rows = repo.deliver(contract.id, "sha256:abc123")
        assert rows == 1

        updated = repo.get_by_id(contract.id)
        assert updated.state.value == "delivered"
        assert updated.delivered_at is not None
        assert updated.result_hash == "sha256:abc123"

    def test_complete(self, db_session):
        from sthrip.db.sla_repo import SLAContractRepository
        provider = _make_agent(db_session, "comp-prov")
        consumer = _make_agent(db_session, "comp-cons")
        repo = SLAContractRepository(db_session)

        contract = repo.create(
            provider.id, consumer.id, None, "svc", [], 300, 3600,
            Decimal("1"), "XMR", 10, None,
        )
        repo.accept(contract.id)
        repo.activate(contract.id)
        repo.deliver(contract.id, "sha256:abc")
        rows = repo.complete(contract.id, sla_met=True)
        assert rows == 1

        updated = repo.get_by_id(contract.id)
        assert updated.state.value == "completed"
        assert updated.sla_met is True

    def test_breach(self, db_session):
        from sthrip.db.sla_repo import SLAContractRepository
        provider = _make_agent(db_session, "br-prov")
        consumer = _make_agent(db_session, "br-cons")
        repo = SLAContractRepository(db_session)

        contract = repo.create(
            provider.id, consumer.id, None, "svc", [], 300, 3600,
            Decimal("1"), "XMR", 10, None,
        )
        repo.accept(contract.id)
        repo.activate(contract.id)
        rows = repo.breach(contract.id)
        assert rows == 1

        updated = repo.get_by_id(contract.id)
        assert updated.state.value == "breached"

    def test_list_by_agent(self, db_session):
        from sthrip.db.sla_repo import SLAContractRepository
        provider = _make_agent(db_session, "lst-prov")
        consumer = _make_agent(db_session, "lst-cons")
        repo = SLAContractRepository(db_session)

        repo.create(provider.id, consumer.id, None, "svc1", [], 300, 3600, Decimal("1"), "XMR", 10, None)
        repo.create(provider.id, consumer.id, None, "svc2", [], 300, 3600, Decimal("2"), "XMR", 10, None)

        items_prov, count_prov = repo.list_by_agent(provider.id)
        items_cons, count_cons = repo.list_by_agent(consumer.id)

        assert count_prov == 2
        assert count_cons == 2

    def test_get_active_past_deadline(self, db_session):
        from sthrip.db.sla_repo import SLAContractRepository
        provider = _make_agent(db_session, "dl-prov")
        consumer = _make_agent(db_session, "dl-cons")
        repo = SLAContractRepository(db_session)

        contract = repo.create(
            provider.id, consumer.id, None, "svc", [], 10, 60,
            Decimal("1"), "XMR", 10, None,
        )
        repo.accept(contract.id)
        repo.activate(contract.id)

        # Manually set started_at to the past
        c = repo.get_by_id(contract.id)
        c.started_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        db_session.flush()

        overdue = repo.get_active_past_deadline()
        assert len(overdue) >= 1
        assert any(o.id == contract.id for o in overdue)
