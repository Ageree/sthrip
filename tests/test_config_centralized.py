"""Tests for centralized Settings config."""

import pytest


def test_settings_loads_from_env(monkeypatch):
    from sthrip.config import Settings
    monkeypatch.setenv("ADMIN_API_KEY", "test-secure-key-12345")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///test.db")
    s = Settings()
    assert s.admin_api_key == "test-secure-key-12345"
    assert s.environment == "dev"


def test_settings_rejects_weak_admin_key_in_production(monkeypatch):
    from sthrip.config import Settings
    monkeypatch.setenv("ADMIN_API_KEY", "change_me")
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(Exception):
        Settings()


def test_settings_has_all_required_fields():
    """Ensure Settings defines fields for all env vars used in the codebase."""
    from sthrip.config import Settings
    fields = Settings.model_fields
    required = [
        "environment", "database_url", "redis_url", "admin_api_key",
        "hub_mode", "monero_rpc_host", "monero_rpc_port",
        "monero_network", "monero_min_confirmations",
        "cors_origins", "trusted_proxy_hosts", "sentry_dsn",
        "log_level", "port", "deposit_poll_interval",
        "log_format", "sql_echo", "alert_webhook_url",
    ]
    for field in required:
        assert field in fields, f"Missing field: {field}"


def test_stagenet_rejects_default_hmac_secret():
    """CRIT-2: stagenet with default HMAC secret must raise ValueError."""
    from sthrip.config import Settings

    with pytest.raises(ValueError, match="API_KEY_HMAC_SECRET"):
        Settings(
            admin_api_key="sk_real_key_here_very_long_and_secure",
            api_key_hmac_secret="dev-hmac-secret-change-in-prod",
            webhook_encryption_key="uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE=",
            environment="stagenet",
            monero_rpc_host="10.0.0.1",
            hub_mode="ledger",
        )


def test_stagenet_rejects_empty_webhook_encryption_key():
    """CRIT-3: stagenet with empty webhook_encryption_key must raise ValueError."""
    from sthrip.config import Settings

    with pytest.raises(ValueError, match="WEBHOOK_ENCRYPTION_KEY"):
        Settings(
            admin_api_key="sk_real_key_here_very_long_and_secure",
            api_key_hmac_secret="real-hmac-secret-here-long-enough-32c",
            webhook_encryption_key="",
            environment="stagenet",
            monero_rpc_host="10.0.0.1",
            hub_mode="ledger",
        )


def test_dev_with_defaults_passes():
    """Dev environment with default secrets should still pass validation."""
    from sthrip.config import Settings

    s = Settings(
        admin_api_key="test-key-12345",
        api_key_hmac_secret="dev-hmac-secret-change-in-prod",
        webhook_encryption_key="",
        environment="dev",
        hub_mode="ledger",
    )
    assert s.environment == "dev"
    assert s.api_key_hmac_secret == "dev-hmac-secret-change-in-prod"
    assert s.webhook_encryption_key == ""
