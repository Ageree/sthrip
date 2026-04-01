"""
TDD integration tests for Payment Channel API endpoints.

RED phase: written before implementation.
"""

import contextlib
import os
import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance,
    PaymentChannel, ChannelUpdate, FeeCollection,
    ChannelStatus,
)

# -----------------------------------------------------------------------
# Test tables & module lists  (mirrors conftest.py pattern)
# -----------------------------------------------------------------------

_CHANNEL_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    PaymentChannel.__table__,
    ChannelUpdate.__table__,
    FeeCollection.__table__,
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


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------

@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_CHANNEL_TEST_TABLES)
    return engine


@pytest.fixture
def db_session_factory(db_engine):
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def client(db_engine, db_session_factory):
    """FastAPI test client with channel tables and route patched."""

    @contextlib.contextmanager
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

    mock_limiter = MagicMock()
    mock_limiter.check_rate_limit.return_value = None
    mock_limiter.check_ip_rate_limit.return_value = None
    mock_limiter.get_limit_status.return_value = {"requests_remaining": 100}

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy", "timestamp": "2026-01-01T00:00:00", "checks": {}
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
            patch("sthrip.services.monitoring.setup_default_monitoring", return_value=mock_monitor)
        )
        stack.enter_context(
            patch("sthrip.services.webhook_service.get_webhook_service", return_value=mock_webhook)
        )
        stack.enter_context(patch("sthrip.services.webhook_service.queue_webhook"))
        stack.enter_context(patch("sthrip.services.channel_service.audit_log"))
        stack.enter_context(patch("sthrip.services.channel_service.queue_webhook"))

        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)


def _make_agent_in_db(db_session_factory, name="test-agent", balance="10.0"):
    """Helper: create agent + api_key + balance directly in DB.

    Uses the same HMAC hashing as AgentRepository._hash_api_key so the
    auth dependency can validate it.
    """
    import hmac
    import hashlib

    _HMAC_SECRET = "dev-hmac-secret-change-in-prod"

    session = db_session_factory()
    try:
        agent = Agent(agent_name=name, is_active=True)
        session.add(agent)
        session.flush()

        rep = AgentReputation(agent_id=agent.id, trust_score=50)
        session.add(rep)

        bal = AgentBalance(agent_id=agent.id, token="XMR", available=Decimal(balance))
        session.add(bal)
        session.flush()

        raw_key = f"key-{name}"
        key_hash = hmac.new(_HMAC_SECRET.encode(), raw_key.encode(), hashlib.sha256).hexdigest()
        agent.api_key_hash = key_hash
        session.commit()
        return str(agent.id), raw_key
    finally:
        session.close()


# -----------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------

class TestChannelAPI:

    def test_open_channel_201(self, client, db_session_factory):
        agent_a_id, key_a = _make_agent_in_db(db_session_factory, "api-open-a", "10.0")
        agent_b_id, _ = _make_agent_in_db(db_session_factory, "api-open-b", "0.0")

        resp = client.post(
            "/v2/channels",
            json={
                "agent_b_id": agent_b_id,
                "deposit_a": "5.0",
                "deposit_b": "0",
                "settlement_period": 3600,
            },
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert "channel_id" in data
        assert data["status"] == "open"
        assert data["deposit_a"] == "5.0"

    def test_open_channel_insufficient_balance_400(self, client, db_session_factory):
        agent_a_id, key_a = _make_agent_in_db(db_session_factory, "api-insuf-a", "1.0")
        agent_b_id, _ = _make_agent_in_db(db_session_factory, "api-insuf-b", "0.0")

        resp = client.post(
            "/v2/channels",
            json={
                "agent_b_id": agent_b_id,
                "deposit_a": "999.0",
                "deposit_b": "0",
            },
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert resp.status_code == 400

    def test_open_channel_self_400(self, client, db_session_factory):
        agent_id, key = _make_agent_in_db(db_session_factory, "api-self", "10.0")

        resp = client.post(
            "/v2/channels",
            json={"agent_b_id": agent_id, "deposit_a": "1.0"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 400

    def test_open_channel_unauthenticated_401(self, client, db_session_factory):
        agent_b_id, _ = _make_agent_in_db(db_session_factory, "api-unauth-b", "0.0")

        resp = client.post(
            "/v2/channels",
            json={"agent_b_id": agent_b_id, "deposit_a": "1.0"},
        )
        assert resp.status_code in (401, 403)

    def test_list_channels_empty(self, client, db_session_factory):
        _, key = _make_agent_in_db(db_session_factory, "api-list-empty", "5.0")

        resp = client.get("/v2/channels", headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["channels"] == []

    def test_list_channels_returns_opened_channels(self, client, db_session_factory):
        agent_a_id, key_a = _make_agent_in_db(db_session_factory, "api-list-a", "10.0")
        agent_b_id, _ = _make_agent_in_db(db_session_factory, "api-list-b", "0.0")

        # Open a channel
        client.post(
            "/v2/channels",
            json={"agent_b_id": agent_b_id, "deposit_a": "2.0"},
            headers={"Authorization": f"Bearer {key_a}"},
        )

        resp = client.get("/v2/channels", headers={"Authorization": f"Bearer {key_a}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1

    def test_get_channel_200(self, client, db_session_factory):
        agent_a_id, key_a = _make_agent_in_db(db_session_factory, "api-get-a", "10.0")
        agent_b_id, _ = _make_agent_in_db(db_session_factory, "api-get-b", "0.0")

        open_resp = client.post(
            "/v2/channels",
            json={"agent_b_id": agent_b_id, "deposit_a": "3.0"},
            headers={"Authorization": f"Bearer {key_a}"},
        )
        channel_id = open_resp.json()["channel_id"]

        resp = client.get(
            f"/v2/channels/{channel_id}",
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert resp.status_code == 200
        assert resp.json()["channel_id"] == channel_id

    def test_get_channel_not_participant_403(self, client, db_session_factory):
        agent_a_id, key_a = _make_agent_in_db(db_session_factory, "api-403-a", "10.0")
        agent_b_id, _ = _make_agent_in_db(db_session_factory, "api-403-b", "0.0")
        _, key_c = _make_agent_in_db(db_session_factory, "api-403-c", "0.0")

        open_resp = client.post(
            "/v2/channels",
            json={"agent_b_id": agent_b_id, "deposit_a": "1.0"},
            headers={"Authorization": f"Bearer {key_a}"},
        )
        channel_id = open_resp.json()["channel_id"]

        resp = client.get(
            f"/v2/channels/{channel_id}",
            headers={"Authorization": f"Bearer {key_c}"},
        )
        assert resp.status_code == 403

    def test_settle_channel_200(self, client, db_session_factory):
        agent_a_id, key_a = _make_agent_in_db(db_session_factory, "api-settle-a", "10.0")
        agent_b_id, _ = _make_agent_in_db(db_session_factory, "api-settle-b", "0.0")

        open_resp = client.post(
            "/v2/channels",
            json={"agent_b_id": agent_b_id, "deposit_a": "5.0"},
            headers={"Authorization": f"Bearer {key_a}"},
        )
        channel_id = open_resp.json()["channel_id"]

        resp = client.post(
            f"/v2/channels/{channel_id}/settle",
            json={
                "nonce": 1,
                "balance_a": "3.0",
                "balance_b": "2.0",
                "sig_a": "fake-sig-a",
                "sig_b": "fake-sig-b",
            },
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "closing"

    def test_close_channel_after_settlement_200(self, client, db_session_factory):
        from sqlalchemy import text as sql_text

        agent_a_id, key_a = _make_agent_in_db(db_session_factory, "api-close-a", "10.0")
        agent_b_id, _ = _make_agent_in_db(db_session_factory, "api-close-b", "0.0")

        # Open with a very short settlement period (1 second)
        open_resp = client.post(
            "/v2/channels",
            json={"agent_b_id": agent_b_id, "deposit_a": "5.0", "settlement_period": 60},
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert open_resp.status_code == 201, open_resp.text
        channel_id = open_resp.json()["channel_id"]

        settle_resp = client.post(
            f"/v2/channels/{channel_id}/settle",
            json={"nonce": 1, "balance_a": "5.0", "balance_b": "0", "sig_a": "sa", "sig_b": "sb"},
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert settle_resp.status_code == 200, settle_resp.text

        # Backdate closes_at so the channel is past the settlement window
        # SQLite stores UUID without hyphens, use hex format
        from uuid import UUID as _UUID
        cid_hex = _UUID(channel_id).hex
        session = db_session_factory()
        try:
            session.execute(
                sql_text(
                    "UPDATE payment_channels SET closes_at=datetime('now', '-10 seconds') WHERE id=:cid"
                ),
                {"cid": cid_hex},
            )
            session.commit()
        finally:
            session.close()

        resp = client.post(
            f"/v2/channels/{channel_id}/close",
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "closed"

    def test_submit_update_200(self, client, db_session_factory):
        agent_a_id, key_a = _make_agent_in_db(db_session_factory, "api-upd-a", "10.0")
        agent_b_id, _ = _make_agent_in_db(db_session_factory, "api-upd-b", "0.0")

        open_resp = client.post(
            "/v2/channels",
            json={"agent_b_id": agent_b_id, "deposit_a": "5.0"},
            headers={"Authorization": f"Bearer {key_a}"},
        )
        channel_id = open_resp.json()["channel_id"]

        resp = client.post(
            f"/v2/channels/{channel_id}/update",
            json={
                "nonce": 1,
                "balance_a": "4.0",
                "balance_b": "1.0",
                "signature_a": "sig-a",
                "signature_b": "sig-b",
            },
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert resp.status_code == 200
        assert resp.json()["nonce"] == 1
