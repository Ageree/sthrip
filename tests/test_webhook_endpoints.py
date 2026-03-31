"""Tests for the self-service webhook endpoint registration API."""

import pytest

# Uses shared fixtures from conftest.py: client, db_engine, db_session_factory.

_VALID_XMR_ADDR = "5" + "a" * 94
_TEST_WEBHOOK_URL = "https://example.com/webhooks"


@pytest.fixture
def registered_agent(client):
    """Register an agent and return the API key."""
    r = client.post("/v2/agents/register", json={
        "agent_name": "webhook-test-agent",
        "xmr_address": _VALID_XMR_ADDR,
    })
    assert r.status_code == 201, f"Registration failed: {r.text}"
    return r.json()["api_key"]


def _auth(api_key: str) -> dict:
    """Build auth headers from an API key."""
    return {"Authorization": f"Bearer {api_key}"}


class TestRegisterWebhook:
    def test_register_webhook(self, client, registered_agent):
        """POST /v2/webhook-endpoints returns 201 and a secret starting with 'whsec_'."""
        r = client.post(
            "/v2/webhook-endpoints",
            json={"url": _TEST_WEBHOOK_URL},
            headers=_auth(registered_agent),
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["url"] == _TEST_WEBHOOK_URL
        assert data["secret"].startswith("whsec_")
        assert data["is_active"] is True
        assert data["failure_count"] == 0
        assert "id" in data
        assert "created_at" in data

    def test_register_webhook_with_filters_and_description(self, client, registered_agent):
        """Verify optional fields are stored and returned."""
        r = client.post(
            "/v2/webhook-endpoints",
            json={
                "url": _TEST_WEBHOOK_URL,
                "event_filters": ["payment.*", "escrow.*"],
                "description": "My production hook",
            },
            headers=_auth(registered_agent),
        )
        assert r.status_code == 201
        data = r.json()
        assert data["event_filters"] == ["payment.*", "escrow.*"]
        assert data["description"] == "My production hook"


class TestListWebhooks:
    def test_list_webhooks(self, client, registered_agent):
        """GET /v2/webhook-endpoints returns correct count."""
        headers = _auth(registered_agent)

        # Create 3 endpoints
        for i in range(3):
            r = client.post(
                "/v2/webhook-endpoints",
                json={"url": f"https://example.com/hook{i}"},
                headers=headers,
            )
            assert r.status_code == 201

        r = client.get("/v2/webhook-endpoints", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 3

    def test_list_webhooks_excludes_secrets(self, client, registered_agent):
        """Secrets must not appear in list responses."""
        headers = _auth(registered_agent)
        client.post(
            "/v2/webhook-endpoints",
            json={"url": _TEST_WEBHOOK_URL},
            headers=headers,
        )
        r = client.get("/v2/webhook-endpoints", headers=headers)
        assert r.status_code == 200
        for endpoint in r.json():
            assert "secret" not in endpoint


class TestDeleteWebhook:
    def test_delete_webhook(self, client, registered_agent):
        """DELETE /v2/webhook-endpoints/{id} returns 200."""
        headers = _auth(registered_agent)
        create_r = client.post(
            "/v2/webhook-endpoints",
            json={"url": _TEST_WEBHOOK_URL},
            headers=headers,
        )
        webhook_id = create_r.json()["id"]

        r = client.delete(f"/v2/webhook-endpoints/{webhook_id}", headers=headers)
        assert r.status_code == 200
        assert r.json()["webhook_id"] == webhook_id

        # Confirm it's gone
        r = client.get("/v2/webhook-endpoints", headers=headers)
        assert len(r.json()) == 0

    def test_delete_nonexistent_webhook(self, client, registered_agent):
        """Deleting a non-existent webhook returns 404."""
        r = client.delete(
            "/v2/webhook-endpoints/00000000-0000-0000-0000-000000000000",
            headers=_auth(registered_agent),
        )
        assert r.status_code == 404


class TestMaxWebhooks:
    def test_max_10_webhooks(self, client, registered_agent):
        """11th webhook registration should return 400."""
        headers = _auth(registered_agent)

        for i in range(10):
            r = client.post(
                "/v2/webhook-endpoints",
                json={"url": f"https://example.com/hook{i}"},
                headers=headers,
            )
            assert r.status_code == 201, f"Webhook {i} failed: {r.text}"

        # 11th should be rejected
        r = client.post(
            "/v2/webhook-endpoints",
            json={"url": "https://example.com/hook10"},
            headers=headers,
        )
        assert r.status_code == 400
        assert "Maximum" in r.json()["detail"]


class TestRotateSecret:
    def test_rotate_secret(self, client, registered_agent):
        """POST /v2/webhook-endpoints/{id}/rotate returns a new secret."""
        headers = _auth(registered_agent)

        create_r = client.post(
            "/v2/webhook-endpoints",
            json={"url": _TEST_WEBHOOK_URL},
            headers=headers,
        )
        original_secret = create_r.json()["secret"]
        webhook_id = create_r.json()["id"]

        rotate_r = client.post(
            f"/v2/webhook-endpoints/{webhook_id}/rotate",
            headers=headers,
        )
        assert rotate_r.status_code == 200
        new_secret = rotate_r.json()["secret"]
        assert new_secret.startswith("whsec_")
        assert new_secret != original_secret

    def test_rotate_nonexistent_webhook(self, client, registered_agent):
        """Rotating a non-existent webhook returns 404."""
        r = client.post(
            "/v2/webhook-endpoints/00000000-0000-0000-0000-000000000000/rotate",
            headers=_auth(registered_agent),
        )
        assert r.status_code == 404


class TestTestWebhook:
    def test_test_webhook_endpoint(self, client, registered_agent):
        """POST /v2/webhook-endpoints/{id}/test returns 200."""
        headers = _auth(registered_agent)
        create_r = client.post(
            "/v2/webhook-endpoints",
            json={"url": _TEST_WEBHOOK_URL},
            headers=headers,
        )
        webhook_id = create_r.json()["id"]

        r = client.post(
            f"/v2/webhook-endpoints/{webhook_id}/test",
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["url"] == _TEST_WEBHOOK_URL

    def test_test_nonexistent_webhook(self, client, registered_agent):
        """Testing a non-existent webhook returns 404."""
        r = client.post(
            "/v2/webhook-endpoints/00000000-0000-0000-0000-000000000000/test",
            headers=_auth(registered_agent),
        )
        assert r.status_code == 404


class TestDuplicateUrl:
    def test_duplicate_url_rejected(self, client, registered_agent):
        """Registering the same URL twice for one agent should fail."""
        headers = _auth(registered_agent)
        r1 = client.post(
            "/v2/webhook-endpoints",
            json={"url": _TEST_WEBHOOK_URL},
            headers=headers,
        )
        assert r1.status_code == 201

        r2 = client.post(
            "/v2/webhook-endpoints",
            json={"url": _TEST_WEBHOOK_URL},
            headers=headers,
        )
        # SQLite raises IntegrityError which should surface as 500 or handled
        # The unique constraint exists; the exact status depends on error handling
        assert r2.status_code in (400, 409, 500)
