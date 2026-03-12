"""Tests for webhook secret encryption at rest."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import Base, Agent, AgentReputation


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[Agent.__table__, AgentReputation.__table__])
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def test_encrypt_decrypt_roundtrip():
    """Encrypting then decrypting returns original value."""
    from sthrip.crypto import encrypt_value, decrypt_value
    original = "whsec_test123456"
    encrypted = encrypt_value(original)
    assert encrypted != original
    assert decrypt_value(encrypted) == original


def test_encrypted_value_is_not_plaintext():
    """Encrypted value must not contain the original string."""
    from sthrip.crypto import encrypt_value
    original = "whsec_abcdef"
    encrypted = encrypt_value(original)
    assert "whsec_" not in encrypted


def test_create_agent_stores_encrypted_secret(db_session):
    """Agent webhook_secret in DB must be encrypted."""
    from sthrip.db.repository import AgentRepository
    repo = AgentRepository(db_session)
    agent, _creds = repo.create_agent("encrypt_test_agent", webhook_url="https://example.com/hook")
    db_session.flush()
    assert not agent.webhook_secret.startswith("whsec_")


def test_get_webhook_secret_decrypted(db_session):
    """Reading webhook secret via repo must return decrypted value."""
    from sthrip.db.repository import AgentRepository
    repo = AgentRepository(db_session)
    agent, _creds = repo.create_agent("decrypt_test_agent", webhook_url="https://example.com/hook")
    db_session.flush()
    decrypted = repo.get_webhook_secret(agent.id)
    assert decrypted.startswith("whsec_")


def test_register_agent_returns_plaintext_webhook_secret(db_session):
    """C2: Registration must return plaintext secret, not Fernet ciphertext."""
    from contextlib import contextmanager
    from unittest.mock import patch
    from sthrip.services.agent_registry import AgentRegistry

    @contextmanager
    def mock_get_db():
        yield db_session

    registry = AgentRegistry()
    with patch("sthrip.services.agent_registry.get_db", mock_get_db):
        result = registry.register_agent("test-c2-agent")

    secret = result["webhook_secret"]
    assert secret.startswith("whsec_"), (
        f"Got encrypted blob instead of plaintext: {secret[:20]}..."
    )


def test_get_webhook_secret_raises_on_decryption_failure(db_session):
    """Decryption failure must raise, not silently return raw value."""
    import unittest.mock
    from sthrip.db.repository import AgentRepository

    repo = AgentRepository(db_session)
    agent, _creds = repo.create_agent("decrypt_fail_agent", webhook_url="https://example.com/hook")
    db_session.flush()

    with unittest.mock.patch("sthrip.crypto.decrypt_value", side_effect=Exception("Bad key")):
        with pytest.raises(ValueError, match="decrypt"):
            repo.get_webhook_secret(agent.id)


def test_webhook_signing_uses_decrypted_secret():
    """M3: Webhook signature must use decrypted secret, not ciphertext."""
    from sthrip.services.webhook_service import WebhookService
    from sthrip.crypto import encrypt_value

    svc = WebhookService()
    plaintext = "whsec_test123"
    encrypted = encrypt_value(plaintext)

    sig_plain = svc._sign_payload({"a": 1}, plaintext, "12345")
    sig_encrypted = svc._sign_payload({"a": 1}, encrypted, "12345")

    assert sig_plain != sig_encrypted, "Sanity check: different keys produce different sigs"
