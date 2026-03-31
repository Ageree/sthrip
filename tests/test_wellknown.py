"""Tests for the /.well-known/agent-payments.json discovery endpoint."""


# Uses shared client fixture from conftest.py (db_engine, db_session_factory, client).

_EXPECTED_FIELDS = {
    "service",
    "version",
    "description",
    "api_url",
    "docs_url",
    "endpoints",
    "supported_tokens",
    "fee_percent",
    "min_confirmations",
    "install",
}

_EXPECTED_ENDPOINTS = {
    "register",
    "payments",
    "balance",
    "deposit",
    "agents",
}


class TestAgentPaymentsDiscovery:
    """GET /.well-known/agent-payments.json — public discovery document."""

    def test_returns_200(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.status_code == 200

    def test_content_type_is_json(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        assert "application/json" in resp.headers["content-type"]

    def test_all_top_level_fields_present(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        data = resp.json()
        missing = _EXPECTED_FIELDS - set(data.keys())
        assert not missing, f"Missing fields: {missing}"

    def test_all_endpoint_keys_present(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        endpoints = resp.json()["endpoints"]
        missing = _EXPECTED_ENDPOINTS - set(endpoints.keys())
        assert not missing, f"Missing endpoint keys: {missing}"

    def test_service_name(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.json()["service"] == "sthrip"

    def test_version(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.json()["version"] == "3.0.0"

    def test_supported_tokens(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.json()["supported_tokens"] == ["XMR"]

    def test_fee_percent(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.json()["fee_percent"] == "1"

    def test_min_confirmations(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.json()["min_confirmations"] == 10

    def test_api_url(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.json()["api_url"] == "https://sthrip-api-production.up.railway.app"

    def test_docs_url(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.json()["docs_url"] == "https://sthrip-api-production.up.railway.app/docs"

    def test_install_command(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.json()["install"] == "pip install sthrip"

    def test_endpoint_paths(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        endpoints = resp.json()["endpoints"]
        assert endpoints["register"] == "/v2/agents/register"
        assert endpoints["payments"] == "/v2/payments/hub-routing"
        assert endpoints["balance"] == "/v2/balance"
        assert endpoints["deposit"] == "/v2/balance/deposit"
        assert endpoints["agents"] == "/v2/agents"

    def test_no_auth_required(self, client):
        """The endpoint must be accessible without any Authorization header."""
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.status_code == 200

    def test_description(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        assert resp.json()["description"] == "Anonymous payments for AI agents"
