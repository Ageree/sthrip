"""Tests for Treasury Management: TreasuryService and API endpoints.

TDD order: tests written first, then implementation.

Covers:
  - TreasuryService.set_policy (valid, invalid allocation)
  - TreasuryService.get_policy
  - TreasuryService.deactivate_policy
  - TreasuryService.get_status
  - TreasuryService.rebalance (executes conversion, skipped below threshold, skipped cooldown)
  - TreasuryService.rebalance respects emergency reserve
  - TreasuryService.get_history
  - API: PUT /v2/me/treasury/policy (200)
  - API: GET /v2/me/treasury/status
  - API: POST /v2/me/treasury/rebalance
  - API: GET /v2/me/treasury/history
"""

import os
import contextlib
import pytest
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
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
    SpendingPolicy, WebhookEndpoint, MessageRelay,
    EscrowDeal, EscrowMilestone, MultisigEscrow, MultisigRound,
    SLATemplate, SLAContract,
    AgentReview, AgentRatingSummary,
    MatchRequest,
    RecurringPayment,
    PaymentChannel, ChannelUpdate,
    PaymentStream,
    CurrencyConversion,
    SwapOrder,
    TreasuryPolicy, TreasuryForecast, TreasuryRebalanceLog,
    AgentCreditScore, AgentLoan, LendingOffer,
    ConditionalPayment,
    MultiPartyPayment, MultiPartyRecipient,
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
    SpendingPolicy.__table__,
    WebhookEndpoint.__table__,
    MessageRelay.__table__,
    EscrowDeal.__table__,
    EscrowMilestone.__table__,
    MultisigEscrow.__table__,
    MultisigRound.__table__,
    SLATemplate.__table__,
    SLAContract.__table__,
    AgentReview.__table__,
    AgentRatingSummary.__table__,
    MatchRequest.__table__,
    RecurringPayment.__table__,
    PaymentChannel.__table__,
    ChannelUpdate.__table__,
    PaymentStream.__table__,
    CurrencyConversion.__table__,
    SwapOrder.__table__,
    TreasuryPolicy.__table__,
    TreasuryForecast.__table__,
    TreasuryRebalanceLog.__table__,
    AgentCreditScore.__table__,
    AgentLoan.__table__,
    LendingOffer.__table__,
    ConditionalPayment.__table__,
    MultiPartyPayment.__table__,
    MultiPartyRecipient.__table__,
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
    "api.routers.swap",
    "api.routers.lending",
    "api.routers.treasury",
    "api.routers.multi_party",
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

def _make_agent(db, name: str = "treasury-agent") -> Agent:
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
# PART 1: TreasuryService unit tests
# ===========================================================================


class TestTreasuryServiceSetPolicy:
    """Tests for TreasuryService.set_policy."""

    def test_set_policy_valid(self, db):
        """set_policy with a valid allocation (sums to 100) creates a policy and returns dict."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)
        svc = TreasuryService()
        result = svc.set_policy(
            db, agent.id,
            allocation={"XMR": 40, "xUSD": 50, "xEUR": 10},
            rebalance_threshold_pct=5,
            cooldown_minutes=60,
            emergency_reserve_pct=10,
        )

        assert result is not None
        assert result["target_allocation"] == {"XMR": 40, "xUSD": 50, "xEUR": 10}
        assert result["rebalance_threshold_pct"] == 5
        assert result["cooldown_minutes"] == 60
        assert result["emergency_reserve_pct"] == 10
        assert result["is_active"] is True

    def test_set_policy_invalid_allocation_sum(self, db):
        """set_policy raises ValueError when allocation values do not sum to 100."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)
        svc = TreasuryService()
        with pytest.raises(ValueError, match="100"):
            svc.set_policy(
                db, agent.id,
                allocation={"XMR": 50, "xUSD": 30},
            )

    def test_set_policy_upsert(self, db):
        """set_policy updates existing policy instead of creating a second one."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)
        svc = TreasuryService()
        svc.set_policy(db, agent.id, allocation={"XMR": 60, "xUSD": 40})
        result = svc.set_policy(db, agent.id, allocation={"XMR": 50, "xUSD": 50})

        assert result["target_allocation"] == {"XMR": 50, "xUSD": 50}

        # Only one policy record for this agent
        count = db.query(TreasuryPolicy).filter_by(agent_id=agent.id).count()
        assert count == 1

    def test_set_policy_empty_allocation_raises(self, db):
        """set_policy raises ValueError for empty allocation dict."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)
        svc = TreasuryService()
        with pytest.raises(ValueError):
            svc.set_policy(db, agent.id, allocation={})

    def test_set_policy_negative_values_raises(self, db):
        """set_policy raises ValueError for negative allocation values."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)
        svc = TreasuryService()
        with pytest.raises(ValueError):
            svc.set_policy(db, agent.id, allocation={"XMR": -10, "xUSD": 110})


class TestTreasuryServiceGetPolicy:
    """Tests for TreasuryService.get_policy."""

    def test_get_policy_exists(self, db):
        """get_policy returns policy dict when one exists."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)
        svc = TreasuryService()
        svc.set_policy(db, agent.id, allocation={"XMR": 60, "xUSD": 40})

        result = svc.get_policy(db, agent.id)
        assert result is not None
        assert result["target_allocation"] == {"XMR": 60, "xUSD": 40}
        assert result["is_active"] is True

    def test_get_policy_none(self, db):
        """get_policy returns None when no policy exists."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)
        svc = TreasuryService()
        result = svc.get_policy(db, agent.id)
        assert result is None


class TestTreasuryServiceDeactivatePolicy:
    """Tests for TreasuryService.deactivate_policy."""

    def test_deactivate_policy(self, db):
        """deactivate_policy sets is_active=False."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)
        svc = TreasuryService()
        svc.set_policy(db, agent.id, allocation={"XMR": 100})

        svc.deactivate_policy(db, agent.id)

        result = svc.get_policy(db, agent.id)
        assert result is not None
        assert result["is_active"] is False


class TestTreasuryServiceGetStatus:
    """Tests for TreasuryService.get_status."""

    def test_get_status_with_balances(self, db):
        """get_status returns current balances and allocation percentages."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)
        # Seed balances: 1 XMR (150 USD), 150 xUSD => total 300 USD => 50%/50%
        _seed_balance(db, agent.id, "XMR", Decimal("1.0"))
        _seed_balance(db, agent.id, "xUSD", Decimal("150.0"))

        svc = TreasuryService()
        result = svc.get_status(db, agent.id)

        assert "balances" in result
        assert "allocation_pct" in result
        assert "total_value_xusd" in result

        # With fallback XMR_USD=150, XMR balance = 150 USD, xUSD = 150 USD
        # Total = 300 USD. XMR = 50%, xUSD = 50%
        assert "XMR" in result["balances"]
        assert "xUSD" in result["balances"]

    def test_get_status_no_balances(self, db):
        """get_status returns empty allocation when agent has no balances."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)
        svc = TreasuryService()
        result = svc.get_status(db, agent.id)

        assert result["balances"] == {}
        assert result["allocation_pct"] == {}
        assert Decimal(result["total_value_xusd"]) == Decimal("0")


class TestTreasuryServiceRebalance:
    """Tests for TreasuryService.rebalance."""

    def test_rebalance_executes_conversion(self, db):
        """rebalance executes conversions when drift exceeds threshold."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)

        # Set policy: 50% XMR, 50% xUSD
        svc = TreasuryService()
        svc.set_policy(db, agent.id, allocation={"XMR": 50, "xUSD": 50},
                       rebalance_threshold_pct=5)

        # Seed unbalanced: 2 XMR (300 USD), 0 xUSD => drift is 50%
        _seed_balance(db, agent.id, "XMR", Decimal("2.0"))

        with patch(
            "sthrip.services.treasury_service.ConversionService"
        ) as MockConv:
            mock_conv = MockConv.return_value
            mock_conv.convert.return_value = {
                "from_currency": "XMR",
                "from_amount": "1.0",
                "to_currency": "xUSD",
                "gross_to_amount": "150.0",
                "fee_amount": "0.75",
                "net_to_amount": "149.25",
                "rate": "150.0",
            }

            result = svc.rebalance(db, agent.id, trigger="manual")

        assert result["rebalanced"] is True
        assert len(result["conversions"]) > 0
        mock_conv.convert.assert_called()

    def test_rebalance_skipped_below_threshold(self, db):
        """rebalance does not execute when drift is below threshold."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)

        # Set policy: 50% XMR, 50% xUSD, threshold=10%
        svc = TreasuryService()
        svc.set_policy(db, agent.id, allocation={"XMR": 50, "xUSD": 50},
                       rebalance_threshold_pct=10)

        # Seed nearly balanced: 1 XMR (150 USD), 140 xUSD => total 290 USD
        # XMR = 51.7%, xUSD = 48.3% => drift ~1.7%, well below 10%
        _seed_balance(db, agent.id, "XMR", Decimal("1.0"))
        _seed_balance(db, agent.id, "xUSD", Decimal("140.0"))

        result = svc.rebalance(db, agent.id, trigger="manual")

        assert result["rebalanced"] is False
        assert result["reason"] == "below_threshold"

    def test_rebalance_skipped_cooldown(self, db):
        """rebalance is skipped when cooldown has not elapsed."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)
        svc = TreasuryService()
        svc.set_policy(db, agent.id, allocation={"XMR": 50, "xUSD": 50},
                       rebalance_threshold_pct=5, cooldown_minutes=60)

        # Seed unbalanced
        _seed_balance(db, agent.id, "XMR", Decimal("2.0"))

        # Set last_rebalance_at to now (cooldown not elapsed)
        policy = db.query(TreasuryPolicy).filter_by(agent_id=agent.id).first()
        policy.last_rebalance_at = datetime.now(timezone.utc)
        db.flush()

        result = svc.rebalance(db, agent.id, trigger="manual")

        assert result["rebalanced"] is False
        assert result["reason"] == "cooldown"

    def test_rebalance_respects_emergency_reserve(self, db):
        """rebalance does not convert more XMR than allowed by emergency reserve."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)
        svc = TreasuryService()
        # 10% emergency reserve in XMR, target: 10% XMR, 90% xUSD
        svc.set_policy(db, agent.id, allocation={"XMR": 10, "xUSD": 90},
                       rebalance_threshold_pct=5, emergency_reserve_pct=10)

        # 2 XMR (300 USD), 0 xUSD. Target: 30 USD XMR (0.2 XMR), 270 USD xUSD
        # Emergency reserve: 10% of total = 30 USD = 0.2 XMR
        # So we should not sell below 0.2 XMR reserve
        _seed_balance(db, agent.id, "XMR", Decimal("2.0"))

        with patch(
            "sthrip.services.treasury_service.ConversionService"
        ) as MockConv:
            mock_conv = MockConv.return_value
            mock_conv.convert.return_value = {
                "from_currency": "XMR",
                "from_amount": "1.6",
                "to_currency": "xUSD",
                "gross_to_amount": "240.0",
                "fee_amount": "1.2",
                "net_to_amount": "238.8",
                "rate": "150.0",
            }

            result = svc.rebalance(db, agent.id, trigger="manual")

        assert result["rebalanced"] is True
        # Verify the conversion amount respects the reserve:
        # We need to keep at least emergency_reserve_pct (10%) of total value in XMR.
        # Total = 300 USD, 10% = 30 USD = 0.2 XMR.
        # Current XMR = 2.0, max sell = 2.0 - 0.2 = 1.8 XMR
        # Target XMR = 10% of 300 = 30 USD = 0.2 XMR, so sell = 2.0 - 0.2 = 1.8
        # But emergency reserve says keep 0.2 XMR. So sell <= 1.8 XMR.
        if mock_conv.convert.called:
            call_args = mock_conv.convert.call_args
            amount_to_sell = call_args[0][4] if len(call_args[0]) > 4 else call_args[1].get("amount")
            # Amount sold should not exceed 1.8 XMR (keeping 0.2 XMR reserve)
            assert amount_to_sell <= Decimal("1.8")

    def test_rebalance_no_policy_raises(self, db):
        """rebalance raises ValueError when no policy exists."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)
        svc = TreasuryService()

        with pytest.raises(ValueError, match="[Nn]o.*policy"):
            svc.rebalance(db, agent.id, trigger="manual")

    def test_rebalance_logs_entry(self, db):
        """rebalance creates a TreasuryRebalanceLog entry on successful execution."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)
        svc = TreasuryService()
        svc.set_policy(db, agent.id, allocation={"XMR": 50, "xUSD": 50},
                       rebalance_threshold_pct=5)

        _seed_balance(db, agent.id, "XMR", Decimal("2.0"))

        with patch(
            "sthrip.services.treasury_service.ConversionService"
        ) as MockConv:
            mock_conv = MockConv.return_value
            mock_conv.convert.return_value = {
                "from_currency": "XMR",
                "from_amount": "1.0",
                "to_currency": "xUSD",
                "gross_to_amount": "150.0",
                "fee_amount": "0.75",
                "net_to_amount": "149.25",
                "rate": "150.0",
            }

            svc.rebalance(db, agent.id, trigger="manual")
        db.flush()

        logs = db.query(TreasuryRebalanceLog).filter_by(agent_id=agent.id).all()
        assert len(logs) == 1
        assert logs[0].trigger == "manual"


class TestTreasuryServiceGetHistory:
    """Tests for TreasuryService.get_history."""

    def test_get_history_returns_entries(self, db):
        """get_history returns rebalance log entries."""
        from sthrip.services.treasury_service import TreasuryService
        from sthrip.db.treasury_repo import TreasuryRepository

        agent = _make_agent(db)
        repo = TreasuryRepository(db)

        # Create two log entries directly via repo
        repo.add_rebalance_log(
            agent_id=agent.id, trigger="manual",
            conversions=[{"from": "XMR", "to": "xUSD", "amount": "1.0"}],
            pre_allocation={"XMR": 80, "xUSD": 20},
            post_allocation={"XMR": 50, "xUSD": 50},
            total_value_xusd=Decimal("300.0"),
        )
        repo.add_rebalance_log(
            agent_id=agent.id, trigger="threshold_breach",
            conversions=[{"from": "xUSD", "to": "XMR", "amount": "50.0"}],
            pre_allocation={"XMR": 30, "xUSD": 70},
            post_allocation={"XMR": 50, "xUSD": 50},
            total_value_xusd=Decimal("400.0"),
        )
        db.flush()

        svc = TreasuryService()
        result = svc.get_history(db, agent.id)

        assert len(result) == 2

    def test_get_history_empty(self, db):
        """get_history returns empty list when no rebalance history exists."""
        from sthrip.services.treasury_service import TreasuryService

        agent = _make_agent(db)
        svc = TreasuryService()
        result = svc.get_history(db, agent.id)

        assert result == []

    def test_get_history_respects_limit(self, db):
        """get_history respects the limit parameter."""
        from sthrip.services.treasury_service import TreasuryService
        from sthrip.db.treasury_repo import TreasuryRepository

        agent = _make_agent(db)
        repo = TreasuryRepository(db)

        for i in range(5):
            repo.add_rebalance_log(
                agent_id=agent.id, trigger="manual",
                conversions=[], pre_allocation={}, post_allocation={},
                total_value_xusd=Decimal("100.0"),
            )
        db.flush()

        svc = TreasuryService()
        result = svc.get_history(db, agent.id, limit=2)

        assert len(result) == 2


# ===========================================================================
# PART 2: API endpoint tests
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


def _register_agent(client, name: str = "treasury-api-agent") -> tuple:
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


class TestTreasuryApiSetPolicy:
    """Integration tests for PUT /v2/me/treasury/policy."""

    def test_api_set_policy_200(self, client):
        """PUT /v2/me/treasury/policy returns 200 with valid allocation."""
        api_key, agent_id = _register_agent(client, "policy-agent")

        resp = client.put(
            "/v2/me/treasury/policy",
            json={
                "allocation": {"XMR": 50, "xUSD": 30, "xEUR": 20},
                "rebalance_threshold_pct": 5,
                "cooldown_minutes": 60,
                "emergency_reserve_pct": 10,
            },
            headers=_auth_headers(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["target_allocation"] == {"XMR": 50, "xUSD": 30, "xEUR": 20}
        assert body["is_active"] is True

    def test_api_set_policy_invalid_sum_422(self, client):
        """PUT /v2/me/treasury/policy returns 422 when allocation doesn't sum to 100."""
        api_key, _ = _register_agent(client, "bad-alloc-agent")

        resp = client.put(
            "/v2/me/treasury/policy",
            json={
                "allocation": {"XMR": 50, "xUSD": 30},
            },
            headers=_auth_headers(api_key),
        )
        assert resp.status_code == 422, resp.text

    def test_api_set_policy_requires_auth(self, client):
        """PUT /v2/me/treasury/policy returns 401 without auth."""
        resp = client.put(
            "/v2/me/treasury/policy",
            json={"allocation": {"XMR": 100}},
        )
        assert resp.status_code in (401, 403), resp.text


class TestTreasuryApiGetPolicy:
    """Integration tests for GET /v2/me/treasury/policy."""

    def test_api_get_policy_200(self, client):
        """GET /v2/me/treasury/policy returns 200 when policy exists."""
        api_key, _ = _register_agent(client, "get-policy-agent")

        # First set a policy
        client.put(
            "/v2/me/treasury/policy",
            json={"allocation": {"XMR": 100}},
            headers=_auth_headers(api_key),
        )

        resp = client.get(
            "/v2/me/treasury/policy",
            headers=_auth_headers(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["target_allocation"] == {"XMR": 100}

    def test_api_get_policy_404_when_none(self, client):
        """GET /v2/me/treasury/policy returns 404 when no policy set."""
        api_key, _ = _register_agent(client, "no-policy-agent")

        resp = client.get(
            "/v2/me/treasury/policy",
            headers=_auth_headers(api_key),
        )
        assert resp.status_code == 404, resp.text


class TestTreasuryApiDeactivatePolicy:
    """Integration tests for DELETE /v2/me/treasury/policy."""

    def test_api_deactivate_policy(self, client):
        """DELETE /v2/me/treasury/policy deactivates the policy."""
        api_key, _ = _register_agent(client, "deactivate-agent")

        # Set policy
        client.put(
            "/v2/me/treasury/policy",
            json={"allocation": {"XMR": 100}},
            headers=_auth_headers(api_key),
        )

        # Deactivate
        resp = client.delete(
            "/v2/me/treasury/policy",
            headers=_auth_headers(api_key),
        )
        assert resp.status_code == 200, resp.text

        # Verify deactivated
        resp = client.get(
            "/v2/me/treasury/policy",
            headers=_auth_headers(api_key),
        )
        # Policy still exists but is_active=False
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_active"] is False


class TestTreasuryApiGetStatus:
    """Integration tests for GET /v2/me/treasury/status."""

    def test_api_get_status(self, client, session_factory):
        """GET /v2/me/treasury/status returns current allocation."""
        import uuid as _uuid

        api_key, agent_id = _register_agent(client, "status-agent")
        agent_uuid = _uuid.UUID(agent_id)

        # Seed balances
        with session_factory() as s:
            s.add(AgentBalance(agent_id=agent_uuid, token="XMR", available=Decimal("1.0")))
            s.add(AgentBalance(agent_id=agent_uuid, token="xUSD", available=Decimal("150.0")))
            s.commit()

        resp = client.get(
            "/v2/me/treasury/status",
            headers=_auth_headers(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "balances" in body
        assert "allocation_pct" in body
        assert "total_value_xusd" in body


class TestTreasuryApiRebalance:
    """Integration tests for POST /v2/me/treasury/rebalance."""

    def test_api_rebalance(self, client, session_factory):
        """POST /v2/me/treasury/rebalance triggers rebalance."""
        import uuid as _uuid

        api_key, agent_id = _register_agent(client, "rebal-agent")
        agent_uuid = _uuid.UUID(agent_id)

        # Set policy
        client.put(
            "/v2/me/treasury/policy",
            json={
                "allocation": {"XMR": 50, "xUSD": 50},
                "rebalance_threshold_pct": 5,
            },
            headers=_auth_headers(api_key),
        )

        # Seed unbalanced
        with session_factory() as s:
            s.add(AgentBalance(agent_id=agent_uuid, token="XMR", available=Decimal("2.0")))
            s.commit()

        with patch(
            "sthrip.services.treasury_service.ConversionService"
        ) as MockConv:
            mock_conv = MockConv.return_value
            mock_conv.convert.return_value = {
                "from_currency": "XMR",
                "from_amount": "1.0",
                "to_currency": "xUSD",
                "gross_to_amount": "150.0",
                "fee_amount": "0.75",
                "net_to_amount": "149.25",
                "rate": "150.0",
            }

            resp = client.post(
                "/v2/me/treasury/rebalance",
                headers=_auth_headers(api_key),
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "rebalanced" in body

    def test_api_rebalance_requires_auth(self, client):
        """POST /v2/me/treasury/rebalance returns 401 without auth."""
        resp = client.post("/v2/me/treasury/rebalance")
        assert resp.status_code in (401, 403), resp.text


class TestTreasuryApiHistory:
    """Integration tests for GET /v2/me/treasury/history."""

    def test_api_history(self, client, session_factory):
        """GET /v2/me/treasury/history returns rebalance log entries."""
        import uuid as _uuid

        api_key, agent_id = _register_agent(client, "history-agent")
        agent_uuid = _uuid.UUID(agent_id)

        # Insert a rebalance log entry directly
        with session_factory() as s:
            log = TreasuryRebalanceLog(
                agent_id=agent_uuid,
                trigger="manual",
                conversions=[],
                pre_allocation={"XMR": 80, "xUSD": 20},
                post_allocation={"XMR": 50, "xUSD": 50},
                total_value_xusd=Decimal("300.0"),
            )
            s.add(log)
            s.commit()

        resp = client.get(
            "/v2/me/treasury/history",
            headers=_auth_headers(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "items" in body
        assert len(body["items"]) >= 1

    def test_api_history_requires_auth(self, client):
        """GET /v2/me/treasury/history returns 401 without auth."""
        resp = client.get("/v2/me/treasury/history")
        assert resp.status_code in (401, 403), resp.text
