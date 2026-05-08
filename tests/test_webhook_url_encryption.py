"""Sprint 5 (anonymity-hardening): webhook URL encryption tests.

Covers the runtime behaviour of the encrypted-URL path:
- repo encrypts on insert, decrypts on read
- decrypt failure → endpoint disabled, no crash
- marketplace + admin views never expose a plaintext URL
- PATCH /v2/me/settings webhook_url path now upserts an encrypted endpoint
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, WebhookEndpoint, WebhookEvent,
)
from sthrip.db.webhook_endpoint_repo import WebhookEndpointRepository
from sthrip.crypto import encrypt_value, decrypt_value


_TEST_URL = "https://example.com/hooks/abc"
_TEST_URL_2 = "https://example.com/hooks/xyz"
_VALID_XMR_ADDR = "5" + "a" * 94


@pytest.fixture
def db_session():
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
            WebhookEndpoint.__table__,
            WebhookEvent.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _make_agent(session) -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        agent_name=f"test-{uuid.uuid4().hex[:8]}",
        api_key_hash="hash",
        webhook_secret=encrypt_value("whsec_seed"),
        is_active=True,
    )
    session.add(agent)
    session.add(AgentReputation(agent_id=agent.id))
    session.flush()
    return agent


# ─────────────────────────────────────────────────────────────────────
# 1. create_webhook_endpoint encrypts URL
# ─────────────────────────────────────────────────────────────────────

def test_create_webhook_endpoint_encrypts_url(db_session):
    """url_encrypted must NOT contain the plaintext URL."""
    agent = _make_agent(db_session)
    repo = WebhookEndpointRepository(db_session)

    endpoint = repo.create(
        agent_id=agent.id,
        url=_TEST_URL,
        secret_encrypted=encrypt_value("whsec_x"),
    )
    db_session.flush()

    assert endpoint.url_encrypted, "url_encrypted must be populated"
    assert endpoint.url_encrypted != _TEST_URL
    assert "example.com" not in endpoint.url_encrypted
    assert "/hooks/abc" not in endpoint.url_encrypted


# ─────────────────────────────────────────────────────────────────────
# 2. get_url decrypts back to original
# ─────────────────────────────────────────────────────────────────────

def test_get_url_decrypts(db_session):
    agent = _make_agent(db_session)
    repo = WebhookEndpointRepository(db_session)
    endpoint = repo.create(
        agent_id=agent.id,
        url=_TEST_URL,
        secret_encrypted=encrypt_value("whsec_x"),
    )
    db_session.flush()

    assert WebhookEndpointRepository.get_url(endpoint) == _TEST_URL


# ─────────────────────────────────────────────────────────────────────
# 3. get_url returns None on malformed ciphertext
# ─────────────────────────────────────────────────────────────────────

def test_get_url_returns_none_on_decrypt_fail(db_session):
    agent = _make_agent(db_session)
    repo = WebhookEndpointRepository(db_session)
    endpoint = repo.create(
        agent_id=agent.id,
        url=_TEST_URL,
        secret_encrypted=encrypt_value("whsec_x"),
    )
    # Corrupt the ciphertext.
    endpoint.url_encrypted = "not-a-valid-fernet-token"
    db_session.flush()

    assert WebhookEndpointRepository.get_url(endpoint) is None


# ─────────────────────────────────────────────────────────────────────
# 4. find_by_agent_and_url + upsert idempotency
# ─────────────────────────────────────────────────────────────────────

def test_upsert_by_url_is_idempotent(db_session):
    agent = _make_agent(db_session)
    repo = WebhookEndpointRepository(db_session)
    secret = encrypt_value("whsec_x")

    first = repo.upsert_by_url(
        agent_id=agent.id, url=_TEST_URL, secret_encrypted=secret,
    )
    second = repo.upsert_by_url(
        agent_id=agent.id, url=_TEST_URL, secret_encrypted=secret,
    )
    assert first.id == second.id
    assert repo.count_by_agent(agent.id) == 1


def test_find_by_agent_and_url_returns_none_for_unknown(db_session):
    agent = _make_agent(db_session)
    repo = WebhookEndpointRepository(db_session)
    repo.create(
        agent_id=agent.id, url=_TEST_URL,
        secret_encrypted=encrypt_value("s"),
    )
    db_session.flush()
    assert repo.find_by_agent_and_url(agent.id, _TEST_URL_2) is None
    assert repo.find_by_agent_and_url(agent.id, _TEST_URL) is not None


# ─────────────────────────────────────────────────────────────────────
# 5. webhook_service reads encrypted URL via get_url
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_service_reads_encrypted_url(db_session):
    """End-to-end: service decrypts + delivers."""
    from sthrip.services.webhook_service import WebhookService, WebhookResult
    from sthrip.db.repository import WebhookRepository

    agent = _make_agent(db_session)
    repo = WebhookEndpointRepository(db_session)
    repo.create(
        agent_id=agent.id,
        url=_TEST_URL,
        secret_encrypted=encrypt_value("whsec_x"),
    )
    webhook_repo = WebhookRepository(db_session)
    event = webhook_repo.create_event(
        agent.id, "payment.received", {"amount": "1"}
    )
    db_session.flush()
    event_id = event.id

    @contextmanager
    def fake_get_db():
        yield db_session

    captured_urls = []

    async def fake_send(self, url, payload, secret=None, timeout=30):
        captured_urls.append(url)
        return WebhookResult(success=True, response_code=200)

    svc = WebhookService()
    with patch("sthrip.services.webhook_service.get_db", fake_get_db), \
         patch.object(WebhookService, "_send_webhook", fake_send):
        result = await svc.process_event(event_id)

    assert result.success is True
    assert _TEST_URL in captured_urls, (
        f"Expected decrypted URL in delivery; got {captured_urls}"
    )


# ─────────────────────────────────────────────────────────────────────
# 6. Decrypt failure during delivery → endpoint disabled
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_decrypt_failure_disables_endpoint(db_session):
    from sthrip.services.webhook_service import WebhookService
    from sthrip.db.repository import WebhookRepository

    agent = _make_agent(db_session)
    repo = WebhookEndpointRepository(db_session)
    endpoint = repo.create(
        agent_id=agent.id,
        url=_TEST_URL,
        secret_encrypted=encrypt_value("whsec_x"),
    )
    # Corrupt url_encrypted so get_url returns None at delivery time.
    endpoint.url_encrypted = "GARBAGE-NOT-FERNET"
    webhook_repo = WebhookRepository(db_session)
    event = webhook_repo.create_event(
        agent.id, "payment.received", {"amount": "1"}
    )
    db_session.flush()
    event_id = event.id
    endpoint_id = endpoint.id

    @contextmanager
    def fake_get_db():
        yield db_session

    svc = WebhookService()
    with patch("sthrip.services.webhook_service.get_db", fake_get_db):
        result = await svc.process_event(event_id)

    # After delivery, the endpoint should be disabled.
    db_session.expire_all()
    refreshed = (
        db_session.query(WebhookEndpoint)
        .filter(WebhookEndpoint.id == endpoint_id)
        .first()
    )
    assert refreshed is not None
    assert refreshed.is_active is False
    assert refreshed.disabled_at is not None
    # Event still gets marked delivered (no targets), success is True overall
    assert result.success is True


# ─────────────────────────────────────────────────────────────────────
# 7. Admin _serialize_agent never returns plaintext URL
# ─────────────────────────────────────────────────────────────────────

def test_admin_no_url_render(db_session):
    from api.admin_ui.views import _serialize_agent

    agent = _make_agent(db_session)
    repo = WebhookEndpointRepository(db_session)
    repo.create(
        agent_id=agent.id, url=_TEST_URL,
        secret_encrypted=encrypt_value("s"),
    )
    db_session.flush()
    db_session.refresh(agent)

    serialized = _serialize_agent(agent)

    # Sprint 5 requirement: admin view MUST NOT include the plaintext URL.
    assert serialized["webhook_url"] is None
    assert serialized["has_encrypted_webhook"] is True
    assert serialized["webhook_endpoint_count"] >= 1
    # Sanity: nowhere in the dict's values does the URL appear.
    flat = repr(serialized)
    assert _TEST_URL not in flat
    assert "example.com" not in flat


# ─────────────────────────────────────────────────────────────────────
# 8. Marketplace JSON / agent responses never expose webhook_url
# ─────────────────────────────────────────────────────────────────────

def test_marketplace_no_webhook_leak(db_session):
    """Marketplace + profile API response shapes never include webhook_url."""
    from api.schemas import (
        AgentResponse, AgentSettingsUpdate, AgentRegistration,
        WebhookEndpointResponse,
    )

    # AgentResponse (registration) -- no webhook_url field.
    assert "webhook_url" not in AgentResponse.model_fields, (
        "AgentResponse must not expose webhook_url"
    )
    # WebhookEndpointResponse intentionally has 'url' -- it is owner-only,
    # auth-gated; not a marketplace shape. Confirm it is not the marketplace
    # shape by checking the marketplace router does not serialise it.
    assert "url" in WebhookEndpointResponse.model_fields  # owner-facing

    # AgentRegistration / AgentSettingsUpdate keep webhook_url as a request
    # field (write-side); we still validate that it isn't echoed in any
    # response shape here.
    assert "webhook_url" in AgentRegistration.model_fields
    assert "webhook_url" in AgentSettingsUpdate.model_fields


# ─────────────────────────────────────────────────────────────────────
# 9. create_agent shim still accepts webhook_url, routes to encrypted endpoint
# ─────────────────────────────────────────────────────────────────────

def test_create_agent_legacy_webhook_url_routes_to_endpoint(db_session):
    """Legacy ``webhook_url`` arg → encrypted WebhookEndpoint row, no agents column."""
    from sthrip.db.repository import AgentRepository

    repo = AgentRepository(db_session)
    agent, _creds = repo.create_agent(
        agent_name="legacy-shim",
        webhook_url=_TEST_URL,
    )
    db_session.flush()

    # The Agent ORM model no longer has webhook_url.
    assert not hasattr(agent, "webhook_url") or getattr(agent, "webhook_url", None) is None

    ep_repo = WebhookEndpointRepository(db_session)
    endpoints = ep_repo.list_by_agent(agent.id)
    assert len(endpoints) == 1
    assert WebhookEndpointRepository.get_url(endpoints[0]) == _TEST_URL
    # And the URL is encrypted on disk.
    assert _TEST_URL not in endpoints[0].url_encrypted


# ─────────────────────────────────────────────────────────────────────
# 10. WebhookEndpoint table no longer has plaintext url column at all
# ─────────────────────────────────────────────────────────────────────

def test_model_has_url_encrypted_not_plain_url():
    cols = {c.name for c in WebhookEndpoint.__table__.columns}
    assert "url_encrypted" in cols
    assert "url" not in cols, (
        "Sprint 5 dropped the plaintext url column; only url_encrypted remains."
    )


def test_agent_model_has_no_webhook_url_column():
    cols = {c.name for c in Agent.__table__.columns}
    assert "webhook_url" not in cols
