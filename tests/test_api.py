"""Integration tests for the Sthrip API"""
import os
import pytest
from unittest.mock import patch, MagicMock

# Uses shared client fixture from conftest.py (db_engine, db_session_factory, client).

# Valid stagenet address for API tests (95 chars, starts with '5', base58 alphabet)
_VALID_XMR_ADDR = "5" + "a" * 94


@pytest.fixture
def registered_agent(client):
    """Register an agent and return (api_key, agent_name)"""
    r = client.post("/v2/agents/register", json={
        "agent_name": "test-sender",
        "xmr_address": _VALID_XMR_ADDR
    })
    assert r.status_code == 201, f"Registration failed: {r.text}"
    return r.json()["api_key"], "test-sender"


@pytest.fixture
def two_agents(client):
    """Register sender and recipient, return (sender_key, recipient_key)"""
    r1 = client.post("/v2/agents/register", json={
        "agent_name": "sender-agent",
        "xmr_address": _VALID_XMR_ADDR
    })
    assert r1.status_code == 201
    sender_key = r1.json()["api_key"]

    r2 = client.post("/v2/agents/register", json={
        "agent_name": "receiver-agent",
        "xmr_address": _VALID_XMR_ADDR
    })
    assert r2.status_code == 201
    receiver_key = r2.json()["api_key"]

    return sender_key, receiver_key


class TestPublicEndpoints:
    def test_root(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Sthrip" in r.json()["name"]

    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"


class TestRegistration:
    def test_register_agent(self, client):
        r = client.post("/v2/agents/register", json={
            "agent_name": "new-agent",
            "xmr_address": _VALID_XMR_ADDR
        })
        assert r.status_code == 201
        data = r.json()
        assert "api_key" in data
        assert data["api_key"].startswith("sk_")
        assert data["agent_name"] == "new-agent"

    def test_register_invalid_name(self, client):
        r = client.post("/v2/agents/register", json={
            "agent_name": "bad name!",
            "xmr_address": "addr"
        })
        assert r.status_code == 422  # Pydantic validation

    def test_register_short_name(self, client):
        r = client.post("/v2/agents/register", json={
            "agent_name": "ab",
            "xmr_address": "addr"
        })
        assert r.status_code == 422


class TestAuthentication:
    def test_unauthenticated_request(self, client):
        r = client.get("/v2/me")
        assert r.status_code == 401

    def test_invalid_api_key(self, client):
        r = client.get("/v2/me", headers={"Authorization": "Bearer invalid_key"})
        assert r.status_code == 401

    def test_valid_api_key(self, client, registered_agent):
        key, name = registered_agent
        r = client.get("/v2/me", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
        assert r.json()["agent_name"] == name


class TestBalance:
    def test_initial_balance_zero(self, client, registered_agent):
        key, _ = registered_agent
        r = client.get("/v2/balance", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
        from decimal import Decimal
        assert Decimal(r.json()["available"]) == 0

    def test_deposit(self, client, registered_agent):
        key, _ = registered_agent
        r = client.post("/v2/balance/deposit",
                        json={"amount": 10.0},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
        from decimal import Decimal
        assert Decimal(r.json()["new_balance"]) == 10

    def test_deposit_invalid_amount(self, client, registered_agent):
        key, _ = registered_agent
        r = client.post("/v2/balance/deposit",
                        json={"amount": -5.0},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 422

    def test_withdraw(self, client, registered_agent):
        key, _ = registered_agent
        # Deposit first
        client.post("/v2/balance/deposit",
                    json={"amount": 10.0},
                    headers={"Authorization": f"Bearer {key}"})
        # Withdraw
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 3.0, "address": "5" + "a" * 94},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
        from decimal import Decimal
        assert Decimal(r.json()["remaining_balance"]) == 7

    def test_withdraw_insufficient(self, client, registered_agent):
        key, _ = registered_agent
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 100.0, "address": "5" + "a" * 94},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 400
        assert "Insufficient" in r.json()["detail"]


class TestHubRouting:
    def test_hub_payment_success(self, client, two_agents):
        sender_key, receiver_key = two_agents
        # Deposit
        client.post("/v2/balance/deposit",
                    json={"amount": 10.0},
                    headers={"Authorization": f"Bearer {sender_key}"})
        # Pay
        r = client.post("/v2/payments/hub-routing",
                        json={"to_agent_name": "receiver-agent", "amount": 5.0},
                        headers={"Authorization": f"Bearer {sender_key}"})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "confirmed"
        from decimal import Decimal
        assert Decimal(data["amount"]) == 5
        assert Decimal(data["fee"]) > 0

    def test_hub_payment_insufficient_balance(self, client, two_agents):
        sender_key, _ = two_agents
        r = client.post("/v2/payments/hub-routing",
                        json={"to_agent_name": "receiver-agent", "amount": 100.0},
                        headers={"Authorization": f"Bearer {sender_key}"})
        assert r.status_code == 400
        assert "Insufficient" in r.json()["detail"]

    def test_hub_payment_unknown_recipient(self, client, registered_agent):
        key, _ = registered_agent
        client.post("/v2/balance/deposit",
                    json={"amount": 10.0},
                    headers={"Authorization": f"Bearer {key}"})
        r = client.post("/v2/payments/hub-routing",
                        json={"to_agent_name": "nonexistent-agent", "amount": 1.0},
                        headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 404

    def test_hub_payment_balances_correct(self, client, two_agents):
        sender_key, receiver_key = two_agents
        # Deposit 10 to sender
        client.post("/v2/balance/deposit",
                    json={"amount": 10.0},
                    headers={"Authorization": f"Bearer {sender_key}"})
        # Send 5
        r = client.post("/v2/payments/hub-routing",
                        json={"to_agent_name": "receiver-agent", "amount": 5.0},
                        headers={"Authorization": f"Bearer {sender_key}"})
        from decimal import Decimal
        fee = Decimal(r.json()["fee"])

        # Check sender balance
        r = client.get("/v2/balance", headers={"Authorization": f"Bearer {sender_key}"})
        sender_balance = Decimal(r.json()["available"])
        assert abs(sender_balance - (Decimal("10") - Decimal("5") - fee)) < Decimal("0.0001")

        # Check receiver balance
        r = client.get("/v2/balance", headers={"Authorization": f"Bearer {receiver_key}"})
        assert Decimal(r.json()["available"]) == 5


class TestDisabledEndpoints:
    def test_p2p_send_disabled(self, client):
        r = client.post("/v2/payments/send", json={})
        assert r.status_code == 501

    def test_escrow_requires_auth(self, client):
        """Escrow create endpoint exists but requires authentication."""
        r = client.post("/v2/escrow", json={})
        assert r.status_code == 401


class TestIdempotencyKeyMaxLength:
    """HIGH-9: idempotency key headers must reject values >255 chars."""

    def test_hub_routing_rejects_long_idempotency_key(self, client, registered_agent):
        key, _ = registered_agent
        long_key = "k" * 256
        r = client.post(
            "/v2/payments/hub-routing",
            json={"to_agent_name": "nobody", "amount": 1.0},
            headers={
                "Authorization": f"Bearer {key}",
                "idempotency-key": long_key,
            },
        )
        assert r.status_code == 422

    def test_deposit_rejects_long_idempotency_key(self, client, registered_agent):
        key, _ = registered_agent
        long_key = "k" * 256
        r = client.post(
            "/v2/balance/deposit",
            json={"amount": 1.0},
            headers={
                "Authorization": f"Bearer {key}",
                "idempotency-key": long_key,
            },
        )
        assert r.status_code == 422

    def test_withdraw_rejects_long_idempotency_key(self, client, registered_agent):
        key, _ = registered_agent
        long_key = "k" * 256
        r = client.post(
            "/v2/balance/withdraw",
            json={"amount": 1.0, "address": "5" + "a" * 94},
            headers={
                "Authorization": f"Bearer {key}",
                "idempotency-key": long_key,
            },
        )
        assert r.status_code == 422

    def test_hub_routing_accepts_255_char_key(self, client, two_agents):
        sender_key, _ = two_agents
        # Deposit first
        client.post(
            "/v2/balance/deposit",
            json={"amount": 100.0},
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        ok_key = "k" * 255
        r = client.post(
            "/v2/payments/hub-routing",
            json={"to_agent_name": "receiver-agent", "amount": 1.0},
            headers={
                "Authorization": f"Bearer {sender_key}",
                "idempotency-key": ok_key,
            },
        )
        # Should not be 422 (may be 200 or other business error, but not validation error)
        assert r.status_code != 422
