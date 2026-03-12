"""Tests for centralized configuration."""

import os
from unittest.mock import patch

import pytest


class TestSettingsValidation:
    """Config validation rejects insecure values."""

    def test_rejects_placeholder_admin_key_in_production(self):
        env = {
            "ADMIN_API_KEY": "change_me",
            "ENVIRONMENT": "production",
        }
        with patch.dict(os.environ, env, clear=False):
            from sthrip.config import Settings

            with pytest.raises(Exception):
                Settings(admin_api_key="change_me", api_key_hmac_secret="test-hmac-secret-long-enough-32char!", environment="production")

    def test_accepts_valid_config_in_dev(self):
        from sthrip.config import Settings

        s = Settings(admin_api_key="dev-admin-key", environment="dev")
        assert s.environment == "dev"

    def test_rejects_invalid_environment(self):
        from sthrip.config import Settings

        with pytest.raises(Exception):
            Settings(admin_api_key="key", environment="invalid")

    def test_rejects_empty_rpc_pass_in_production_onchain(self):
        from sthrip.config import Settings

        with pytest.raises(Exception):
            Settings(
                admin_api_key="real-key-here",
                api_key_hmac_secret="test-hmac-secret-long-enough-32char!",
                environment="production",
                hub_mode="onchain",
                monero_rpc_pass="",
            )

    def test_accepts_empty_rpc_pass_in_ledger_mode(self):
        from sthrip.config import Settings

        s = Settings(
            admin_api_key="real-key-here",
            api_key_hmac_secret="test-hmac-secret-long-enough-32char!",
            webhook_encryption_key="uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE=",
            environment="production",
            monero_network="mainnet",
            hub_mode="ledger",
            monero_rpc_host="10.0.0.1",
            monero_rpc_pass="",
        )
        assert s.hub_mode == "ledger"

    def test_accepts_stagenet_environment(self):
        from sthrip.config import Settings

        # stagenet now requires proper secrets (CRIT-2, CRIT-3)
        s = Settings(
            admin_api_key="sk_real_key_here_very_long_and_secure",
            api_key_hmac_secret="real-hmac-secret-here-long-enough-32c",
            webhook_encryption_key="uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE=",
            environment="stagenet",
            hub_mode="ledger",
            monero_rpc_host="10.0.0.1",
        )
        assert s.environment == "stagenet"

    def test_production_rejects_stagenet(self):
        from sthrip.config import Settings

        with pytest.raises(Exception, match="mainnet"):
            Settings(
                admin_api_key="sk_real_key_here_very_long_and_secure",
                api_key_hmac_secret="real-hmac-secret-here-long-enough-32c",
                webhook_encryption_key="uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE=",
                environment="production",
                monero_network="stagenet",
                hub_mode="onchain",
                monero_rpc_host="10.0.0.1",
                monero_rpc_pass="real_rpc_pass_here",
            )

    def test_stagenet_allowed_in_non_production(self):
        from sthrip.config import Settings

        s = Settings(
            admin_api_key="sk_real_key_here_very_long_and_secure",
            api_key_hmac_secret="real-hmac-secret-here-long-enough-32c",
            webhook_encryption_key="uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE=",
            environment="stagenet",
            monero_network="stagenet",
            hub_mode="ledger",
            monero_rpc_host="10.0.0.1",
        )
        assert s.monero_network == "stagenet"

    def test_accepts_valid_production_config(self):
        from sthrip.config import Settings

        s = Settings(
            admin_api_key="sk_real_key_here_very_long_and_secure",
            api_key_hmac_secret="real-hmac-secret-here-long-enough-32c",
            webhook_encryption_key="uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE=",
            environment="production",
            hub_mode="onchain",
            monero_network="mainnet",
            monero_rpc_host="10.0.0.1",
            monero_rpc_pass="real_rpc_pass_here",
        )
        assert s.environment == "production"
        assert s.monero_rpc_pass == "real_rpc_pass_here"


def test_webhook_encryption_key_required_in_production(monkeypatch):
    """C3: Production must have explicit WEBHOOK_ENCRYPTION_KEY."""
    import importlib
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("MONERO_NETWORK", "mainnet")
    monkeypatch.setenv("ADMIN_API_KEY", "secure-key-123")
    monkeypatch.setenv("API_KEY_HMAC_SECRET", "secure-hmac-secret-long-enough-32chars!")
    monkeypatch.setenv("WEBHOOK_ENCRYPTION_KEY", "")
    monkeypatch.setenv("MONERO_RPC_PASS", "secure-pass")

    from sthrip.config import Settings
    import pytest
    with pytest.raises(ValueError, match="WEBHOOK_ENCRYPTION_KEY"):
        Settings()
