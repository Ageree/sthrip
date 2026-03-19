"""Tests for marketplace API endpoints: registration with marketplace fields,
agent discovery filtering, marketplace endpoint, profile updates, and /v2/me
marketplace field inclusion.

Uses the shared client fixture from conftest.py (db_engine, db_session_factory, client).
"""

import pytest

# Valid stagenet address for API tests (95 chars, starts with '5', base58 alphabet)
_VALID_XMR_ADDR = "5" + "a" * 94


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth(api_key: str) -> dict:
    """Return auth headers for the given API key."""
    return {"Authorization": f"Bearer {api_key}"}


def _register(client, name: str, **kwargs) -> str:
    """Register an agent and return its API key."""
    payload = {"agent_name": name, "xmr_address": _VALID_XMR_ADDR, **kwargs}
    r = client.post("/v2/agents/register", json=payload)
    assert r.status_code == 201, f"Registration of '{name}' failed: {r.text}"
    return r.json()["api_key"]


# ---------------------------------------------------------------------------
# 1. Registration with marketplace fields
# ---------------------------------------------------------------------------

class TestRegistrationMarketplaceFields:
    """Register agents with marketplace-specific fields."""

    def test_register_with_capabilities_and_pricing(self, client):
        """Capabilities and pricing provided at registration are stored."""
        key = _register(
            client,
            "mkt-agent-1",
            capabilities=["translation", "code-review"],
            pricing={"translation": "0.01 XMR/1k words", "code-review": "0.05 XMR/PR"},
            description="Polyglot code reviewer",
            accepts_escrow=True,
        )

        r = client.get(f"/v2/agents/mkt-agent-1")
        assert r.status_code == 200
        data = r.json()
        assert data["capabilities"] == ["translation", "code-review"]
        assert data["pricing"]["translation"] == "0.01 XMR/1k words"
        assert data["description"] == "Polyglot code reviewer"
        assert data["accepts_escrow"] is True

    def test_register_without_marketplace_fields_uses_defaults(self, client):
        """Omitting marketplace fields gives sensible defaults."""
        _register(client, "mkt-default-agent")

        r = client.get("/v2/agents/mkt-default-agent")
        assert r.status_code == 200
        data = r.json()
        assert data["capabilities"] == []
        assert data["pricing"] == {}
        assert data["description"] is None
        assert data["accepts_escrow"] is True

    def test_register_with_accepts_escrow_false(self, client):
        """Agent can opt out of escrow at registration."""
        _register(client, "no-escrow-agent", accepts_escrow=False)

        r = client.get("/v2/agents/no-escrow-agent")
        assert r.status_code == 200
        assert r.json()["accepts_escrow"] is False

    def test_capabilities_max_items_rejected(self, client):
        """More than 20 capabilities should be rejected (422)."""
        caps = [f"cap-{i}" for i in range(21)]
        r = client.post("/v2/agents/register", json={
            "agent_name": "too-many-caps",
            "xmr_address": _VALID_XMR_ADDR,
            "capabilities": caps,
        })
        assert r.status_code == 422

    def test_capabilities_long_item_rejected(self, client):
        """A capability longer than 50 characters should be rejected."""
        r = client.post("/v2/agents/register", json={
            "agent_name": "long-cap-agent",
            "xmr_address": _VALID_XMR_ADDR,
            "capabilities": ["x" * 51],
        })
        assert r.status_code == 422

    def test_capabilities_empty_item_rejected(self, client):
        """An empty-string capability should be rejected."""
        r = client.post("/v2/agents/register", json={
            "agent_name": "empty-cap-agent",
            "xmr_address": _VALID_XMR_ADDR,
            "capabilities": [""],
        })
        assert r.status_code == 422

    def test_pricing_max_entries_rejected(self, client):
        """More than 20 pricing entries should be rejected."""
        pricing = {f"svc-{i}": f"price-{i}" for i in range(21)}
        r = client.post("/v2/agents/register", json={
            "agent_name": "too-many-prices",
            "xmr_address": _VALID_XMR_ADDR,
            "pricing": pricing,
        })
        assert r.status_code == 422

    def test_pricing_key_too_long_rejected(self, client):
        """A pricing key longer than 50 characters should be rejected."""
        r = client.post("/v2/agents/register", json={
            "agent_name": "long-price-key",
            "xmr_address": _VALID_XMR_ADDR,
            "pricing": {"k" * 51: "value"},
        })
        assert r.status_code == 422

    def test_pricing_value_too_long_rejected(self, client):
        """A pricing value longer than 100 characters should be rejected."""
        r = client.post("/v2/agents/register", json={
            "agent_name": "long-price-val",
            "xmr_address": _VALID_XMR_ADDR,
            "pricing": {"key": "v" * 101},
        })
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# 2. Agent discovery filtering (/v2/agents)
# ---------------------------------------------------------------------------

class TestAgentDiscoveryFiltering:
    """GET /v2/agents with capability and accepts_escrow filters."""

    @pytest.fixture
    def three_agents(self, client):
        """Register 3 agents with different capabilities and escrow preferences."""
        keys = {}
        keys["translator"] = _register(
            client, "agent-translator",
            capabilities=["translation", "summarization"],
            accepts_escrow=True,
        )
        keys["coder"] = _register(
            client, "agent-coder",
            capabilities=["code-review", "debugging"],
            accepts_escrow=True,
        )
        keys["no-escrow"] = _register(
            client, "agent-no-escrow",
            capabilities=["translation"],
            accepts_escrow=False,
        )
        return keys

    def test_filter_by_capability(self, client, three_agents):
        """GET /v2/agents?capability=translation returns only matching agents."""
        r = client.get("/v2/agents?capability=translation")
        assert r.status_code == 200
        data = r.json()
        names = [item["agent_name"] for item in data["items"]]
        assert "agent-translator" in names
        assert "agent-no-escrow" in names
        assert "agent-coder" not in names

    def test_filter_by_accepts_escrow_true(self, client, three_agents):
        """GET /v2/agents?accepts_escrow=true excludes agents that opt out."""
        r = client.get("/v2/agents?accepts_escrow=true")
        assert r.status_code == 200
        data = r.json()
        names = [item["agent_name"] for item in data["items"]]
        assert "agent-no-escrow" not in names
        assert "agent-translator" in names

    def test_filter_by_accepts_escrow_false(self, client, three_agents):
        """GET /v2/agents?accepts_escrow=false returns only opt-out agents."""
        r = client.get("/v2/agents?accepts_escrow=false")
        assert r.status_code == 200
        data = r.json()
        names = [item["agent_name"] for item in data["items"]]
        assert "agent-no-escrow" in names
        assert "agent-translator" not in names

    def test_filter_combined_capability_and_escrow(self, client, three_agents):
        """capability + accepts_escrow filters compose correctly."""
        r = client.get("/v2/agents?capability=translation&accepts_escrow=true")
        assert r.status_code == 200
        data = r.json()
        names = [item["agent_name"] for item in data["items"]]
        assert "agent-translator" in names
        assert "agent-no-escrow" not in names
        assert "agent-coder" not in names


# ---------------------------------------------------------------------------
# 3. Marketplace endpoint (/v2/agents/marketplace)
# ---------------------------------------------------------------------------

class TestMarketplaceEndpoint:
    """GET /v2/agents/marketplace with marketplace-specific response shape."""

    @pytest.fixture
    def marketplace_agents(self, client):
        """Register agents for marketplace testing."""
        _register(
            client, "mkt-translate",
            capabilities=["translation"],
            pricing={"translation": "0.01 XMR"},
            description="Translation service",
        )
        _register(
            client, "mkt-review",
            capabilities=["code-review"],
            pricing={"code-review": "0.05 XMR"},
            description="Code review service",
        )

    def test_marketplace_returns_agents(self, client, marketplace_agents):
        """GET /v2/agents/marketplace returns agents with marketplace fields."""
        r = client.get("/v2/agents/marketplace")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 2
        item = data["items"][0]
        # Marketplace response includes these fields
        assert "agent_name" in item
        assert "capabilities" in item
        assert "pricing" in item
        assert "description" in item
        assert "accepts_escrow" in item
        assert "tier" in item
        assert "trust_score" in item

    def test_marketplace_filter_by_capability(self, client, marketplace_agents):
        """GET /v2/agents/marketplace?capability=translation filters correctly."""
        r = client.get("/v2/agents/marketplace?capability=translation")
        assert r.status_code == 200
        data = r.json()
        names = [item["agent_name"] for item in data["items"]]
        assert "mkt-translate" in names
        assert "mkt-review" not in names

    def test_marketplace_filter_by_accepts_escrow(self, client, marketplace_agents):
        """Marketplace supports accepts_escrow filter."""
        r = client.get("/v2/agents/marketplace?accepts_escrow=true")
        assert r.status_code == 200
        assert r.json()["total"] >= 2

    def test_marketplace_pagination(self, client, marketplace_agents):
        """Marketplace supports limit and offset."""
        r = client.get("/v2/agents/marketplace?limit=1&offset=0")
        assert r.status_code == 200
        data = r.json()
        assert len(data["items"]) == 1
        assert data["limit"] == 1
        assert data["offset"] == 0


# ---------------------------------------------------------------------------
# 4. Profile update (PATCH /v2/me/settings)
# ---------------------------------------------------------------------------

class TestProfileUpdate:
    """PATCH /v2/me/settings with marketplace fields."""

    @pytest.fixture
    def agent_key(self, client):
        """Register an agent and return its API key."""
        return _register(client, "updatable-agent")

    def test_update_capabilities(self, client, agent_key):
        """PATCH capabilities via settings endpoint."""
        r = client.patch(
            "/v2/me/settings",
            json={"capabilities": ["newcap-1", "newcap-2"]},
            headers=_auth(agent_key),
        )
        assert r.status_code == 200
        assert "capabilities" in r.json()["updated"]

        # Verify via public profile
        r2 = client.get("/v2/agents/updatable-agent")
        assert r2.status_code == 200
        assert r2.json()["capabilities"] == ["newcap-1", "newcap-2"]

    def test_update_description(self, client, agent_key):
        """PATCH description via settings endpoint."""
        r = client.patch(
            "/v2/me/settings",
            json={"description": "Updated description"},
            headers=_auth(agent_key),
        )
        assert r.status_code == 200
        assert "description" in r.json()["updated"]

    def test_update_pricing(self, client, agent_key):
        """PATCH pricing via settings endpoint."""
        r = client.patch(
            "/v2/me/settings",
            json={"pricing": {"task": "0.1 XMR"}},
            headers=_auth(agent_key),
        )
        assert r.status_code == 200
        assert "pricing" in r.json()["updated"]

    def test_update_accepts_escrow(self, client, agent_key):
        """PATCH accepts_escrow via settings endpoint."""
        r = client.patch(
            "/v2/me/settings",
            json={"accepts_escrow": False},
            headers=_auth(agent_key),
        )
        assert r.status_code == 200
        assert "accepts_escrow" in r.json()["updated"]

        r2 = client.get("/v2/agents/updatable-agent")
        assert r2.json()["accepts_escrow"] is False

    def test_update_invalid_capabilities_rejected(self, client, agent_key):
        """PATCH with >20 capabilities returns 422."""
        caps = [f"c-{i}" for i in range(21)]
        r = client.patch(
            "/v2/me/settings",
            json={"capabilities": caps},
            headers=_auth(agent_key),
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# 5. Profile response includes marketplace fields (GET /v2/me)
# ---------------------------------------------------------------------------

class TestMeIncludesMarketplaceFields:
    """GET /v2/me should include marketplace fields for the authenticated agent."""

    def test_me_includes_capabilities(self, client):
        """GET /v2/me includes capabilities field."""
        key = _register(
            client, "me-agent",
            capabilities=["testing"],
            description="Test agent",
            accepts_escrow=False,
        )
        r = client.get("/v2/me", headers=_auth(key))
        assert r.status_code == 200
        data = r.json()
        # These fields should be present in the /v2/me response
        assert "capabilities" in data
        assert "description" in data
        assert "accepts_escrow" in data

    def test_me_marketplace_fields_match_registration(self, client):
        """Marketplace fields in /v2/me match what was provided at registration."""
        key = _register(
            client, "me-match-agent",
            capabilities=["alpha", "beta"],
            pricing={"alpha": "1 XMR"},
            description="My description",
            accepts_escrow=True,
        )
        r = client.get("/v2/me", headers=_auth(key))
        assert r.status_code == 200
        data = r.json()
        assert data["capabilities"] == ["alpha", "beta"]
        assert data["pricing"] == {"alpha": "1 XMR"}
        assert data["description"] == "My description"
        assert data["accepts_escrow"] is True
