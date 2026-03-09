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
                Settings(admin_api_key="change_me", environment="production")

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
                environment="production",
                hub_mode="onchain",
                monero_rpc_pass="",
            )

    def test_accepts_empty_rpc_pass_in_ledger_mode(self):
        from sthrip.config import Settings

        s = Settings(
            admin_api_key="real-key-here",
            environment="production",
            hub_mode="ledger",
            monero_rpc_pass="",
        )
        assert s.hub_mode == "ledger"

    def test_accepts_valid_production_config(self):
        from sthrip.config import Settings

        s = Settings(
            admin_api_key="sk_real_key_here_very_long_and_secure",
            environment="production",
            hub_mode="onchain",
            monero_rpc_pass="real_rpc_pass_here",
        )
        assert s.environment == "production"
        assert s.monero_rpc_pass == "real_rpc_pass_here"
