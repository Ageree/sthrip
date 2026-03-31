"""Tests for E2E encrypted agent messaging via NaCl Box.

Tests cover:
- Registering encryption keys
- Retrieving public keys
- Sending encrypted messages (hub relay)
- Receiving inbox (mark as delivered)
- Full E2E encrypt/decrypt roundtrip with NaCl Box
- Message size limit (64 KB)
"""

import base64

import pytest
from nacl.public import PrivateKey, Box

# Uses shared fixtures from conftest.py: client, db_engine, db_session_factory.

_VALID_XMR_ADDR = "5" + "a" * 94


def _auth(api_key: str) -> dict:
    """Build auth headers from an API key."""
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture
def agent_a(client):
    """Register agent A and return (api_key, agent_id)."""
    r = client.post("/v2/agents/register", json={
        "agent_name": "msg-agent-a",
        "xmr_address": _VALID_XMR_ADDR,
    })
    assert r.status_code == 201, f"Registration failed: {r.text}"
    data = r.json()
    return data["api_key"], data["agent_id"]


@pytest.fixture
def agent_b(client):
    """Register agent B and return (api_key, agent_id)."""
    r = client.post("/v2/agents/register", json={
        "agent_name": "msg-agent-b",
        "xmr_address": _VALID_XMR_ADDR,
    })
    assert r.status_code == 201, f"Registration failed: {r.text}"
    data = r.json()
    return data["api_key"], data["agent_id"]


class TestEncryptionKey:
    def test_register_encryption_key(self, client, agent_a):
        """PUT /v2/me/encryption-key stores the public key."""
        api_key, agent_id = agent_a
        sk = PrivateKey.generate()
        pub_b64 = base64.b64encode(bytes(sk.public_key)).decode()

        r = client.put(
            "/v2/me/encryption-key",
            json={"public_key": pub_b64},
            headers=_auth(api_key),
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "ok"
        assert data["public_key"] == pub_b64

    def test_get_public_key(self, client, agent_a, agent_b):
        """GET /v2/agents/{id}/public-key returns the registered key."""
        api_key_a, agent_id_a = agent_a
        api_key_b, agent_id_b = agent_b

        # Register B's encryption key
        sk_b = PrivateKey.generate()
        pub_b_b64 = base64.b64encode(bytes(sk_b.public_key)).decode()
        r = client.put(
            "/v2/me/encryption-key",
            json={"public_key": pub_b_b64},
            headers=_auth(api_key_b),
        )
        assert r.status_code == 200

        # A retrieves B's public key
        r = client.get(
            f"/v2/agents/{agent_id_b}/public-key",
            headers=_auth(api_key_a),
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["agent_id"] == agent_id_b
        assert data["public_key"] == pub_b_b64

    def test_get_public_key_not_registered(self, client, agent_a, agent_b):
        """GET /v2/agents/{id}/public-key returns 404 if no key registered."""
        api_key_a, _ = agent_a
        _, agent_id_b = agent_b

        r = client.get(
            f"/v2/agents/{agent_id_b}/public-key",
            headers=_auth(api_key_a),
        )
        assert r.status_code == 404
        assert "encryption key" in r.json()["detail"].lower()

    def test_get_public_key_agent_not_found(self, client, agent_a):
        """GET /v2/agents/{id}/public-key returns 404 for missing agent."""
        api_key_a, _ = agent_a
        fake_id = "00000000-0000-0000-0000-000000000000"

        r = client.get(
            f"/v2/agents/{fake_id}/public-key",
            headers=_auth(api_key_a),
        )
        assert r.status_code == 404


class TestSendMessage:
    def test_send_message(self, client, agent_a, agent_b):
        """POST /v2/messages/send relays ciphertext and returns 201."""
        api_key_a, agent_id_a = agent_a
        _, agent_id_b = agent_b

        ciphertext = base64.b64encode(b"encrypted-payload").decode()
        nonce = base64.b64encode(b"x" * 24).decode()
        sender_pk = base64.b64encode(b"k" * 32).decode()

        r = client.post(
            "/v2/messages/send",
            json={
                "to_agent_id": agent_id_b,
                "ciphertext": ciphertext,
                "nonce": nonce,
                "sender_public_key": sender_pk,
            },
            headers=_auth(api_key_a),
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["status"] == "sent"
        assert "message_id" in data
        assert "expires_at" in data

    def test_message_size_limit(self, client, agent_a, agent_b):
        """POST /v2/messages/send rejects ciphertext > 64 KB."""
        api_key_a, _ = agent_a
        _, agent_id_b = agent_b

        # 65 KB payload exceeds the 64 KB limit
        oversized = base64.b64encode(b"x" * (65 * 1024)).decode()
        nonce = base64.b64encode(b"n" * 24).decode()
        sender_pk = base64.b64encode(b"k" * 32).decode()

        r = client.post(
            "/v2/messages/send",
            json={
                "to_agent_id": agent_id_b,
                "ciphertext": oversized,
                "nonce": nonce,
                "sender_public_key": sender_pk,
            },
            headers=_auth(api_key_a),
        )
        assert r.status_code == 400, r.text
        assert "64" in r.json()["detail"] or "size" in r.json()["detail"].lower()

    def test_send_to_nonexistent_agent(self, client, agent_a):
        """POST /v2/messages/send returns 404 for unknown recipient."""
        api_key_a, _ = agent_a
        fake_id = "00000000-0000-0000-0000-000000000000"

        ciphertext = base64.b64encode(b"data").decode()
        nonce = base64.b64encode(b"n" * 24).decode()
        sender_pk = base64.b64encode(b"k" * 32).decode()

        r = client.post(
            "/v2/messages/send",
            json={
                "to_agent_id": fake_id,
                "ciphertext": ciphertext,
                "nonce": nonce,
                "sender_public_key": sender_pk,
            },
            headers=_auth(api_key_a),
        )
        assert r.status_code == 404

    def test_send_with_payment_id(self, client, agent_a, agent_b):
        """POST /v2/messages/send accepts optional payment_id."""
        api_key_a, _ = agent_a
        _, agent_id_b = agent_b

        ciphertext = base64.b64encode(b"encrypted-payload").decode()
        nonce = base64.b64encode(b"n" * 24).decode()
        sender_pk = base64.b64encode(b"k" * 32).decode()

        r = client.post(
            "/v2/messages/send",
            json={
                "to_agent_id": agent_id_b,
                "ciphertext": ciphertext,
                "nonce": nonce,
                "sender_public_key": sender_pk,
                "payment_id": "pay-123-abc",
            },
            headers=_auth(api_key_a),
        )
        assert r.status_code == 201, r.text


class TestInbox:
    def test_inbox_empty(self, client, agent_a):
        """GET /v2/messages/inbox returns empty list for new agent."""
        api_key_a, _ = agent_a

        r = client.get("/v2/messages/inbox", headers=_auth(api_key_a))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["messages"] == []
        assert data["count"] == 0

    def test_inbox_returns_messages(self, client, agent_a, agent_b):
        """GET /v2/messages/inbox returns sent messages and marks them delivered."""
        api_key_a, agent_id_a = agent_a
        api_key_b, agent_id_b = agent_b

        # A sends two messages to B
        for i in range(2):
            ciphertext = base64.b64encode(f"msg-{i}".encode()).decode()
            nonce = base64.b64encode(b"n" * 24).decode()
            sender_pk = base64.b64encode(b"k" * 32).decode()
            r = client.post(
                "/v2/messages/send",
                json={
                    "to_agent_id": agent_id_b,
                    "ciphertext": ciphertext,
                    "nonce": nonce,
                    "sender_public_key": sender_pk,
                },
                headers=_auth(api_key_a),
            )
            assert r.status_code == 201

        # B fetches inbox
        r = client.get("/v2/messages/inbox", headers=_auth(api_key_b))
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 2
        assert len(data["messages"]) == 2
        assert data["messages"][0]["from_agent_id"] == agent_id_a

        # Second fetch should be empty (messages marked as delivered)
        r = client.get("/v2/messages/inbox", headers=_auth(api_key_b))
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 0


class TestE2EEncryptDecrypt:
    def test_full_nacl_box_roundtrip(self, client, agent_a, agent_b):
        """Full E2E test: A encrypts with NaCl Box, sends via hub, B decrypts.

        This proves the hub never sees plaintext and the crypto roundtrip works.
        """
        api_key_a, agent_id_a = agent_a
        api_key_b, agent_id_b = agent_b

        # Generate keypairs for both agents
        sk_a = PrivateKey.generate()
        sk_b = PrivateKey.generate()
        pub_a_b64 = base64.b64encode(bytes(sk_a.public_key)).decode()
        pub_b_b64 = base64.b64encode(bytes(sk_b.public_key)).decode()

        # Both agents register their encryption keys
        r = client.put(
            "/v2/me/encryption-key",
            json={"public_key": pub_a_b64},
            headers=_auth(api_key_a),
        )
        assert r.status_code == 200

        r = client.put(
            "/v2/me/encryption-key",
            json={"public_key": pub_b_b64},
            headers=_auth(api_key_b),
        )
        assert r.status_code == 200

        # A retrieves B's public key
        r = client.get(
            f"/v2/agents/{agent_id_b}/public-key",
            headers=_auth(api_key_a),
        )
        assert r.status_code == 200
        retrieved_pub_b = r.json()["public_key"]
        assert retrieved_pub_b == pub_b_b64

        # A encrypts a message for B using NaCl Box
        plaintext = b"Hello from Agent A! This is a secret message."
        box_a = Box(sk_a, sk_b.public_key)
        encrypted = box_a.encrypt(plaintext)
        # NaCl Box.encrypt() returns nonce (24 bytes) + ciphertext
        nonce_bytes = encrypted.nonce
        ciphertext_bytes = encrypted.ciphertext

        ciphertext_b64 = base64.b64encode(ciphertext_bytes).decode()
        nonce_b64 = base64.b64encode(nonce_bytes).decode()

        # A sends the encrypted message via the hub
        r = client.post(
            "/v2/messages/send",
            json={
                "to_agent_id": agent_id_b,
                "ciphertext": ciphertext_b64,
                "nonce": nonce_b64,
                "sender_public_key": pub_a_b64,
                "payment_id": "payment-e2e-test",
            },
            headers=_auth(api_key_a),
        )
        assert r.status_code == 201, r.text

        # B fetches inbox
        r = client.get("/v2/messages/inbox", headers=_auth(api_key_b))
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 1

        msg = data["messages"][0]
        assert msg["from_agent_id"] == agent_id_a
        assert msg["sender_public_key"] == pub_a_b64
        assert msg["payment_id"] == "payment-e2e-test"

        # B decrypts the message using NaCl Box
        received_ciphertext = base64.b64decode(msg["ciphertext"])
        received_nonce = base64.b64decode(msg["nonce"])

        box_b = Box(sk_b, sk_a.public_key)
        decrypted = box_b.decrypt(received_ciphertext, received_nonce)

        assert decrypted == plaintext
        assert decrypted == b"Hello from Agent A! This is a secret message."
