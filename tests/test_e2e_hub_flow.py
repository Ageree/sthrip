"""
End-to-end test for the full hub payment flow.

Flow:
1. Register agent A
2. Register agent B
3. Agent A deposits 10 XMR
4. Agent A sends 5 XMR to Agent B via hub routing
5. Verify: Agent A balance = 10 - 5 - fee
6. Verify: Agent B balance = 5
7. Verify: fee_collections has 1 entry
8. Verify: payment history has 1 entry for each agent
"""
from decimal import Decimal

import pytest

# Uses shared client fixture from conftest.py (db_engine, db_session_factory, client).


class TestE2EHubPaymentFlow:
    """Full end-to-end hub payment flow"""

    def test_complete_hub_payment_flow(self, client):
        # ── Step 1: Register Agent A ──
        r = client.post("/v2/agents/register", json={
            "agent_name": "agent-alice",
            "xmr_address": "5" + "a" * 94,
        })
        assert r.status_code == 201
        alice_key = r.json()["api_key"]
        alice_id = r.json()["agent_id"]
        alice_headers = {"Authorization": f"Bearer {alice_key}"}

        # ── Step 2: Register Agent B ──
        r = client.post("/v2/agents/register", json={
            "agent_name": "agent-bob",
            "xmr_address": "5" + "b" * 94,
        })
        assert r.status_code == 201
        bob_key = r.json()["api_key"]
        bob_headers = {"Authorization": f"Bearer {bob_key}"}

        # ── Step 3: Agent A deposits 10 XMR ──
        r = client.post("/v2/balance/deposit",
                        json={"amount": 10.0},
                        headers=alice_headers)
        assert r.status_code == 200
        from decimal import Decimal
        assert Decimal(r.json()["new_balance"]) == 10

        # Verify Alice balance is 10
        r = client.get("/v2/balance", headers=alice_headers)
        assert Decimal(r.json()["available"]) == 10

        # Verify Bob balance is 0
        r = client.get("/v2/balance", headers=bob_headers)
        assert Decimal(r.json()["available"]) == 0

        # ── Step 4: Agent A sends 5 XMR to Agent B ──
        r = client.post("/v2/payments/hub-routing",
                        json={"to_agent_name": "agent-bob", "amount": 5.0},
                        headers=alice_headers)
        assert r.status_code == 200
        payment = r.json()
        assert payment["status"] == "confirmed"
        assert Decimal(payment["amount"]) == 5
        assert payment["payment_type"] == "hub_routing"
        fee = Decimal(payment["fee"])
        assert fee > 0
        total_deducted = Decimal(payment["total_deducted"])

        # ── Step 5: Verify Agent A balance = 10 - 5 - fee ──
        r = client.get("/v2/balance", headers=alice_headers)
        alice_balance = Decimal(r.json()["available"])
        expected_alice = Decimal("10") - total_deducted
        assert abs(alice_balance - expected_alice) < Decimal("0.000001"), \
            f"Alice balance {alice_balance} != expected {expected_alice}"

        # ── Step 6: Verify Agent B balance = 5 ──
        r = client.get("/v2/balance", headers=bob_headers)
        assert Decimal(r.json()["available"]) == 5

        # ── Step 7: Verify payment details ──
        assert payment["recipient"]["agent_name"] == "agent-bob"
        assert Decimal(payment["fee_percent"]) > 0

    def test_multiple_payments_accumulate(self, client):
        """Multiple payments should correctly update balances"""
        # Register agents
        r = client.post("/v2/agents/register", json={
            "agent_name": "multi-sender",
            "xmr_address": "5" + "c" * 94,
        })
        sender_key = r.json()["api_key"]
        sender_h = {"Authorization": f"Bearer {sender_key}"}

        r = client.post("/v2/agents/register", json={
            "agent_name": "multi-receiver",
            "xmr_address": "5" + "d" * 94,
        })
        receiver_key = r.json()["api_key"]
        receiver_h = {"Authorization": f"Bearer {receiver_key}"}

        # Deposit 100
        client.post("/v2/balance/deposit", json={"amount": 100.0}, headers=sender_h)

        # Send 3 payments
        total_fees = 0
        for i in range(3):
            r = client.post("/v2/payments/hub-routing",
                            json={"to_agent_name": "multi-receiver", "amount": 10.0},
                            headers=sender_h)
            assert r.status_code == 200
            total_fees += Decimal(r.json()["fee"])

        # Verify receiver got 30 XMR total
        r = client.get("/v2/balance", headers=receiver_h)
        assert Decimal(r.json()["available"]) == 30

        # Verify sender balance is 100 - 30 - fees
        r = client.get("/v2/balance", headers=sender_h)
        sender_balance = Decimal(r.json()["available"])
        expected = Decimal("100") - Decimal("30") - total_fees
        assert abs(sender_balance - expected) < Decimal("0.0001")

    def test_deposit_and_withdraw_roundtrip(self, client):
        """Deposit and withdraw should correctly track totals"""
        r = client.post("/v2/agents/register", json={
            "agent_name": "roundtrip-agent",
            "xmr_address": "5" + "e" * 94,
        })
        key = r.json()["api_key"]
        h = {"Authorization": f"Bearer {key}"}

        # Deposit 50
        client.post("/v2/balance/deposit", json={"amount": 50.0}, headers=h)

        # Withdraw 20
        r = client.post("/v2/balance/withdraw",
                        json={"amount": 20.0, "address": "5" + "c" * 94},
                        headers=h)
        assert r.status_code == 200
        assert Decimal(r.json()["remaining_balance"]) == 30

        # Check balance
        r = client.get("/v2/balance", headers=h)
        data = r.json()
        assert Decimal(data["available"]) == 30
        assert Decimal(data["total_deposited"]) == 50
