"""
TDD tests for migrating os.getenv usage to centralized get_settings().

RED phase: these tests document expected behavior after migration.
They verify that:
1. logging_config.setup_logging() reads config from get_settings(), not os.getenv
2. Sthrip.from_env() reads Monero settings from get_settings(), not os.getenv
3. create_regtest_client() reads BTC settings from get_settings(), not os.getenv
4. No active (non-legacy, non-fallback) os.getenv calls exist in sthrip/logging_config.py
"""

import logging
import os
import pytest
from unittest.mock import patch, MagicMock, call


# ---------------------------------------------------------------------------
# 1. logging_config.py — fallback os.getenv must be removed
# ---------------------------------------------------------------------------

class TestLoggingConfigUsesSettings:
    """setup_logging() must read values from get_settings(), never os.getenv directly."""

    def test_setup_logging_reads_log_format_from_settings(self, monkeypatch):
        """When LOG_FORMAT env var is set, setup_logging reads it via get_settings()."""
        from sthrip.config import get_settings
        monkeypatch.setenv("LOG_FORMAT", "json")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        get_settings.cache_clear()

        from sthrip.logging_config import setup_logging, JSONFormatter
        setup_logging()

        root = logging.getLogger()
        json_handlers = [h for h in root.handlers if isinstance(h.formatter, JSONFormatter)]
        assert len(json_handlers) >= 1, "Expected JSONFormatter when LOG_FORMAT=json"

    def test_setup_logging_reads_log_level_from_settings(self, monkeypatch):
        """When LOG_LEVEL env var is set, setup_logging reads it via get_settings()."""
        from sthrip.config import get_settings
        monkeypatch.setenv("LOG_FORMAT", "text")
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        get_settings.cache_clear()

        from sthrip.logging_config import setup_logging
        setup_logging()

        root = logging.getLogger()
        assert root.level == logging.WARNING

    def test_setup_logging_reads_betterstack_token_from_settings(self, monkeypatch):
        """betterstack_token from Settings is used (env key: BETTERSTACK_TOKEN)."""
        from sthrip.config import get_settings
        monkeypatch.setenv("BETTERSTACK_TOKEN", "tok_from_settings_123")
        monkeypatch.setenv("LOG_FORMAT", "text")
        get_settings.cache_clear()

        mock_handler = MagicMock()
        mock_logtail_cls = MagicMock(return_value=mock_handler)

        with patch.dict("sys.modules", {"logtail": MagicMock(LogtailHandler=mock_logtail_cls)}):
            from sthrip.logging_config import setup_logging
            setup_logging()

        mock_logtail_cls.assert_called_once_with(source_token="tok_from_settings_123")

    def test_setup_logging_does_not_call_os_getenv_for_betterstack(self, monkeypatch):
        """
        Ensure setup_logging does NOT use the old key BETTERSTACK_SOURCE_TOKEN
        (which was a bug where the os.getenv fallback used a different key than
        the Settings field 'betterstack_token' which reads BETTERSTACK_TOKEN).
        """
        from sthrip.config import get_settings
        # Set only the OLD wrong key — if code still uses os.getenv("BETTERSTACK_SOURCE_TOKEN")
        # instead of settings.betterstack_token, this would incorrectly add the handler.
        monkeypatch.setenv("BETTERSTACK_SOURCE_TOKEN", "old_key_should_be_ignored")
        monkeypatch.delenv("BETTERSTACK_TOKEN", raising=False)
        monkeypatch.setenv("LOG_FORMAT", "text")
        get_settings.cache_clear()

        mock_logtail_cls = MagicMock()

        with patch.dict("sys.modules", {"logtail": MagicMock(LogtailHandler=mock_logtail_cls)}):
            from sthrip.logging_config import setup_logging
            setup_logging()

        # The old BETTERSTACK_SOURCE_TOKEN must NOT trigger the Logtail handler
        mock_logtail_cls.assert_not_called()

    def test_setup_logging_fallback_does_not_use_os_getenv(self, monkeypatch):
        """
        Even when get_settings() raises (e.g. missing ADMIN_API_KEY),
        the fallback path must not use os.getenv — it should use safe defaults.
        This test verifies the except block does not call os.getenv.
        """
        from sthrip.config import get_settings
        get_settings.cache_clear()

        # Simulate settings failure by making get_settings raise
        with patch("sthrip.logging_config.get_settings", side_effect=Exception("settings error")):
            original_getenv = os.getenv
            getenv_calls = []

            def tracking_getenv(key, *args, **kwargs):
                getenv_calls.append(key)
                return original_getenv(key, *args, **kwargs)

            with patch("os.getenv", side_effect=tracking_getenv):
                from sthrip.logging_config import setup_logging
                setup_logging()

        # The fallback must not call os.getenv for config keys
        config_keys = {"LOG_FORMAT", "LOG_LEVEL", "BETTERSTACK_SOURCE_TOKEN", "BETTERSTACK_TOKEN"}
        called_config_keys = set(getenv_calls) & config_keys
        assert not called_config_keys, (
            f"setup_logging fallback called os.getenv for config keys: {called_config_keys}. "
            "Use safe defaults instead."
        )


# ---------------------------------------------------------------------------
# 2. client.py — Sthrip.from_env() must use get_settings()
# ---------------------------------------------------------------------------

class TestSthrip_from_env_UsesSettings:
    """Sthrip.from_env() must read Monero RPC config from get_settings()."""

    def test_from_env_reads_rpc_host_from_settings(self, monkeypatch):
        """MONERO_RPC_HOST is read via get_settings(), not os.getenv directly."""
        from sthrip.config import get_settings
        monkeypatch.setenv("MONERO_RPC_HOST", "192.168.1.50")
        monkeypatch.setenv("HUB_MODE", "ledger")  # avoid RPC validation in Settings
        get_settings.cache_clear()

        mock_wallet_cls = MagicMock()
        mock_wallet_instance = MagicMock()
        mock_wallet_cls.return_value = mock_wallet_instance
        mock_wallet_instance.get_height.return_value = 1000

        with patch("sthrip.client.MoneroWalletRPC", mock_wallet_cls):
            with patch("sthrip.client.StealthAddressManager"), \
                 patch("sthrip.client.EscrowManager"), \
                 patch("sthrip.client.ChannelManager"), \
                 patch("sthrip.client.PrivacyEnhancer"), \
                 patch("sthrip.client.TransactionScheduler"):
                from sthrip.client import Sthrip
                Sthrip.from_env()

        # Verify MoneroWalletRPC was called with host from settings
        call_kwargs = mock_wallet_cls.call_args
        assert call_kwargs is not None, "MoneroWalletRPC was never called"
        host_arg = call_kwargs.kwargs.get("host") or (call_kwargs.args[0] if call_kwargs.args else None)
        assert host_arg == "192.168.1.50", (
            f"Expected host='192.168.1.50' from settings, got '{host_arg}'"
        )

    def test_from_env_reads_rpc_port_from_settings(self, monkeypatch):
        """MONERO_RPC_PORT is read via get_settings(), not os.getenv directly."""
        from sthrip.config import get_settings
        monkeypatch.setenv("MONERO_RPC_PORT", "19999")
        monkeypatch.setenv("HUB_MODE", "ledger")
        get_settings.cache_clear()

        mock_wallet_cls = MagicMock()
        mock_wallet_instance = MagicMock()
        mock_wallet_cls.return_value = mock_wallet_instance
        mock_wallet_instance.get_height.return_value = 1000

        with patch("sthrip.client.MoneroWalletRPC", mock_wallet_cls):
            with patch("sthrip.client.StealthAddressManager"), \
                 patch("sthrip.client.EscrowManager"), \
                 patch("sthrip.client.ChannelManager"), \
                 patch("sthrip.client.PrivacyEnhancer"), \
                 patch("sthrip.client.TransactionScheduler"):
                from sthrip.client import Sthrip
                Sthrip.from_env()

        call_kwargs = mock_wallet_cls.call_args
        port_arg = call_kwargs.kwargs.get("port") or (call_kwargs.args[1] if len(call_kwargs.args) > 1 else None)
        assert port_arg == 19999, (
            f"Expected port=19999 from settings, got '{port_arg}'"
        )

    def test_from_env_reads_rpc_user_from_settings(self, monkeypatch):
        """MONERO_RPC_USER is read via get_settings(), not os.getenv directly."""
        from sthrip.config import get_settings
        monkeypatch.setenv("MONERO_RPC_USER", "settings_user")
        monkeypatch.setenv("HUB_MODE", "ledger")
        get_settings.cache_clear()

        mock_wallet_cls = MagicMock()
        mock_wallet_instance = MagicMock()
        mock_wallet_cls.return_value = mock_wallet_instance
        mock_wallet_instance.get_height.return_value = 1000

        with patch("sthrip.client.MoneroWalletRPC", mock_wallet_cls):
            with patch("sthrip.client.StealthAddressManager"), \
                 patch("sthrip.client.EscrowManager"), \
                 patch("sthrip.client.ChannelManager"), \
                 patch("sthrip.client.PrivacyEnhancer"), \
                 patch("sthrip.client.TransactionScheduler"):
                from sthrip.client import Sthrip
                Sthrip.from_env()

        call_kwargs = mock_wallet_cls.call_args
        user_arg = call_kwargs.kwargs.get("user") or (call_kwargs.args[2] if len(call_kwargs.args) > 2 else None)
        assert user_arg == "settings_user", (
            f"Expected user='settings_user' from settings, got '{user_arg}'"
        )

    def test_from_env_does_not_call_os_getenv_for_rpc_config(self, monkeypatch):
        """
        Sthrip.from_env() must not directly call os.getenv for Monero RPC keys.
        These values must come exclusively through get_settings().
        """
        from sthrip.config import get_settings
        monkeypatch.setenv("HUB_MODE", "ledger")
        get_settings.cache_clear()

        rpc_keys = {"MONERO_RPC_HOST", "MONERO_RPC_PORT", "MONERO_RPC_USER", "MONERO_RPC_PASS"}
        original_getenv = os.getenv
        getenv_calls = []

        def tracking_getenv(key, *args, **kwargs):
            getenv_calls.append(key)
            return original_getenv(key, *args, **kwargs)

        mock_wallet_cls = MagicMock()
        mock_wallet_instance = MagicMock()
        mock_wallet_cls.return_value = mock_wallet_instance
        mock_wallet_instance.get_height.return_value = 1000

        with patch("sthrip.client.MoneroWalletRPC", mock_wallet_cls):
            with patch("sthrip.client.StealthAddressManager"), \
                 patch("sthrip.client.EscrowManager"), \
                 patch("sthrip.client.ChannelManager"), \
                 patch("sthrip.client.PrivacyEnhancer"), \
                 patch("sthrip.client.TransactionScheduler"), \
                 patch("os.getenv", side_effect=tracking_getenv):
                from sthrip.client import Sthrip
                Sthrip.from_env()

        called_rpc_keys = set(getenv_calls) & rpc_keys
        assert not called_rpc_keys, (
            f"Sthrip.from_env() called os.getenv for: {called_rpc_keys}. "
            "Use get_settings() instead."
        )


# ---------------------------------------------------------------------------
# 3. swaps/btc/rpc_client.py — create_regtest_client() must use get_settings()
# ---------------------------------------------------------------------------

class TestCreateRegtestClientUsesSettings:
    """create_regtest_client() must read BTC regtest config from get_settings()."""

    def test_create_regtest_client_reads_host_from_settings(self, monkeypatch):
        """BTC_REGTEST_HOST is read via get_settings(), not os.getenv directly."""
        from sthrip.config import get_settings, Settings
        monkeypatch.setenv("BTC_REGTEST_HOST", "btc-node.internal")
        get_settings.cache_clear()

        # Verify Settings has the field
        assert "btc_regtest_host" in Settings.model_fields, (
            "Settings must have btc_regtest_host field for BTC_REGTEST_HOST env var"
        )

        settings = get_settings()
        assert settings.btc_regtest_host == "btc-node.internal"

    def test_create_regtest_client_reads_port_from_settings(self, monkeypatch):
        """BTC_REGTEST_PORT is read via get_settings(), not os.getenv directly."""
        from sthrip.config import get_settings, Settings
        monkeypatch.setenv("BTC_REGTEST_PORT", "19443")
        get_settings.cache_clear()

        assert "btc_regtest_port" in Settings.model_fields, (
            "Settings must have btc_regtest_port field for BTC_REGTEST_PORT env var"
        )

        settings = get_settings()
        assert settings.btc_regtest_port == 19443

    def test_create_regtest_client_reads_credentials_from_settings(self, monkeypatch):
        """BTC_REGTEST_USER and BTC_REGTEST_PASS are read via get_settings()."""
        from sthrip.config import get_settings, Settings
        monkeypatch.setenv("BTC_REGTEST_USER", "btc_user")
        monkeypatch.setenv("BTC_REGTEST_PASS", "btc_pass")
        get_settings.cache_clear()

        assert "btc_regtest_user" in Settings.model_fields, (
            "Settings must have btc_regtest_user field"
        )
        assert "btc_regtest_pass" in Settings.model_fields, (
            "Settings must have btc_regtest_pass field"
        )

        settings = get_settings()
        assert settings.btc_regtest_user == "btc_user"
        assert settings.btc_regtest_pass == "btc_pass"

    def test_create_regtest_client_does_not_call_os_getenv(self, monkeypatch):
        """
        create_regtest_client() must not directly call os.getenv for BTC regtest keys.
        """
        from sthrip.config import get_settings
        get_settings.cache_clear()

        btc_keys = {"BTC_REGTEST_HOST", "BTC_REGTEST_PORT", "BTC_REGTEST_USER", "BTC_REGTEST_PASS"}
        original_getenv = os.getenv
        getenv_calls = []

        def tracking_getenv(key, *args, **kwargs):
            getenv_calls.append(key)
            return original_getenv(key, *args, **kwargs)

        with patch("os.getenv", side_effect=tracking_getenv):
            with patch("requests.Session"):  # prevent actual HTTP calls
                from sthrip.swaps.btc.rpc_client import create_regtest_client
                create_regtest_client()

        called_btc_keys = set(getenv_calls) & btc_keys
        assert not called_btc_keys, (
            f"create_regtest_client() called os.getenv for: {called_btc_keys}. "
            "Use get_settings() instead."
        )

    def test_create_regtest_client_uses_settings_values(self, monkeypatch):
        """create_regtest_client() passes settings values to BitcoinRPCClient."""
        from sthrip.config import get_settings
        monkeypatch.setenv("BTC_REGTEST_HOST", "test-btc-host")
        monkeypatch.setenv("BTC_REGTEST_PORT", "19999")
        monkeypatch.setenv("BTC_REGTEST_USER", "test_user")
        monkeypatch.setenv("BTC_REGTEST_PASS", "test_pass")
        get_settings.cache_clear()

        from sthrip.swaps.btc.rpc_client import create_regtest_client, BitcoinRPCClient

        with patch.object(BitcoinRPCClient, "__init__", return_value=None) as mock_init:
            create_regtest_client()

        mock_init.assert_called_once()
        call_kwargs = mock_init.call_args.kwargs
        assert call_kwargs.get("host") == "test-btc-host"
        assert call_kwargs.get("port") == 19999
        assert call_kwargs.get("username") == "test_user"
        assert call_kwargs.get("password") == "test_pass"


# ---------------------------------------------------------------------------
# 4. Dead-code audit: verify bridge/swap modules are not imported by active API
# ---------------------------------------------------------------------------

class TestLegacyModulesNotImportedByActiveCode:
    """
    bridge/ and swaps/ modules are legacy/CLI-only and must not be imported
    by any active API or service module.
    """

    def test_api_main_does_not_import_bridge(self):
        """api/main_v2.py must not import sthrip.bridge.*"""
        import ast
        main_path = os.path.join(
            os.path.dirname(__file__), "..", "api", "main_v2.py"
        )
        with open(main_path) as f:
            tree = ast.parse(f.read())

        bridge_imports = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and any(
                (alias.name or "").startswith("sthrip.bridge")
                for alias in getattr(node, "names", [])
            )
            or (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith("sthrip.bridge")
            )
        ]
        assert not bridge_imports, (
            f"api/main_v2.py imports bridge modules: {bridge_imports}. "
            "Bridge is legacy/CLI-only."
        )

    def test_api_main_does_not_import_swaps(self):
        """api/main_v2.py must not import sthrip.swaps.*"""
        import ast
        main_path = os.path.join(
            os.path.dirname(__file__), "..", "api", "main_v2.py"
        )
        with open(main_path) as f:
            tree = ast.parse(f.read())

        swap_imports = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("sthrip.swaps")
        ]
        assert not swap_imports, (
            f"api/main_v2.py imports swap modules. Swaps is legacy/CLI-only."
        )

    def test_services_do_not_import_client(self):
        """sthrip/services/*.py must not import sthrip.client (legacy standalone client)."""
        import ast
        services_dir = os.path.join(
            os.path.dirname(__file__), "..", "sthrip", "services"
        )
        violations = []
        for fname in os.listdir(services_dir):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(services_dir, fname)
            with open(fpath) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "") == "sthrip.client":
                    violations.append(fname)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "sthrip.client":
                            violations.append(fname)
        assert not violations, (
            f"Service modules import sthrip.client (legacy): {violations}"
        )

    def test_api_routers_do_not_import_bridge_or_swaps(self):
        """api/routers/*.py must not import bridge or swap modules."""
        import ast
        routers_dir = os.path.join(
            os.path.dirname(__file__), "..", "api", "routers"
        )
        violations = []
        for fname in os.listdir(routers_dir):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(routers_dir, fname)
            with open(fpath) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module.startswith("sthrip.bridge") or module.startswith("sthrip.swaps"):
                        violations.append(f"{fname}: {module}")
        assert not violations, (
            f"Router modules import legacy bridge/swap modules: {violations}"
        )
