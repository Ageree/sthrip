"""Integration tests for the spending-policy API endpoints and payment integration."""

import pytest

# Uses shared client fixture from conftest.py (db_engine, db_session_factory, client).

# Valid stagenet address for API tests (95 chars, starts with '5', base58 alphabet)
_VALID_XMR_ADDR = "5" + "a" * 94


@pytest.fixture
def sender_with_balance(client):
    """Register a sender agent, deposit funds, return api_key."""
    r = client.post("/v2/agents/register", json={
        "agent_name": "policy-sender",
        "xmr_address": _VALID_XMR_ADDR,
    })
    assert r.status_code == 201, f"Registration failed: {r.text}"
    api_key = r.json()["api_key"]

    # Deposit funds
    r = client.post(
        "/v2/balance/deposit",
        json={"amount": 100.0},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    return api_key


@pytest.fixture
def recipient_agent(client):
    """Register a recipient agent, return name."""
    r = client.post("/v2/agents/register", json={
        "agent_name": "policy-recipient",
        "xmr_address": _VALID_XMR_ADDR,
    })
    assert r.status_code == 201
    return "policy-recipient"


class TestSpendingPolicyAPI:
    """PUT / GET /v2/me/spending-policy."""

    def test_get_policy_404_when_none(self, client, sender_with_balance):
        r = client.get(
            "/v2/me/spending-policy",
            headers={"Authorization": f"Bearer {sender_with_balance}"},
        )
        assert r.status_code == 404

    def test_set_and_get_spending_policy(self, client, sender_with_balance):
        key = sender_with_balance
        headers = {"Authorization": f"Bearer {key}"}

        # Set policy
        r = client.put(
            "/v2/me/spending-policy",
            json={
                "max_per_tx": "5.0",
                "daily_limit": "50.0",
                "allowed_agents": ["research-*", "data-*"],
                "require_escrow_above": "10.0",
            },
            headers=headers,
        )
        assert r.status_code == 200, f"PUT failed: {r.text}"
        data = r.json()
        assert float(data["max_per_tx"]) == 5.0
        assert float(data["daily_limit"]) == 50.0
        assert data["allowed_agents"] == ["research-*", "data-*"]
        assert float(data["require_escrow_above"]) == 10.0
        assert data["is_active"] is True

        # Read it back
        r = client.get("/v2/me/spending-policy", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert float(data["max_per_tx"]) == 5.0

    def test_update_policy_overwrites(self, client, sender_with_balance):
        key = sender_with_balance
        headers = {"Authorization": f"Bearer {key}"}

        client.put(
            "/v2/me/spending-policy",
            json={"max_per_tx": "5.0"},
            headers=headers,
        )
        r = client.put(
            "/v2/me/spending-policy",
            json={"max_per_tx": "10.0", "daily_limit": "100.0"},
            headers=headers,
        )
        assert r.status_code == 200
        data = r.json()
        assert float(data["max_per_tx"]) == 10.0
        assert float(data["daily_limit"]) == 100.0

    def test_unauthenticated_rejected(self, client):
        r = client.get("/v2/me/spending-policy")
        assert r.status_code == 401


class TestPaymentPolicyIntegration:
    """Payment blocked / allowed by spending policy."""

    def test_payment_blocked_by_max_per_tx(self, client, sender_with_balance, recipient_agent):
        key = sender_with_balance
        headers = {"Authorization": f"Bearer {key}"}

        # Set a low per-tx limit
        r = client.put(
            "/v2/me/spending-policy",
            json={"max_per_tx": "1.0"},
            headers=headers,
        )
        assert r.status_code == 200

        # Try to send more than the limit
        r = client.post(
            "/v2/payments/hub-routing",
            json={"to_agent_name": recipient_agent, "amount": "5.0"},
            headers=headers,
        )
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["error"] == "spending_policy_violation"
        assert detail["field"] == "max_per_tx"

    def test_payment_allowed_under_limit(self, client, sender_with_balance, recipient_agent):
        key = sender_with_balance
        headers = {"Authorization": f"Bearer {key}"}

        # Set a generous per-tx limit
        r = client.put(
            "/v2/me/spending-policy",
            json={"max_per_tx": "50.0"},
            headers=headers,
        )
        assert r.status_code == 200

        # Payment within limit should succeed
        r = client.post(
            "/v2/payments/hub-routing",
            json={"to_agent_name": recipient_agent, "amount": "1.0"},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "confirmed"

    def test_payment_blocked_by_allowed_agents(self, client, sender_with_balance, recipient_agent):
        key = sender_with_balance
        headers = {"Authorization": f"Bearer {key}"}

        # Only allow agents matching "research-*"
        r = client.put(
            "/v2/me/spending-policy",
            json={"allowed_agents": ["research-*"]},
            headers=headers,
        )
        assert r.status_code == 200

        # Recipient doesn't match the pattern
        r = client.post(
            "/v2/payments/hub-routing",
            json={"to_agent_name": recipient_agent, "amount": "1.0"},
            headers=headers,
        )
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["field"] == "allowed_agents"

    def test_payment_blocked_by_blocked_agents(self, client, sender_with_balance, recipient_agent):
        key = sender_with_balance
        headers = {"Authorization": f"Bearer {key}"}

        # Block the recipient by name
        r = client.put(
            "/v2/me/spending-policy",
            json={"blocked_agents": ["policy-*"]},
            headers=headers,
        )
        assert r.status_code == 200

        r = client.post(
            "/v2/payments/hub-routing",
            json={"to_agent_name": recipient_agent, "amount": "1.0"},
            headers=headers,
        )
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["field"] == "blocked_agents"
