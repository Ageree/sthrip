"""Tests for webhook fan-out to multiple registered endpoints.

Validates that WebhookService.process_event() delivers events to all
matching WebhookEndpoint records, respects event_filters, tracks
per-endpoint failure_count, and maintains backward compatibility
with the legacy single webhook_url on the Agent model.
"""

import asyncio
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.crypto import encrypt_value
from sthrip.db.models import (
    Agent,
    AgentReputation,
    AgentTier,
    Base,
    PrivacyLevel,
    RateLimitTier,
    WebhookEndpoint,
    WebhookEvent,
    WebhookStatus,
)
from sthrip.db.repository import WebhookRepository
from sthrip.db.webhook_endpoint_repo import WebhookEndpointRepository
from sthrip.services.webhook_service import WebhookResult, WebhookService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_engine():
    """In-memory SQLite engine with webhook-related tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Agent.__table__,
            AgentReputation.__table__,
            WebhookEvent.__table__,
            WebhookEndpoint.__table__,
        ],
    )
    return engine


@pytest.fixture
def db_session_factory(db_engine):
    """Session factory bound to the in-memory test engine."""
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def db_session(db_session_factory):
    """Single session for test setup and verification."""
    session = db_session_factory()
    yield session
    session.close()


@pytest.fixture
def get_test_db(db_session_factory):
    """Context manager that yields a session (mirrors get_db)."""

    @contextmanager
    def _get_db():
        session = db_session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _get_db


@pytest.fixture
def agent(db_session) -> Agent:
    """Create a basic test agent with no legacy webhook_url."""
    ag = Agent(
        agent_name="fanout-test-agent",
        api_key_hash="fanout-test-hash",
        tier=AgentTier.FREE,
        rate_limit_tier=RateLimitTier.STANDARD,
        privacy_level=PrivacyLevel.MEDIUM,
        is_active=True,
        webhook_url=None,
        webhook_secret=None,
    )
    db_session.add(ag)
    db_session.flush()
    return ag


@pytest.fixture
def agent_with_legacy_url(db_session) -> Agent:
    """Create a test agent with legacy webhook_url set."""
    secret_enc = encrypt_value("legacy-secret")
    ag = Agent(
        agent_name="legacy-url-agent",
        api_key_hash="legacy-url-hash",
        tier=AgentTier.FREE,
        rate_limit_tier=RateLimitTier.STANDARD,
        privacy_level=PrivacyLevel.MEDIUM,
        is_active=True,
        webhook_url="https://legacy.example.com/webhook",
        webhook_secret=secret_enc,
    )
    db_session.add(ag)
    db_session.flush()
    return ag


def _create_endpoint(
    db_session,
    agent_id: uuid.UUID,
    url: str,
    secret: str = "ep-secret",
    event_filters=None,
    is_active: bool = True,
    failure_count: int = 0,
) -> WebhookEndpoint:
    """Helper to insert a WebhookEndpoint row."""
    ep = WebhookEndpoint(
        agent_id=agent_id,
        url=url,
        secret_encrypted=encrypt_value(secret),
        event_filters=event_filters,
        is_active=is_active,
        failure_count=failure_count,
    )
    db_session.add(ep)
    db_session.flush()
    return ep


def _create_event(
    db_session,
    agent_id: uuid.UUID,
    event_type: str = "payment.received",
) -> WebhookEvent:
    """Helper to insert a WebhookEvent row."""
    event = WebhookEvent(
        agent_id=agent_id,
        event_type=event_type,
        payload={
            "event_id": f"evt_{uuid.uuid4().hex}",
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {"amount": "1.5"},
        },
        status=WebhookStatus.PENDING,
        attempt_count=0,
        max_attempts=5,
        next_attempt_at=datetime.now(timezone.utc),
    )
    db_session.add(event)
    db_session.flush()
    return event


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEventFilterMatching:
    """Unit tests for _matches_event_filter (no DB needed)."""

    def test_none_filters_matches_all(self):
        assert WebhookService._matches_event_filter("payment.received", None) is True

    def test_empty_filters_matches_all(self):
        assert WebhookService._matches_event_filter("escrow.created", []) is True

    def test_wildcard_payment(self):
        assert WebhookService._matches_event_filter("payment.received", ["payment.*"]) is True
        assert WebhookService._matches_event_filter("payment.sent", ["payment.*"]) is True

    def test_wildcard_no_match(self):
        assert WebhookService._matches_event_filter("escrow.created", ["payment.*"]) is False

    def test_exact_match(self):
        assert WebhookService._matches_event_filter("escrow.created", ["escrow.created"]) is True

    def test_multiple_filters_any_match(self):
        filters = ["payment.*", "escrow.*"]
        assert WebhookService._matches_event_filter("payment.received", filters) is True
        assert WebhookService._matches_event_filter("escrow.completed", filters) is True
        assert WebhookService._matches_event_filter("agent.verified", filters) is False

    def test_star_star_matches_all(self):
        assert WebhookService._matches_event_filter("anything.here", ["*"]) is True


class TestFanoutToMultipleEndpoints:
    """Verify that process_event delivers to all matching registered endpoints."""

    @pytest.mark.asyncio
    async def test_fanout_to_multiple_endpoints(self, db_session, agent, get_test_db):
        """Register 3 endpoints, all should receive the event."""
        ep1 = _create_endpoint(db_session, agent.id, "https://ep1.example.com/hook")
        ep2 = _create_endpoint(db_session, agent.id, "https://ep2.example.com/hook")
        ep3 = _create_endpoint(db_session, agent.id, "https://ep3.example.com/hook")
        event = _create_event(db_session, agent.id, "payment.received")
        db_session.commit()

        delivered_urls = []

        async def mock_send(url, payload, secret=None, timeout=30):
            delivered_urls.append(url)
            return WebhookResult(success=True, response_code=200, response_body="OK")

        svc = WebhookService()

        with patch("sthrip.services.webhook_service.get_db", side_effect=get_test_db):
            with patch.object(svc, "_send_webhook", side_effect=mock_send):
                result = await svc.process_event(str(event.id))

        assert result.success is True
        assert len(delivered_urls) == 3
        assert "https://ep1.example.com/hook" in delivered_urls
        assert "https://ep2.example.com/hook" in delivered_urls
        assert "https://ep3.example.com/hook" in delivered_urls

    @pytest.mark.asyncio
    async def test_event_filter_matching(self, db_session, agent, get_test_db):
        """Endpoint with ["payment.*"] gets payment.received but not escrow.created."""
        ep_payment = _create_endpoint(
            db_session, agent.id,
            "https://payments.example.com/hook",
            event_filters=["payment.*"],
        )
        ep_escrow = _create_endpoint(
            db_session, agent.id,
            "https://escrow.example.com/hook",
            event_filters=["escrow.*"],
        )
        ep_all = _create_endpoint(
            db_session, agent.id,
            "https://all.example.com/hook",
            event_filters=None,
        )

        # Create payment event
        event = _create_event(db_session, agent.id, "payment.received")
        db_session.commit()

        delivered_urls = []

        async def mock_send(url, payload, secret=None, timeout=30):
            delivered_urls.append(url)
            return WebhookResult(success=True, response_code=200, response_body="OK")

        svc = WebhookService()

        with patch("sthrip.services.webhook_service.get_db", side_effect=get_test_db):
            with patch.object(svc, "_send_webhook", side_effect=mock_send):
                result = await svc.process_event(str(event.id))

        assert result.success is True
        # payment endpoint and catch-all should receive, escrow should not
        assert "https://payments.example.com/hook" in delivered_urls
        assert "https://all.example.com/hook" in delivered_urls
        assert "https://escrow.example.com/hook" not in delivered_urls
        assert len(delivered_urls) == 2


class TestInactiveEndpointSkipped:
    """Endpoints with is_active=False must not receive events."""

    @pytest.mark.asyncio
    async def test_inactive_endpoint_skipped(self, db_session, agent, get_test_db):
        _create_endpoint(
            db_session, agent.id,
            "https://active.example.com/hook",
            is_active=True,
        )
        _create_endpoint(
            db_session, agent.id,
            "https://inactive.example.com/hook",
            is_active=False,
        )
        event = _create_event(db_session, agent.id, "payment.received")
        db_session.commit()

        delivered_urls = []

        async def mock_send(url, payload, secret=None, timeout=30):
            delivered_urls.append(url)
            return WebhookResult(success=True, response_code=200, response_body="OK")

        svc = WebhookService()

        with patch("sthrip.services.webhook_service.get_db", side_effect=get_test_db):
            with patch.object(svc, "_send_webhook", side_effect=mock_send):
                result = await svc.process_event(str(event.id))

        assert result.success is True
        assert len(delivered_urls) == 1
        assert "https://active.example.com/hook" in delivered_urls
        assert "https://inactive.example.com/hook" not in delivered_urls


class TestFailureCountIncremented:
    """On delivery failure, endpoint failure_count should increase."""

    @pytest.mark.asyncio
    async def test_failure_count_incremented(self, db_session, agent, get_test_db):
        ep = _create_endpoint(
            db_session, agent.id,
            "https://flaky.example.com/hook",
            failure_count=0,
        )
        event = _create_event(db_session, agent.id, "payment.received")
        db_session.commit()
        ep_id = ep.id

        async def mock_send(url, payload, secret=None, timeout=30):
            return WebhookResult(success=False, response_code=500, error="HTTP 500")

        svc = WebhookService()

        with patch("sthrip.services.webhook_service.get_db", side_effect=get_test_db):
            with patch.object(svc, "_send_webhook", side_effect=mock_send):
                result = await svc.process_event(str(event.id))

        # Event delivery failed (no endpoints succeeded)
        assert result.success is False

        # Verify failure_count was incremented in DB
        with get_test_db() as db:
            repo = WebhookEndpointRepository(db)
            updated_ep = repo.get_by_id(ep_id, agent.id)
            assert updated_ep.failure_count == 1
            assert updated_ep.is_active is True  # not yet disabled

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self, db_session, agent, get_test_db):
        """On successful delivery, failure_count should be reset to 0."""
        ep = _create_endpoint(
            db_session, agent.id,
            "https://recovered.example.com/hook",
            failure_count=5,
        )
        event = _create_event(db_session, agent.id, "payment.received")
        db_session.commit()
        ep_id = ep.id

        async def mock_send(url, payload, secret=None, timeout=30):
            return WebhookResult(success=True, response_code=200, response_body="OK")

        svc = WebhookService()

        with patch("sthrip.services.webhook_service.get_db", side_effect=get_test_db):
            with patch.object(svc, "_send_webhook", side_effect=mock_send):
                result = await svc.process_event(str(event.id))

        assert result.success is True

        with get_test_db() as db:
            repo = WebhookEndpointRepository(db)
            updated_ep = repo.get_by_id(ep_id, agent.id)
            assert updated_ep.failure_count == 0


class TestEndpointDisabledAfterMaxFailures:
    """Endpoint should be auto-disabled after 10 consecutive failures."""

    @pytest.mark.asyncio
    async def test_endpoint_disabled_after_10_failures(self, db_session, agent, get_test_db):
        ep = _create_endpoint(
            db_session, agent.id,
            "https://dying.example.com/hook",
            failure_count=9,  # one more failure = 10 = disabled
        )
        event = _create_event(db_session, agent.id, "payment.received")
        db_session.commit()
        ep_id = ep.id

        async def mock_send(url, payload, secret=None, timeout=30):
            return WebhookResult(success=False, response_code=503, error="HTTP 503")

        svc = WebhookService()

        with patch("sthrip.services.webhook_service.get_db", side_effect=get_test_db):
            with patch.object(svc, "_send_webhook", side_effect=mock_send):
                result = await svc.process_event(str(event.id))

        assert result.success is False

        with get_test_db() as db:
            repo = WebhookEndpointRepository(db)
            updated_ep = repo.get_by_id(ep_id, agent.id)
            assert updated_ep.failure_count == 10
            assert updated_ep.is_active is False
            assert updated_ep.disabled_at is not None

    @pytest.mark.asyncio
    async def test_endpoint_at_max_failures_is_skipped(self, db_session, agent, get_test_db):
        """An endpoint already at 10 failures should not be called."""
        _create_endpoint(
            db_session, agent.id,
            "https://dead.example.com/hook",
            failure_count=10,
            is_active=True,  # active but at max failures
        )
        _create_endpoint(
            db_session, agent.id,
            "https://alive.example.com/hook",
            failure_count=0,
        )
        event = _create_event(db_session, agent.id, "payment.received")
        db_session.commit()

        delivered_urls = []

        async def mock_send(url, payload, secret=None, timeout=30):
            delivered_urls.append(url)
            return WebhookResult(success=True, response_code=200, response_body="OK")

        svc = WebhookService()

        with patch("sthrip.services.webhook_service.get_db", side_effect=get_test_db):
            with patch.object(svc, "_send_webhook", side_effect=mock_send):
                result = await svc.process_event(str(event.id))

        assert result.success is True
        assert len(delivered_urls) == 1
        assert "https://alive.example.com/hook" in delivered_urls


class TestBackwardCompatSingleUrl:
    """Agent with only legacy webhook_url (no registered endpoints) still works."""

    @pytest.mark.asyncio
    async def test_backward_compat_single_url(
        self, db_session, agent_with_legacy_url, get_test_db
    ):
        """Agent with only webhook_url and no registered endpoints still receives events."""
        event = _create_event(db_session, agent_with_legacy_url.id, "payment.received")
        db_session.commit()

        delivered_urls = []

        async def mock_send(url, payload, secret=None, timeout=30):
            delivered_urls.append(url)
            return WebhookResult(success=True, response_code=200, response_body="OK")

        svc = WebhookService()

        with patch("sthrip.services.webhook_service.get_db", side_effect=get_test_db):
            with patch.object(svc, "_send_webhook", side_effect=mock_send):
                result = await svc.process_event(str(event.id))

        assert result.success is True
        assert len(delivered_urls) == 1
        assert "https://legacy.example.com/webhook" in delivered_urls

    @pytest.mark.asyncio
    async def test_legacy_url_plus_registered_endpoints(
        self, db_session, agent_with_legacy_url, get_test_db
    ):
        """Agent with both legacy webhook_url AND registered endpoints gets both."""
        _create_endpoint(
            db_session, agent_with_legacy_url.id,
            "https://registered.example.com/hook",
        )
        event = _create_event(db_session, agent_with_legacy_url.id, "payment.received")
        db_session.commit()

        delivered_urls = []

        async def mock_send(url, payload, secret=None, timeout=30):
            delivered_urls.append(url)
            return WebhookResult(success=True, response_code=200, response_body="OK")

        svc = WebhookService()

        with patch("sthrip.services.webhook_service.get_db", side_effect=get_test_db):
            with patch.object(svc, "_send_webhook", side_effect=mock_send):
                result = await svc.process_event(str(event.id))

        assert result.success is True
        assert len(delivered_urls) == 2
        assert "https://legacy.example.com/webhook" in delivered_urls
        assert "https://registered.example.com/hook" in delivered_urls

    @pytest.mark.asyncio
    async def test_legacy_url_deduped_with_registered(
        self, db_session, agent_with_legacy_url, get_test_db
    ):
        """If a registered endpoint has the same URL as legacy, deliver only once."""
        _create_endpoint(
            db_session, agent_with_legacy_url.id,
            "https://legacy.example.com/webhook",  # same URL as legacy
        )
        event = _create_event(db_session, agent_with_legacy_url.id, "payment.received")
        db_session.commit()

        delivered_urls = []

        async def mock_send(url, payload, secret=None, timeout=30):
            delivered_urls.append(url)
            return WebhookResult(success=True, response_code=200, response_body="OK")

        svc = WebhookService()

        with patch("sthrip.services.webhook_service.get_db", side_effect=get_test_db):
            with patch.object(svc, "_send_webhook", side_effect=mock_send):
                result = await svc.process_event(str(event.id))

        assert result.success is True
        # Should not duplicate: registered endpoint covers the URL
        assert len(delivered_urls) == 1
        assert delivered_urls[0] == "https://legacy.example.com/webhook"


class TestNoTargetsMarksDelivered:
    """Agent with no webhook_url and no registered endpoints marks event delivered."""

    @pytest.mark.asyncio
    async def test_no_targets_marks_delivered(self, db_session, agent, get_test_db):
        event = _create_event(db_session, agent.id, "payment.received")
        db_session.commit()

        svc = WebhookService()

        with patch("sthrip.services.webhook_service.get_db", side_effect=get_test_db):
            result = await svc.process_event(str(event.id))

        assert result.success is True

        # Event should be marked as delivered
        with get_test_db() as db:
            repo = WebhookRepository(db)
            updated_event = repo.get_by_id(event.id)
            assert updated_event.status == WebhookStatus.DELIVERED


class TestPartialFanoutFailure:
    """When some endpoints succeed and some fail, event is still marked delivered."""

    @pytest.mark.asyncio
    async def test_partial_failure_still_succeeds(self, db_session, agent, get_test_db):
        """If at least one endpoint succeeds, the event is marked delivered."""
        ep_ok = _create_endpoint(
            db_session, agent.id,
            "https://ok.example.com/hook",
        )
        ep_fail = _create_endpoint(
            db_session, agent.id,
            "https://fail.example.com/hook",
        )
        event = _create_event(db_session, agent.id, "payment.received")
        db_session.commit()

        async def mock_send(url, payload, secret=None, timeout=30):
            if "fail" in url:
                return WebhookResult(success=False, response_code=500, error="HTTP 500")
            return WebhookResult(success=True, response_code=200, response_body="OK")

        svc = WebhookService()

        with patch("sthrip.services.webhook_service.get_db", side_effect=get_test_db):
            with patch.object(svc, "_send_webhook", side_effect=mock_send):
                result = await svc.process_event(str(event.id))

        # Overall success because at least one endpoint succeeded
        assert result.success is True

        # Event should be marked as delivered
        with get_test_db() as db:
            repo = WebhookRepository(db)
            updated_event = repo.get_by_id(event.id)
            assert updated_event.status == WebhookStatus.DELIVERED

        # Failed endpoint should have incremented failure_count
        with get_test_db() as db:
            ep_repo = WebhookEndpointRepository(db)
            updated_fail = ep_repo.get_by_id(ep_fail.id, agent.id)
            assert updated_fail.failure_count == 1

            updated_ok = ep_repo.get_by_id(ep_ok.id, agent.id)
            assert updated_ok.failure_count == 0
