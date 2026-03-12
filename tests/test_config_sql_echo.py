"""Test that SQL_ECHO=true is rejected in production."""
import pytest


def test_sql_echo_rejected_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SQL_ECHO", "true")
    monkeypatch.setenv("ADMIN_API_KEY", "a" * 32)
    monkeypatch.setenv("API_KEY_HMAC_SECRET", "b" * 32)
    monkeypatch.setenv("WEBHOOK_ENCRYPTION_KEY", "uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE=")
    monkeypatch.setenv("MONERO_RPC_HOST", "10.0.0.1")
    monkeypatch.setenv("MONERO_RPC_PASS", "secure-pass-123")
    monkeypatch.setenv("HUB_MODE", "onchain")
    monkeypatch.setenv("MONERO_NETWORK", "mainnet")

    from sthrip.config import Settings

    with pytest.raises(SystemExit):
        Settings()


def test_sql_echo_allowed_in_dev(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("SQL_ECHO", "true")
    monkeypatch.setenv("ADMIN_API_KEY", "test-key")

    from sthrip.config import Settings

    settings = Settings()
    assert settings.sql_echo is True
