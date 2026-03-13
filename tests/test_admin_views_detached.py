"""Test that admin views don't pass ORM objects outside session scope.

HIGH-2: agent_detail, agents_list, and transactions_list pass ORM objects
to templates after the get_db() session is closed. This test ensures all
views serialize ORM objects to plain dicts inside the session scope.
"""

import os
import re
import asyncio
import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock
from contextlib import ExitStack, contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, AgentTier,
    RateLimitTier, PrivacyLevel, HubRoute, HubRouteStatus, FeeCollection,
)

_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
]

ADMIN_KEY = "test-admin-key-detached"


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_TEST_TABLES)
    return engine


@pytest.fixture
def db_session_factory(db_engine):
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def seed_data(db_session_factory):
    """Seed test data: 2 agents with balances, reputation, and a payment."""
    import secrets
    session = db_session_factory()

    agent_a = Agent(
        agent_name="detach-alpha",
        api_key_hash="hash_a",
        tier=AgentTier.VERIFIED,
        rate_limit_tier=RateLimitTier.STANDARD,
        privacy_level=PrivacyLevel.MEDIUM,
        is_active=True,
    )
    agent_b = Agent(
        agent_name="detach-beta",
        api_key_hash="hash_b",
        tier=AgentTier.FREE,
        rate_limit_tier=RateLimitTier.STANDARD,
        privacy_level=PrivacyLevel.LOW,
        is_active=False,
    )
    session.add_all([agent_a, agent_b])
    session.flush()

    bal_a = AgentBalance(agent_id=agent_a.id, available=Decimal("10.0"), pending=Decimal("1.0"))
    rep_a = AgentReputation(agent_id=agent_a.id, trust_score=85, successful_transactions=10, failed_transactions=0)
    session.add_all([bal_a, rep_a])

    route = HubRoute(
        payment_id=secrets.token_hex(32),
        from_agent_id=agent_a.id,
        to_agent_id=agent_b.id,
        amount=Decimal("2.5"),
        fee_amount=Decimal("0.025"),
        status=HubRouteStatus.CONFIRMED,
    )
    session.add(route)
    session.commit()
    agents = [agent_a, agent_b]
    session.close()
    return agents


def _make_get_test_db(db_session_factory):
    @contextmanager
    def get_test_db():
        session = db_session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    return get_test_db


class TestAgentDetailSerializesToDicts:
    """agent_detail must pass dicts, not ORM objects, to the template."""

    def test_agent_detail_passes_dicts_to_template(
        self, db_session_factory, seed_data,
    ):
        """Capture template context and verify all values are plain dicts."""
        import api.admin_ui.views as views_mod

        get_test_db = _make_get_test_db(db_session_factory)
        agent_id = str(seed_data[0].id)

        mock_request = MagicMock()
        mock_request.cookies = {"admin_session": "valid"}

        captured = {}

        def capture_response(request, name, context=None, **kwargs):
            if context is not None:
                captured.update(context)
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("api.admin_ui.views.get_db", get_test_db), \
             patch("api.admin_ui.views._require_auth"), \
             patch.object(views_mod.templates, "TemplateResponse", side_effect=capture_response):
            asyncio.get_event_loop().run_until_complete(
                views_mod.agent_detail(mock_request, agent_id)
            )

        # agent must be a dict
        agent = captured.get("agent")
        assert isinstance(agent, dict), (
            f"Expected agent to be dict, got {type(agent).__name__}"
        )
        assert agent["agent_name"] == "detach-alpha"

        # balance must be a dict (or None)
        balance = captured.get("balance")
        assert balance is None or isinstance(balance, dict), (
            f"Expected balance to be dict or None, got {type(balance).__name__}"
        )

        # reputation must be a dict (or None)
        reputation = captured.get("reputation")
        assert reputation is None or isinstance(reputation, dict), (
            f"Expected reputation to be dict or None, got {type(reputation).__name__}"
        )

        # transactions must be a list of dicts
        transactions = captured.get("transactions", [])
        assert isinstance(transactions, list)
        for tx in transactions:
            assert isinstance(tx, dict), (
                f"Expected transaction to be dict, got {type(tx).__name__}"
            )


class TestAgentsListSerializesToDicts:
    """agents_list must pass dicts, not ORM objects, to the template."""

    def test_agents_list_passes_dicts_to_template(
        self, db_session_factory, seed_data,
    ):
        import api.admin_ui.views as views_mod

        get_test_db = _make_get_test_db(db_session_factory)

        mock_request = MagicMock()
        mock_request.cookies = {"admin_session": "valid"}

        captured = {}

        def capture_response(request, name, context=None, **kwargs):
            if context is not None:
                captured.update(context)
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("api.admin_ui.views.get_db", get_test_db), \
             patch("api.admin_ui.views._require_auth"), \
             patch.object(views_mod.templates, "TemplateResponse", side_effect=capture_response):
            asyncio.get_event_loop().run_until_complete(
                views_mod.agents_list(mock_request, search="", tier="", page=1)
            )

        agents = captured.get("agents", [])
        assert len(agents) > 0, "Expected at least one agent"
        for agent in agents:
            assert isinstance(agent, dict), (
                f"Expected agent to be dict, got {type(agent).__name__}"
            )
            assert "agent_name" in agent
            assert "id" in agent


class TestTransactionsListSerializesToDicts:
    """transactions_list must pass dicts, not ORM objects, to the template."""

    def test_transactions_list_passes_dicts_to_template(
        self, db_session_factory, seed_data,
    ):
        import api.admin_ui.views as views_mod

        get_test_db = _make_get_test_db(db_session_factory)

        mock_request = MagicMock()
        mock_request.cookies = {"admin_session": "valid"}

        captured = {}

        def capture_response(request, name, context=None, **kwargs):
            if context is not None:
                captured.update(context)
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("api.admin_ui.views.get_db", get_test_db), \
             patch("api.admin_ui.views._require_auth"), \
             patch.object(views_mod.templates, "TemplateResponse", side_effect=capture_response):
            asyncio.get_event_loop().run_until_complete(
                views_mod.transactions_list(mock_request, status="", page=1)
            )

        transactions = captured.get("transactions", [])
        assert len(transactions) > 0, "Expected at least one transaction"
        for item in transactions:
            assert isinstance(item, dict), (
                f"Expected transaction item to be dict, got {type(item).__name__}"
            )
            # tx sub-dict must also be a dict, not ORM
            tx = item.get("tx")
            assert isinstance(tx, dict), (
                f"Expected tx to be dict, got {type(tx).__name__}"
            )
            # from_agent / to_agent must be dict or None
            for key in ("from_agent", "to_agent"):
                val = item.get(key)
                assert val is None or isinstance(val, dict), (
                    f"Expected {key} to be dict or None, got {type(val).__name__}"
                )
