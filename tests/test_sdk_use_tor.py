"""Sprint 6: SDK ``Sthrip(use_tor=True)`` configures SOCKS5 transport.

Acceptance criteria:
1. Default ``use_tor=False`` is byte-for-byte unchanged — no proxy mounted.
2. ``use_tor=True`` mounts ``socks5h://127.0.0.1:9050`` (default).
3. ``STHRIP_TOR_SOCKS_PROXY`` env var overrides the default proxy URL.

Import strategy: the SDK lives at ``sdk/sthrip/`` but the repo root also has
a ``sthrip/`` package (the server-side library). To avoid the namespace clash
we reuse the bootstrap from ``test_sdk.py`` — loading the SDK modules under
the ``sthrip_sdk`` alias.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Bootstrap (same pattern as tests/test_sdk.py).
# ---------------------------------------------------------------------------

_SDK_STHRIP_DIR = Path(__file__).parent.parent / "sdk" / "sthrip"


def _load_sdk_module(alias, filename):
    path = _SDK_STHRIP_DIR / filename
    spec = importlib.util.spec_from_file_location(alias, str(path))
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "sthrip_sdk"
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


def _bootstrap_sdk():
    if "sthrip_sdk" in sys.modules:
        return
    pkg = types.ModuleType("sthrip_sdk")
    pkg.__path__ = [str(_SDK_STHRIP_DIR)]
    pkg.__package__ = "sthrip_sdk"
    sys.modules["sthrip_sdk"] = pkg
    exc_mod = _load_sdk_module("sthrip_sdk.exceptions", "exceptions.py")
    auth_mod = _load_sdk_module("sthrip_sdk.auth", "auth.py")
    client_mod = _load_sdk_module("sthrip_sdk.client", "client.py")
    pkg.exceptions = exc_mod
    pkg.auth = auth_mod
    pkg.client = client_mod


_bootstrap_sdk()

from sthrip_sdk.client import Sthrip, _DEFAULT_TOR_SOCKS_PROXY  # noqa: E402


class TestSdkUseTor:
    def _build(self, **kwargs):
        """Construct an Sthrip without auto-registration / spending sync."""
        with patch.object(Sthrip, "_resolve_api_key", return_value="test-key"), \
             patch.object(Sthrip, "_sync_spending_policy", return_value=None):
            return Sthrip(api_url="https://example.test", **kwargs)

    def test_use_tor_default_false_no_socks(self, monkeypatch):
        monkeypatch.delenv("STHRIP_TOR_SOCKS_PROXY", raising=False)
        client = self._build()
        # No proxies dict means default behaviour. requests defaults to {}.
        assert not client._session.proxies, (
            f"expected no proxies on default client; got {client._session.proxies}"
        )
        assert client._use_tor is False
        assert client._tor_proxy is None

    def test_use_tor_true_configures_socks5_transport(self, monkeypatch):
        monkeypatch.delenv("STHRIP_TOR_SOCKS_PROXY", raising=False)
        client = self._build(use_tor=True)
        proxies = client._session.proxies
        assert proxies.get("http") == _DEFAULT_TOR_SOCKS_PROXY
        assert proxies.get("https") == _DEFAULT_TOR_SOCKS_PROXY
        assert _DEFAULT_TOR_SOCKS_PROXY == "socks5h://127.0.0.1:9050"

    def test_use_tor_respects_env_proxy(self, monkeypatch):
        monkeypatch.setenv(
            "STHRIP_TOR_SOCKS_PROXY", "socks5h://tor-sidecar.internal:9050",
        )
        client = self._build(use_tor=True)
        proxies = client._session.proxies
        assert proxies["http"] == "socks5h://tor-sidecar.internal:9050"
        assert proxies["https"] == "socks5h://tor-sidecar.internal:9050"

    def test_use_tor_false_ignores_env_proxy(self, monkeypatch):
        """Setting the env without opt-in must NOT mount the proxy."""
        monkeypatch.setenv(
            "STHRIP_TOR_SOCKS_PROXY", "socks5h://malicious.example:9050",
        )
        client = self._build(use_tor=False)
        assert not client._session.proxies

    def test_use_tor_attribute_persists(self):
        client = self._build(use_tor=True)
        assert client._use_tor is True
        client2 = self._build(use_tor=False)
        assert client2._use_tor is False

    @pytest.mark.parametrize(
        "value, expected",
        [(True, True), (False, False), (1, True), (0, False)],
    )
    def test_use_tor_truthiness(self, value, expected):
        client = self._build(use_tor=value)
        assert client._use_tor is expected
