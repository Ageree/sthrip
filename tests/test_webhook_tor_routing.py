"""Sprint 6: webhook delivery routes through Tor SOCKS5 ONLY for ``.onion``
targets when ``STHRIP_ONION_ENABLED`` is truthy.

Per Lead decision Q4: outbound Tor only for ``.onion``, never all-traffic.

These tests cover the routing decision (which code path is taken) by spying
on the two delivery helpers — ``_send_webhook_via_tor`` (Tor path) and the
existing IP-pinning code that calls ``resolve_and_validate`` (clearnet path).
We do NOT actually open sockets.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from sthrip.services.webhook_service import (
    WebhookResult,
    WebhookService,
    _is_onion_url,
    _onion_routing_enabled,
    _should_route_via_tor,
    _tor_socks_proxy,
)


# ---------------------------------------------------------------------------
# Pure routing-predicate tests (no IO, no async).
# ---------------------------------------------------------------------------


class TestRoutingPredicates:
    def test_is_onion_url_true(self):
        assert _is_onion_url("http://abc123.onion/webhook")
        assert _is_onion_url("https://abc123.ONION/")
        assert _is_onion_url(
            "https://exampleabcdefghijklmnopqrstuvwxyz234567.onion/x"
        )

    def test_is_onion_url_false(self):
        assert not _is_onion_url("https://example.com/")
        assert not _is_onion_url("http://10.0.0.1/x")
        assert not _is_onion_url("https://onion.example.com/x")  # not endswith
        assert not _is_onion_url("not a url")

    def test_onion_routing_enabled_truthy(self, monkeypatch):
        for v in ("1", "true", "yes", "on", "TRUE"):
            monkeypatch.setenv("STHRIP_ONION_ENABLED", v)
            assert _onion_routing_enabled() is True

    def test_onion_routing_enabled_falsy(self, monkeypatch):
        for v in ("0", "false", "no", "off", "", "junk"):
            monkeypatch.setenv("STHRIP_ONION_ENABLED", v)
            assert _onion_routing_enabled() is False

    def test_onion_routing_enabled_unset(self, monkeypatch):
        monkeypatch.delenv("STHRIP_ONION_ENABLED", raising=False)
        assert _onion_routing_enabled() is False

    def test_tor_socks_proxy_default(self, monkeypatch):
        monkeypatch.delenv("STHRIP_TOR_SOCKS_PROXY", raising=False)
        assert _tor_socks_proxy() == "socks5h://127.0.0.1:9050"

    def test_tor_socks_proxy_override(self, monkeypatch):
        monkeypatch.setenv(
            "STHRIP_TOR_SOCKS_PROXY", "socks5h://tor.internal:9050",
        )
        assert _tor_socks_proxy() == "socks5h://tor.internal:9050"

    def test_should_route_via_tor_matrix(self, monkeypatch):
        # flag on + onion target -> True
        monkeypatch.setenv("STHRIP_ONION_ENABLED", "true")
        assert _should_route_via_tor("https://abc.onion/x") is True
        # flag on + clearnet target -> False (Lead Q4)
        assert _should_route_via_tor("https://example.com/x") is False
        # flag off + onion target -> False
        monkeypatch.setenv("STHRIP_ONION_ENABLED", "false")
        assert _should_route_via_tor("https://abc.onion/x") is False
        # flag off + clearnet target -> False
        assert _should_route_via_tor("https://example.com/x") is False


# ---------------------------------------------------------------------------
# Routing dispatch — does ``_send_webhook`` choose the right helper?
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def service():
    return WebhookService()


@pytest.fixture
def fake_payload():
    return {"event_id": "evt_test", "event_type": "test", "payload": {}}


def _patch_tor(service):
    """Replace the Tor delivery helper with an AsyncMock that returns success."""
    return patch.object(
        service, "_send_webhook_via_tor",
        new=AsyncMock(return_value=WebhookResult(success=True, response_code=200)),
    )


def _patch_resolve():
    """Stub resolve_and_validate so the clearnet path doesn't try real DNS."""
    return patch(
        "sthrip.services.webhook_service.resolve_and_validate",
        return_value=("https://example.com/", "203.0.113.42"),
    )


def _patch_session():
    """Stub the aiohttp session so the clearnet path returns a fake 200.

    Returning a context-manager-shaped mock is fiddly, so we short-circuit by
    raising an exception that ``_send_webhook`` catches and translates to a
    failure result. Routing tests don't care about success/failure, only that
    the Tor helper was (or was not) called.
    """
    async def _raise(*args, **kwargs):
        raise RuntimeError("test stub: clearnet path reached")

    return patch.object(WebhookService, "_get_session", new=_raise)


class TestWebhookRouting:
    def test_onion_target_uses_socks5_when_enabled(
        self, service, fake_payload, monkeypatch,
    ):
        monkeypatch.setenv("STHRIP_ONION_ENABLED", "true")
        with _patch_tor(service) as tor_mock, _patch_resolve() as resolve_mock:
            result = _run(service._send_webhook(
                "http://abc.onion/webhook", fake_payload, secret="s",
            ))
        assert result.success is True
        # Tor helper called
        tor_mock.assert_awaited_once()
        # Clearnet path NOT entered (resolve_and_validate untouched).
        resolve_mock.assert_not_called()

    def test_clearnet_target_uses_direct_when_enabled(
        self, service, fake_payload, monkeypatch,
    ):
        """Lead Q4: clearnet target with flag on -> NEVER through Tor."""
        monkeypatch.setenv("STHRIP_ONION_ENABLED", "true")
        with _patch_tor(service) as tor_mock, \
             _patch_resolve() as resolve_mock, _patch_session():
            _run(service._send_webhook(
                "https://example.com/webhook", fake_payload, secret="s",
            ))
        # Tor helper was NOT called — clearnet stays clearnet.
        tor_mock.assert_not_awaited()
        resolve_mock.assert_called_once()

    def test_onion_target_uses_direct_when_disabled(
        self, service, fake_payload, monkeypatch,
    ):
        """Flag off + onion target: legacy SSRF path runs (and will reject)."""
        monkeypatch.setenv("STHRIP_ONION_ENABLED", "false")
        with _patch_tor(service) as tor_mock, _patch_resolve() as resolve_mock, \
             _patch_session():
            _run(service._send_webhook(
                "http://abc.onion/webhook", fake_payload, secret="s",
            ))
        tor_mock.assert_not_awaited()
        # Clearnet path was attempted (resolve called).
        resolve_mock.assert_called_once()

    def test_clearnet_target_uses_direct_when_disabled(
        self, service, fake_payload, monkeypatch,
    ):
        monkeypatch.setenv("STHRIP_ONION_ENABLED", "false")
        with _patch_tor(service) as tor_mock, _patch_resolve() as resolve_mock, \
             _patch_session():
            _run(service._send_webhook(
                "https://example.com/webhook", fake_payload, secret="s",
            ))
        tor_mock.assert_not_awaited()
        resolve_mock.assert_called_once()

    def test_onion_target_with_unset_flag_uses_direct(
        self, service, fake_payload, monkeypatch,
    ):
        monkeypatch.delenv("STHRIP_ONION_ENABLED", raising=False)
        with _patch_tor(service) as tor_mock, _patch_resolve(), _patch_session():
            _run(service._send_webhook(
                "http://abc.onion/webhook", fake_payload, secret="s",
            ))
        tor_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tor delivery helper — graceful fallback when aiohttp_socks is missing.
# ---------------------------------------------------------------------------


class TestTorDeliveryGuards:
    def test_tor_helper_returns_failure_when_aiohttp_socks_missing(
        self, service, fake_payload, monkeypatch,
    ):
        """If aiohttp_socks is unavailable in the runtime, the helper must
        return a failure result (not raise) so the retry pipeline still works.
        """
        # Force the dynamic import inside the helper to fail.
        import builtins
        real_import = builtins.__import__

        def fail_aiohttp_socks(name, *args, **kwargs):
            if name == "aiohttp_socks":
                raise ImportError("simulated missing dep")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fail_aiohttp_socks)
        result = _run(service._send_webhook_via_tor(
            "http://abc.onion/x", fake_payload, secret="s",
        ))
        assert result.success is False
        assert "aiohttp_socks" in (result.error or "")
