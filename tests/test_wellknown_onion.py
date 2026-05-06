"""Sprint 6: ``.onion`` discovery surfacing in /.well-known/agent-payments.json.

The endpoint must include an ``onion_endpoint`` field iff BOTH:
- ``STHRIP_ONION_ENABLED`` env var is truthy ("1", "true", "yes", "on")
- ``STHRIP_ONION_ENDPOINT`` env var is set to a non-empty string

When either condition fails the field MUST be absent (no leak that an onion
sidecar exists at all). Tests use the shared ``client`` fixture from conftest.
"""
from __future__ import annotations

import pytest


_ONION_ADDR = (
    "exampleexampleexampleexampleexampleexampleexampleexamplead.onion"
)


@pytest.fixture
def onion_enabled(monkeypatch):
    """Set both env vars so the endpoint should expose ``onion_endpoint``."""
    monkeypatch.setenv("STHRIP_ONION_ENABLED", "true")
    monkeypatch.setenv("STHRIP_ONION_ENDPOINT", _ONION_ADDR)


@pytest.fixture
def onion_disabled(monkeypatch):
    """Flag explicitly off — even if endpoint env is present, must be omitted."""
    monkeypatch.setenv("STHRIP_ONION_ENABLED", "false")
    monkeypatch.setenv("STHRIP_ONION_ENDPOINT", _ONION_ADDR)


@pytest.fixture
def onion_env_unset(monkeypatch):
    """Flag on but endpoint env missing — must NOT advertise."""
    monkeypatch.setenv("STHRIP_ONION_ENABLED", "true")
    monkeypatch.delenv("STHRIP_ONION_ENDPOINT", raising=False)


class TestOnionDiscovery:
    def test_onion_endpoint_published_when_enabled(self, client, onion_enabled):
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("onion_endpoint") == _ONION_ADDR

    def test_onion_endpoint_excluded_when_disabled(self, client, onion_disabled):
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "onion_endpoint" not in data

    def test_onion_endpoint_excluded_when_env_unset(
        self, client, onion_env_unset,
    ):
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "onion_endpoint" not in data

    def test_onion_field_absent_in_default_state(self, client, monkeypatch):
        """No env vars set at all — default behaviour is unchanged."""
        monkeypatch.delenv("STHRIP_ONION_ENABLED", raising=False)
        monkeypatch.delenv("STHRIP_ONION_ENDPOINT", raising=False)
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.status_code == 200
        assert "onion_endpoint" not in resp.json()

    def test_existing_payload_unchanged_when_disabled(self, client, monkeypatch):
        """Sanity: turning the flag off must not regress any existing field."""
        monkeypatch.delenv("STHRIP_ONION_ENABLED", raising=False)
        monkeypatch.delenv("STHRIP_ONION_ENDPOINT", raising=False)
        resp = client.get("/.well-known/agent-payments.json")
        data = resp.json()
        # Spot-check one stable field we know was present pre-Sprint-6.
        assert data["service"] == "sthrip"
        assert data["version"] == "4.0.0"

    @pytest.mark.parametrize("flag_value", ["1", "true", "yes", "on", "TRUE", "Yes"])
    def test_truthy_variants_all_enable(self, client, monkeypatch, flag_value):
        monkeypatch.setenv("STHRIP_ONION_ENABLED", flag_value)
        monkeypatch.setenv("STHRIP_ONION_ENDPOINT", _ONION_ADDR)
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.json().get("onion_endpoint") == _ONION_ADDR

    @pytest.mark.parametrize("flag_value", ["0", "false", "no", "off", "", "junk"])
    def test_non_truthy_variants_disable(self, client, monkeypatch, flag_value):
        monkeypatch.setenv("STHRIP_ONION_ENABLED", flag_value)
        monkeypatch.setenv("STHRIP_ONION_ENDPOINT", _ONION_ADDR)
        resp = client.get("/.well-known/agent-payments.json")
        assert "onion_endpoint" not in resp.json()
