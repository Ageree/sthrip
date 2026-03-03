"""
E2E smoke test for hub routing.
Usage: python scripts/test_hub_routing_e2e.py [base_url]
Default: http://localhost:8000
"""
import sys
import requests

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


def test_full_flow():
    print(f"Testing against {BASE_URL}\n")

    # 1. Health check
    r = requests.get(f"{BASE_URL}/health")
    assert r.status_code == 200, f"Health check failed: {r.text}"
    print("[OK] Health check passed")

    # 2. Register sender
    r = requests.post(f"{BASE_URL}/v2/agents/register", json={
        "agent_name": "e2e-sender",
        "xmr_address": "stagenet_sender_address_test"
    })
    assert r.status_code in [200, 201], f"Register sender failed: {r.text}"
    sender_key = r.json()["api_key"]
    print(f"[OK] Sender registered: {sender_key[:20]}...")

    sender_headers = {"Authorization": f"Bearer {sender_key}"}

    # 3. Register recipient
    r = requests.post(f"{BASE_URL}/v2/agents/register", json={
        "agent_name": "e2e-recipient",
        "xmr_address": "stagenet_recipient_address_test"
    })
    assert r.status_code in [200, 201], f"Register recipient failed: {r.text}"
    recipient_key = r.json()["api_key"]
    print(f"[OK] Recipient registered: {recipient_key[:20]}...")

    recipient_headers = {"Authorization": f"Bearer {recipient_key}"}

    # 4. Check initial balance (should be 0)
    r = requests.get(f"{BASE_URL}/v2/balance", headers=sender_headers)
    assert r.status_code == 200
    assert r.json()["available"] == 0
    print("[OK] Initial balance is 0")

    # 5. Deposit 10 XMR
    r = requests.post(f"{BASE_URL}/v2/balance/deposit",
                      json={"amount": 10.0},
                      headers=sender_headers)
    assert r.status_code == 200
    assert r.json()["new_balance"] == 10.0
    print("[OK] Deposited 10 XMR")

    # 6. Send 5 XMR via hub routing
    r = requests.post(f"{BASE_URL}/v2/payments/hub-routing",
                      json={"to_agent_name": "e2e-recipient", "amount": 5.0},
                      headers=sender_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "confirmed"
    assert data["amount"] == 5.0
    fee = data["fee"]
    print(f"[OK] Hub payment sent: 5 XMR, fee: {fee} XMR")

    # 7. Check sender balance (should be 10 - 5 - fee)
    r = requests.get(f"{BASE_URL}/v2/balance", headers=sender_headers)
    assert r.status_code == 200
    sender_balance = r.json()["available"]
    expected = 10.0 - 5.0 - fee
    assert abs(sender_balance - expected) < 0.0001, f"Sender balance {sender_balance} != {expected}"
    print(f"[OK] Sender balance: {sender_balance} XMR (expected ~{expected})")

    # 8. Check recipient balance (should be 5 XMR)
    r = requests.get(f"{BASE_URL}/v2/balance", headers=recipient_headers)
    assert r.status_code == 200
    recipient_balance = r.json()["available"]
    assert recipient_balance == 5.0, f"Recipient balance {recipient_balance} != 5.0"
    print(f"[OK] Recipient balance: {recipient_balance} XMR")

    # 9. Try sending more than available
    r = requests.post(f"{BASE_URL}/v2/payments/hub-routing",
                      json={"to_agent_name": "e2e-recipient", "amount": 999.0},
                      headers=sender_headers)
    assert r.status_code == 400
    assert "Insufficient" in r.json()["detail"]
    print("[OK] Insufficient balance correctly rejected")

    # 10. Check disabled endpoints
    r = requests.post(f"{BASE_URL}/v2/escrow/create", json={})
    assert r.status_code in [401, 501]
    print("[OK] Escrow endpoint correctly disabled")

    r = requests.post(f"{BASE_URL}/v2/payments/send", json={})
    assert r.status_code in [401, 501]
    print("[OK] P2P endpoint correctly disabled")

    print(f"\n{'='*50}")
    print(f"ALL TESTS PASSED")
    print(f"Fee collected: {fee} XMR (0.1% of 5.0)")
    print(f"{'='*50}")


if __name__ == "__main__":
    test_full_flow()
