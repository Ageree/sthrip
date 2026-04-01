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
    "escrow",
    "spending_policy",
    "messages",
    "reputation",
    "webhooks",
    # Phase 3a — Marketplace v2
    "sla_templates",
    "sla_contracts",
    "reviews",
    "matchmaking",
    "marketplace_discover",
    # Phase 3b — Payment Scaling
    "payment_channels",
    "recurring_payments",
    "payment_streams",
    # Phase 3c — Multi-Currency
    "cross_chain_swaps",
    "virtual_stablecoins",
    "currency_conversion",
}

_EXPECTED_CAPABILITIES = {
    "hub-routing",
    "escrow",
    "multisig-escrow",
    "webhooks",
    "mcp-server",
    "spending-policies",
    "encrypted-messaging",
    "zk-reputation",
    "pow-registration",
    # Phase 3a
    "sla-contracts",
    "zk-reviews",
    "matchmaking",
    # Phase 3b
    "payment-channels",
    "recurring-payments",
    "payment-streaming",
    # Phase 3c
    "cross-chain-swaps",
    "virtual-stablecoins",
    "currency-conversion",
}

_PHASE3_FEATURE_SECTIONS = {
    "sla_contracts",
    "zk_reviews",
    "matchmaking",
    "payment_channels",
    "recurring_payments",
    "payment_streaming",
    "cross_chain_swaps",
    "virtual_stablecoins",
    "currency_conversion",
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
        assert resp.json()["version"] == "4.0.0"

    def test_supported_tokens(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        tokens = resp.json()["supported_tokens"]
        assert "XMR" in tokens
        assert "BTC" in tokens
        assert "ETH" in tokens
        assert "xUSD" in tokens
        assert "xEUR" in tokens

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

    def test_all_capabilities_present(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        caps = set(resp.json()["capabilities"])
        missing = _EXPECTED_CAPABILITIES - caps
        assert not missing, f"Missing capabilities: {missing}"

    def test_phase3_feature_sections_present(self, client):
        """Every Phase 3 feature must have a top-level section with supported=True."""
        resp = client.get("/.well-known/agent-payments.json")
        data = resp.json()
        for section in _PHASE3_FEATURE_SECTIONS:
            assert section in data, f"Missing feature section: {section}"
            assert data[section]["supported"] is True, (
                f"{section} should be supported"
            )

    # -- Phase 3a: Marketplace v2 --

    def test_sla_contracts_endpoints(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        sla = resp.json()["sla_contracts"]
        eps = sla["endpoints"]
        assert "create_contract" in eps
        assert "accept_contract" in eps
        assert "terminate_contract" in eps
        assert "list_templates" in eps

    def test_zk_reviews_endpoints(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        reviews = resp.json()["zk_reviews"]
        eps = reviews["endpoints"]
        assert "submit_review" in eps
        assert "verify_review" in eps

    def test_matchmaking_filters(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        mm = resp.json()["matchmaking"]
        assert "capability" in mm["filters"]
        assert "min_rating" in mm["filters"]
        assert "max_price" in mm["filters"]
        assert "sla_tier" in mm["filters"]

    # -- Phase 3b: Payment Scaling --

    def test_payment_channels_signing(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        pc = resp.json()["payment_channels"]
        assert pc["signing"] == "Ed25519"
        eps = pc["endpoints"]
        assert "open" in eps
        assert "close" in eps
        assert "dispute" in eps

    def test_recurring_payments_intervals(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        rp = resp.json()["recurring_payments"]
        assert set(rp["intervals"]) == {"hourly", "daily", "weekly", "monthly"}
        eps = rp["endpoints"]
        assert "create" in eps
        assert "cancel" in eps
        assert "pause" in eps

    def test_payment_streaming_rate(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        ps = resp.json()["payment_streaming"]
        assert ps["min_rate_per_second"] == "0.000001"
        eps = ps["endpoints"]
        assert "start" in eps
        assert "stop" in eps
        assert "adjust_rate" in eps

    # -- Phase 3c: Multi-Currency --

    def test_cross_chain_swaps_pairs(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        swaps = resp.json()["cross_chain_swaps"]
        assert swaps["mechanism"] == "HTLC (Hash Time-Locked Contracts)"
        assert "BTC/XMR" in swaps["supported_pairs"]
        assert "ETH/XMR" in swaps["supported_pairs"]

    def test_virtual_stablecoins(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        vs = resp.json()["virtual_stablecoins"]
        assert "xUSD" in vs["coins"]
        assert "xEUR" in vs["coins"]
        eps = vs["endpoints"]
        assert "mint" in eps
        assert "burn" in eps
        assert "rates" in eps

    def test_currency_conversion_endpoints(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        cc = resp.json()["currency_conversion"]
        eps = cc["endpoints"]
        assert "quote" in eps
        assert "execute" in eps

    def test_new_fee_entries(self, client):
        resp = client.get("/.well-known/agent-payments.json")
        fees = resp.json()["fees"]
        assert "payment_channels" in fees
        assert "cross_chain_swap" in fees
        assert "currency_conversion" in fees
