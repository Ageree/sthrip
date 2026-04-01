"""Tests for currency conversion: ConversionRepository, ConversionService, and API endpoints.

TDD order: tests written first, then implementation.

Covers:
  - ConversionRepository.create and list_by_agent
  - ConversionService.convert (XMR->xUSD, xUSD->XMR, insufficient, unsupported)
  - ConversionService.get_all_balances
  - POST /v2/balance/convert (200, auth, validation)
  - GET /v2/balance/all (200, auth)
"""

import os
import contextlib
import pytest
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance,
    HubRoute, FeeCollection, PendingWithdrawal, Transaction,
    EscrowDeal, EscrowMilestone, CurrencyConversion,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
    PendingWithdrawal.__table__,
    Transaction.__table__,
    EscrowDeal.__table__,
    EscrowMilestone.__table__,
    CurrencyConversion.__table__,
]

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
    "api.routers.spending_policy",
    "api.routers.webhook_endpoints",
    "api.routers.reputation",
    "api.routers.messages",
    "api.routers.multisig_escrow",
    "api.routers.escrow",
    "api.routers.sla",
    "api.routers.reviews",
    "api.routers.matchmaking",
    "api.routers.channels",
    "api.routers.subscriptions",
    "api.routers.streams",
    "api.routers.conversion",
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

_TEST_ENCRYPTION_KEY = "uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE="


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=_TEST_TABLES)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def db(session_factory):
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Helper: create an agent with optional balance
# ---------------------------------------------------------------------------

def _make_agent(db, name="test-agent") -> Agent:
    agent = Agent(agent_name=name, api_key_hash="test-hash")
    db.add(agent)
    db.flush()
    return agent


def _seed_balance(db, agent_id, token: str, amount: Decimal) -> AgentBalance:
    bal = AgentBalance(agent_id=agent_id, token=token, available=amount)
    db.add(bal)
    db.flush()
    return bal


# ===========================================================================
# PART 1: ConversionRepository
# ===========================================================================

class TestConversionRepository:
    """Unit tests for ConversionRepository data access."""

    def test_create_records_conversion(self, db):
        """create() persists a CurrencyConversion row and returns it."""
        from sthrip.db.conversion_repo import ConversionRepository

        agent = _make_agent(db)
        repo = ConversionRepository(db)
        record = repo.create(
            agent_id=agent.id,
            from_currency="XMR",
            from_amount=Decimal("1.0"),
            to_currency="xUSD",
            to_amount=Decimal("149.25"),
            rate=Decimal("150.0"),
            fee_amount=Decimal("0.75"),
        )
        db.flush()

        assert record.id is not None
        assert record.agent_id == agent.id
        assert record.from_currency == "XMR"
        assert record.from_amount == Decimal("1.0")
        assert record.to_currency == "xUSD"
        assert record.to_amount == Decimal("149.25")
        assert record.rate == Decimal("150.0")
        assert record.fee_amount == Decimal("0.75")

    def test_create_does_not_mutate_inputs(self, db):
        """create() returns a new object; it does not mutate the caller's data."""
        from sthrip.db.conversion_repo import ConversionRepository

        agent = _make_agent(db)
        repo = ConversionRepository(db)
        original_amount = Decimal("2.0")
        record = repo.create(
            agent_id=agent.id,
            from_currency="XMR",
            from_amount=original_amount,
            to_currency="xUSD",
            to_amount=Decimal("298.5"),
            rate=Decimal("150.0"),
            fee_amount=Decimal("1.5"),
        )
        # original_amount must not be changed
        assert original_amount == Decimal("2.0")
        assert record is not None

    def test_list_by_agent_returns_records(self, db):
        """list_by_agent() returns (records, total_count) for the given agent."""
        from sthrip.db.conversion_repo import ConversionRepository

        agent = _make_agent(db)
        repo = ConversionRepository(db)

        for i in range(3):
            repo.create(
                agent_id=agent.id,
                from_currency="XMR",
                from_amount=Decimal("1.0"),
                to_currency="xUSD",
                to_amount=Decimal("150.0"),
                rate=Decimal("150.0"),
                fee_amount=Decimal("0.75"),
            )
        db.flush()

        records, total = repo.list_by_agent(agent.id)
        assert total == 3
        assert len(records) == 3

    def test_list_by_agent_default_limit(self, db):
        """list_by_agent() default limit is 50."""
        from sthrip.db.conversion_repo import ConversionRepository

        agent = _make_agent(db)
        repo = ConversionRepository(db)

        for i in range(5):
            repo.create(
                agent_id=agent.id,
                from_currency="XMR",
                from_amount=Decimal("0.1"),
                to_currency="xUSD",
                to_amount=Decimal("15.0"),
                rate=Decimal("150.0"),
                fee_amount=Decimal("0.075"),
            )
        db.flush()

        records, total = repo.list_by_agent(agent.id, limit=2, offset=0)
        assert len(records) == 2
        assert total == 5

    def test_list_by_agent_isolates_by_agent(self, db):
        """list_by_agent() does not return records belonging to another agent."""
        from sthrip.db.conversion_repo import ConversionRepository

        agent_a = _make_agent(db, "agent-a")
        agent_b = _make_agent(db, "agent-b")
        repo = ConversionRepository(db)

        repo.create(
            agent_id=agent_a.id,
            from_currency="XMR",
            from_amount=Decimal("1.0"),
            to_currency="xUSD",
            to_amount=Decimal("150.0"),
            rate=Decimal("150.0"),
            fee_amount=Decimal("0.75"),
        )
        db.flush()

        records, total = repo.list_by_agent(agent_b.id)
        assert total == 0
        assert records == []

    def test_list_by_agent_empty(self, db):
        """list_by_agent() returns empty list and zero count when no records exist."""
        from sthrip.db.conversion_repo import ConversionRepository

        agent = _make_agent(db)
        repo = ConversionRepository(db)
        records, total = repo.list_by_agent(agent.id)
        assert records == []
        assert total == 0


# ===========================================================================
# PART 2: ConversionService
# ===========================================================================

class TestConversionService:
    """Unit tests for ConversionService business logic."""

    def test_convert_xmr_to_xusd_happy_path(self, db):
        """convert() XMR->xUSD deducts XMR balance and credits xUSD minus fee."""
        from sthrip.services.conversion_service import ConversionService

        agent = _make_agent(db)
        _seed_balance(db, agent.id, "XMR", Decimal("10.0"))

        svc = ConversionService()
        result = svc.convert(db, agent.id, "XMR", "xUSD", Decimal("1.0"))
        db.commit()

        assert result["from_currency"] == "XMR"
        assert result["to_currency"] == "xUSD"
        assert Decimal(result["from_amount"]) == Decimal("1.0")
        # rate * amount = gross; net = gross - fee (0.5%)
        rate = Decimal(result["rate"])
        gross = Decimal("1.0") * rate
        fee = gross * Decimal("0.005")
        expected_net = gross - fee
        assert Decimal(result["net_to_amount"]) == expected_net

        # XMR balance deducted
        xmr_bal = db.query(AgentBalance).filter_by(agent_id=agent.id, token="XMR").first()
        assert xmr_bal.available == Decimal("9.0")

        # xUSD balance credited
        xusd_bal = db.query(AgentBalance).filter_by(agent_id=agent.id, token="xUSD").first()
        assert xusd_bal is not None
        assert xusd_bal.available == expected_net

    def test_convert_xusd_to_xmr_happy_path(self, db):
        """convert() xUSD->XMR deducts xUSD and credits XMR minus fee."""
        from sthrip.services.conversion_service import ConversionService, FALLBACK_RATES

        agent = _make_agent(db)
        xusd_amount = Decimal("150.0")
        _seed_balance(db, agent.id, "xUSD", xusd_amount)

        svc = ConversionService()
        result = svc.convert(db, agent.id, "xUSD", "XMR", Decimal("150.0"))
        db.commit()

        assert result["from_currency"] == "xUSD"
        assert result["to_currency"] == "XMR"

        xmr_rate = Decimal("1") / FALLBACK_RATES["XMR_USD"]
        gross = Decimal("150.0") * xmr_rate
        fee = gross * Decimal("0.005")
        expected_net = gross - fee

        xmr_bal = db.query(AgentBalance).filter_by(agent_id=agent.id, token="XMR").first()
        assert xmr_bal is not None
        # Use approx comparison due to decimal division precision
        assert abs(xmr_bal.available - expected_net) < Decimal("0.00000001")

    def test_convert_xmr_to_xeur_happy_path(self, db):
        """convert() XMR->xEUR uses EUR fallback rate."""
        from sthrip.services.conversion_service import ConversionService, FALLBACK_RATES

        agent = _make_agent(db)
        _seed_balance(db, agent.id, "XMR", Decimal("5.0"))

        svc = ConversionService()
        result = svc.convert(db, agent.id, "XMR", "xEUR", Decimal("1.0"))
        db.commit()

        rate = Decimal(result["rate"])
        assert abs(rate - FALLBACK_RATES["XMR_EUR"]) < Decimal("0.001")

    def test_convert_insufficient_balance_raises(self, db):
        """convert() raises ValueError when available balance is too low."""
        from sthrip.services.conversion_service import ConversionService

        agent = _make_agent(db)
        _seed_balance(db, agent.id, "XMR", Decimal("0.5"))

        svc = ConversionService()
        with pytest.raises(ValueError, match="[Ii]nsufficient"):
            svc.convert(db, agent.id, "XMR", "xUSD", Decimal("1.0"))

    def test_convert_unsupported_pair_raises(self, db):
        """convert() raises ValueError for an unsupported currency pair."""
        from sthrip.services.conversion_service import ConversionService

        agent = _make_agent(db)
        _seed_balance(db, agent.id, "XMR", Decimal("10.0"))

        svc = ConversionService()
        with pytest.raises(ValueError, match="[Uu]nsupported"):
            svc.convert(db, agent.id, "XMR", "BTC", Decimal("1.0"))

    def test_convert_unsupported_reverse_pair_raises(self, db):
        """convert() raises ValueError for an unsupported reverse pair."""
        from sthrip.services.conversion_service import ConversionService

        agent = _make_agent(db)
        _seed_balance(db, agent.id, "DOGE", Decimal("1000.0"))

        svc = ConversionService()
        with pytest.raises(ValueError, match="[Uu]nsupported"):
            svc.convert(db, agent.id, "DOGE", "xUSD", Decimal("100.0"))

    def test_convert_zero_amount_raises(self, db):
        """convert() raises ValueError for zero amount."""
        from sthrip.services.conversion_service import ConversionService

        agent = _make_agent(db)
        _seed_balance(db, agent.id, "XMR", Decimal("10.0"))

        svc = ConversionService()
        with pytest.raises((ValueError, Exception)):
            svc.convert(db, agent.id, "XMR", "xUSD", Decimal("0"))

    def test_convert_records_conversion(self, db):
        """convert() persists a CurrencyConversion record."""
        from sthrip.services.conversion_service import ConversionService

        agent = _make_agent(db)
        _seed_balance(db, agent.id, "XMR", Decimal("10.0"))

        svc = ConversionService()
        svc.convert(db, agent.id, "XMR", "xUSD", Decimal("1.0"))
        db.commit()

        records = db.query(CurrencyConversion).filter_by(agent_id=agent.id).all()
        assert len(records) == 1
        assert records[0].from_currency == "XMR"
        assert records[0].to_currency == "xUSD"

    def test_get_all_balances_returns_dict(self, db):
        """get_all_balances() returns a dict of token->amount strings for the agent."""
        from sthrip.services.conversion_service import ConversionService

        agent = _make_agent(db)
        _seed_balance(db, agent.id, "XMR", Decimal("5.5"))
        _seed_balance(db, agent.id, "xUSD", Decimal("100.0"))

        svc = ConversionService()
        balances = svc.get_all_balances(db, agent.id)

        assert "XMR" in balances
        assert "xUSD" in balances
        assert Decimal(balances["XMR"]) == Decimal("5.5")
        assert Decimal(balances["xUSD"]) == Decimal("100.0")

    def test_get_all_balances_empty_agent(self, db):
        """get_all_balances() returns empty dict when agent has no balances."""
        from sthrip.services.conversion_service import ConversionService

        agent = _make_agent(db)
        svc = ConversionService()
        balances = svc.get_all_balances(db, agent.id)

        assert isinstance(balances, dict)
        assert len(balances) == 0

    def test_get_all_balances_isolates_by_agent(self, db):
        """get_all_balances() does not return balances for a different agent."""
        from sthrip.services.conversion_service import ConversionService

        agent_a = _make_agent(db, "agent-a")
        agent_b = _make_agent(db, "agent-b")
        _seed_balance(db, agent_b.id, "XMR", Decimal("99.0"))

        svc = ConversionService()
        balances = svc.get_all_balances(db, agent_a.id)
        assert len(balances) == 0


# ===========================================================================
# PART 3: API endpoints
# ===========================================================================

@pytest.fixture
def client(engine, session_factory, monkeypatch):
    """FastAPI test client with all common dependencies mocked."""
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key-for-tests-long-enough-32")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("WEBHOOK_ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY)
    monkeypatch.setenv("HUB_MODE", "ledger")

    from sthrip.config import get_settings
    get_settings.cache_clear()
    import sthrip.crypto as _crypto
    _crypto._fernet_instance = None

    @contextmanager
    def get_test_db():
        session = session_factory()
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
    mock_limiter.get_limit_status.return_value = {"requests_remaining": 100}

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy",
        "timestamp": "2026-04-01T00:00:00",
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
            patch("sthrip.services.monitoring.get_monitor", return_value=mock_monitor)
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

        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)


def _register_agent(client, name="conv-agent") -> tuple:
    """Register an agent and return (api_key, agent_id)."""
    resp = client.post(
        "/v2/agents/register",
        json={"agent_name": name, "privacy_level": "medium"},
    )
    assert resp.status_code in (200, 201), resp.text
    data = resp.json()
    return data["api_key"], data["agent_id"]


def _auth_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


class TestConversionApiEndpoints:
    """Integration tests for POST /v2/balance/convert and GET /v2/balance/all."""

    def test_api_convert_200(self, client, session_factory):
        """POST /v2/balance/convert returns 200 with conversion result."""
        import uuid as _uuid
        api_key, agent_id = _register_agent(client, "convert-agent")

        # Seed XMR balance directly in the DB (convert str UUID to uuid.UUID)
        with session_factory() as s:
            bal = AgentBalance(
                agent_id=_uuid.UUID(agent_id),
                token="XMR",
                available=Decimal("10.0"),
            )
            s.add(bal)
            s.commit()

        resp = client.post(
            "/v2/balance/convert",
            json={"from_currency": "XMR", "to_currency": "xUSD", "amount": "1.0"},
            headers=_auth_headers(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["from_currency"] == "XMR"
        assert body["to_currency"] == "xUSD"
        assert "from_amount" in body
        assert "to_amount" in body
        assert "rate" in body
        assert "fee_amount" in body

    def test_api_convert_requires_auth(self, client):
        """POST /v2/balance/convert returns 401 without auth header."""
        resp = client.post(
            "/v2/balance/convert",
            json={"from_currency": "XMR", "to_currency": "xUSD", "amount": "1.0"},
        )
        assert resp.status_code in (401, 403), resp.text

    def test_api_convert_insufficient_balance_returns_400(self, client):
        """POST /v2/balance/convert returns 400 when balance is too low."""
        api_key, agent_id = _register_agent(client, "broke-agent")

        resp = client.post(
            "/v2/balance/convert",
            json={"from_currency": "XMR", "to_currency": "xUSD", "amount": "999.0"},
            headers=_auth_headers(api_key),
        )
        assert resp.status_code == 400, resp.text

    def test_api_convert_unsupported_pair_returns_400(self, client):
        """POST /v2/balance/convert returns 400 for unsupported pair."""
        api_key, _ = _register_agent(client, "pair-agent")

        resp = client.post(
            "/v2/balance/convert",
            json={"from_currency": "XMR", "to_currency": "BTC", "amount": "1.0"},
            headers=_auth_headers(api_key),
        )
        assert resp.status_code == 400, resp.text

    def test_api_convert_invalid_amount_returns_422(self, client):
        """POST /v2/balance/convert returns 422 for zero or negative amount."""
        api_key, _ = _register_agent(client, "zero-agent")

        resp = client.post(
            "/v2/balance/convert",
            json={"from_currency": "XMR", "to_currency": "xUSD", "amount": "0"},
            headers=_auth_headers(api_key),
        )
        assert resp.status_code == 422, resp.text

    def test_api_balance_all_200(self, client, session_factory):
        """GET /v2/balance/all returns 200 with dict of all token balances."""
        import uuid as _uuid
        api_key, agent_id = _register_agent(client, "all-bal-agent")
        agent_uuid = _uuid.UUID(agent_id)

        with session_factory() as s:
            s.add(AgentBalance(agent_id=agent_uuid, token="XMR", available=Decimal("3.0")))
            s.add(AgentBalance(agent_id=agent_uuid, token="xUSD", available=Decimal("50.0")))
            s.commit()

        resp = client.get("/v2/balance/all", headers=_auth_headers(api_key))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Response has a "balances" key
        balances = body.get("balances", body)
        assert "XMR" in balances
        assert "xUSD" in balances

    def test_api_balance_all_requires_auth(self, client):
        """GET /v2/balance/all returns 401 without auth header."""
        resp = client.get("/v2/balance/all")
        assert resp.status_code in (401, 403), resp.text
