"""Integration tests for SLA API endpoints (TDD — tests written first).

Test coverage:
- POST /v2/sla/templates            (201, 401, 422)
- GET  /v2/sla/templates            (200 mine, 200 public)
- GET  /v2/sla/templates/{id}       (200, 404)
- POST /v2/sla/contracts            (201, 400, 401, 422)
- GET  /v2/sla/contracts            (200)
- GET  /v2/sla/contracts/{id}       (200, 403, 404)
- PATCH /v2/sla/contracts/{id}/accept   (200, 403, 404)
- PATCH /v2/sla/contracts/{id}/deliver  (200, 400, 403, 404)
- PATCH /v2/sla/contracts/{id}/verify   (200, 400, 403, 404)
- POST  /v2/sla/contracts/{id}/dispute  (200, 403, 404)
"""

from unittest.mock import patch, MagicMock
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register(client, name: str) -> dict:
    """Register an agent and return its full response dict."""
    r = client.post("/v2/agents/register", json={"agent_name": name})
    assert r.status_code == 201, f"agent registration failed: {r.text}"
    return r.json()


def _auth(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


_TEMPLATE_PAYLOAD = {
    "name": "Data Enrichment SLA",
    "service_description": "Enrich structured datasets with external sources",
    "deliverables": [{"item": "enriched_csv", "format": "CSV"}],
    "response_time_secs": 3600,
    "delivery_time_secs": 86400,
    "base_price": "0.05",
    "currency": "XMR",
    "penalty_percent": 10,
}


def _funded_contract_payload(provider_name: str) -> dict:
    return {
        "provider_agent_name": provider_name,
        "service_description": "Enrich my dataset",
        "deliverables": [{"item": "result.csv"}],
        "response_time_secs": 3600,
        "delivery_time_secs": 86400,
        "price": "0.05",
        "currency": "XMR",
        "penalty_percent": 10,
    }


# ---------------------------------------------------------------------------
# Template tests
# ---------------------------------------------------------------------------

class TestCreateTemplate:
    def test_create_template_201(self, client):
        provider = _register(client, "sla-tmpl-provider-1")
        headers = _auth(provider["api_key"])

        r = client.post("/v2/sla/templates", json=_TEMPLATE_PAYLOAD, headers=headers)

        assert r.status_code == 201
        body = r.json()
        assert body["name"] == _TEMPLATE_PAYLOAD["name"]
        assert body["provider_id"] == provider["agent_id"]
        assert "id" in body
        assert body["is_active"] is True

    def test_create_template_requires_auth(self, client):
        r = client.post("/v2/sla/templates", json=_TEMPLATE_PAYLOAD)
        assert r.status_code == 401

    def test_create_template_validation_missing_name(self, client):
        provider = _register(client, "sla-tmpl-val-1")
        headers = _auth(provider["api_key"])
        payload = {k: v for k, v in _TEMPLATE_PAYLOAD.items() if k != "name"}

        r = client.post("/v2/sla/templates", json=payload, headers=headers)

        assert r.status_code == 422

    def test_create_template_validation_bad_response_time(self, client):
        provider = _register(client, "sla-tmpl-val-2")
        headers = _auth(provider["api_key"])
        payload = {**_TEMPLATE_PAYLOAD, "response_time_secs": 0}

        r = client.post("/v2/sla/templates", json=payload, headers=headers)

        assert r.status_code == 422

    def test_create_template_validation_bad_penalty(self, client):
        provider = _register(client, "sla-tmpl-val-3")
        headers = _auth(provider["api_key"])
        payload = {**_TEMPLATE_PAYLOAD, "penalty_percent": 99}

        r = client.post("/v2/sla/templates", json=payload, headers=headers)

        assert r.status_code == 422

    def test_create_template_validation_price_too_high(self, client):
        provider = _register(client, "sla-tmpl-val-4")
        headers = _auth(provider["api_key"])
        payload = {**_TEMPLATE_PAYLOAD, "base_price": "99999"}

        r = client.post("/v2/sla/templates", json=payload, headers=headers)

        assert r.status_code == 422


class TestListTemplates:
    def test_list_my_templates(self, client):
        provider = _register(client, "sla-list-tmpl-1")
        headers = _auth(provider["api_key"])
        client.post("/v2/sla/templates", json=_TEMPLATE_PAYLOAD, headers=headers)

        r = client.get("/v2/sla/templates", headers=headers)

        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert body["total"] >= 1

    def test_list_public_templates(self, client):
        provider = _register(client, "sla-list-tmpl-2")
        headers = _auth(provider["api_key"])
        client.post("/v2/sla/templates", json=_TEMPLATE_PAYLOAD, headers=headers)

        # Another agent listing public templates
        consumer = _register(client, "sla-list-tmpl-consumer-1")
        c_headers = _auth(consumer["api_key"])

        r = client.get("/v2/sla/templates?public=true", headers=c_headers)

        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert body["total"] >= 1

    def test_list_templates_requires_auth(self, client):
        r = client.get("/v2/sla/templates")
        assert r.status_code == 401


class TestGetTemplate:
    def test_get_template_200(self, client):
        provider = _register(client, "sla-get-tmpl-1")
        headers = _auth(provider["api_key"])
        create_r = client.post("/v2/sla/templates", json=_TEMPLATE_PAYLOAD, headers=headers)
        template_id = create_r.json()["id"]

        r = client.get(f"/v2/sla/templates/{template_id}", headers=headers)

        assert r.status_code == 200
        assert r.json()["id"] == template_id

    def test_get_template_404(self, client):
        provider = _register(client, "sla-get-tmpl-404")
        headers = _auth(provider["api_key"])
        fake_id = "00000000-0000-0000-0000-000000000000"

        r = client.get(f"/v2/sla/templates/{fake_id}", headers=headers)

        assert r.status_code == 404

    def test_get_template_requires_auth(self, client):
        fake_id = "00000000-0000-0000-0000-000000000000"
        r = client.get(f"/v2/sla/templates/{fake_id}")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------

def _create_contract_with_mock(client, consumer, provider_name: str) -> dict:
    """Create an SLA contract with EscrowService and balance mocked."""
    headers = _auth(consumer["api_key"])
    mock_escrow_result = {
        "escrow_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "status": "created",
        "amount": "0.05",
        "seller_agent_name": provider_name,
        "description": "Enrich my dataset",
        "accept_deadline": "2099-01-01T00:00:00+00:00",
        "created_at": "2026-01-01T00:00:00+00:00",
        "buyer_agent_name": consumer["agent_name"] if "agent_name" in consumer else "consumer",
        "seller_id": "aaaaaaaa-bbbb-cccc-dddd-ffffffffffff",
        "buyer_id": str(consumer["agent_id"]),
    }
    with (
        patch("sthrip.services.sla_service.EscrowService") as mock_es_cls,
        patch("sthrip.services.sla_service.audit_log"),
        patch("sthrip.services.sla_service.queue_webhook"),
        patch("sthrip.db.balance_repo.BalanceRepository.get_available", return_value=__import__("decimal").Decimal("1.0")),
    ):
        mock_es = MagicMock()
        mock_es.create_escrow.return_value = mock_escrow_result
        mock_es_cls.return_value = mock_es

        r = client.post(
            "/v2/sla/contracts",
            json=_funded_contract_payload(provider_name),
            headers=headers,
        )
    return r


class TestCreateContract:
    def test_create_contract_201(self, client):
        consumer = _register(client, "sla-ctr-consumer-1")
        provider = _register(client, "sla-ctr-provider-1")

        r = _create_contract_with_mock(client, consumer, provider["agent_name"])

        assert r.status_code == 201, r.text
        body = r.json()
        assert body["state"] == "proposed"
        assert body["price"] == "0.05"
        assert "contract_id" in body

    def test_create_contract_requires_auth(self, client):
        r = client.post("/v2/sla/contracts", json={"provider_agent_name": "x", "price": "0.05"})
        assert r.status_code == 401

    def test_create_contract_provider_not_found(self, client):
        consumer = _register(client, "sla-ctr-consumer-nf")
        headers = _auth(consumer["api_key"])

        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
            patch("sthrip.db.balance_repo.BalanceRepository.get_available", return_value=__import__("decimal").Decimal("1.0")),
        ):
            r = client.post(
                "/v2/sla/contracts",
                json=_funded_contract_payload("nonexistent-provider"),
                headers=headers,
            )
        assert r.status_code == 404

    def test_create_contract_validation_missing_price(self, client):
        consumer = _register(client, "sla-ctr-val-1")
        headers = _auth(consumer["api_key"])
        payload = {"provider_agent_name": "someone"}

        r = client.post("/v2/sla/contracts", json=payload, headers=headers)
        assert r.status_code == 422

    def test_create_contract_cannot_self_deal(self, client):
        agent = _register(client, "sla-self-deal")
        headers = _auth(agent["api_key"])

        with (
            patch("sthrip.services.sla_service.EscrowService") as mock_es_cls,
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
            patch("sthrip.db.balance_repo.BalanceRepository.get_available", return_value=__import__("decimal").Decimal("1.0")),
        ):
            mock_es = MagicMock()
            mock_es_cls.return_value = mock_es
            r = client.post(
                "/v2/sla/contracts",
                json=_funded_contract_payload(agent["agent_name"]),
                headers=headers,
            )
        assert r.status_code == 400


class TestListContracts:
    def test_list_contracts(self, client):
        consumer = _register(client, "sla-list-ctr-c1")
        provider = _register(client, "sla-list-ctr-p1")
        _create_contract_with_mock(client, consumer, provider["agent_name"])

        headers = _auth(consumer["api_key"])
        r = client.get("/v2/sla/contracts", headers=headers)

        assert r.status_code == 200
        body = r.json()
        assert "items" in body
        assert body["total"] >= 1

    def test_list_contracts_requires_auth(self, client):
        r = client.get("/v2/sla/contracts")
        assert r.status_code == 401

    def test_list_contracts_filter_by_role(self, client):
        consumer = _register(client, "sla-list-role-c1")
        provider = _register(client, "sla-list-role-p1")
        _create_contract_with_mock(client, consumer, provider["agent_name"])

        headers = _auth(consumer["api_key"])
        r = client.get("/v2/sla/contracts?role=consumer", headers=headers)

        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 1


class TestGetContract:
    def test_get_contract_200(self, client):
        consumer = _register(client, "sla-get-ctr-c1")
        provider = _register(client, "sla-get-ctr-p1")
        create_r = _create_contract_with_mock(client, consumer, provider["agent_name"])
        contract_id = create_r.json()["contract_id"]

        headers = _auth(consumer["api_key"])
        r = client.get(f"/v2/sla/contracts/{contract_id}", headers=headers)

        assert r.status_code == 200
        assert r.json()["contract_id"] == contract_id

    def test_get_contract_403_for_unrelated_agent(self, client):
        consumer = _register(client, "sla-get-403-c1")
        provider = _register(client, "sla-get-403-p1")
        unrelated = _register(client, "sla-get-403-unrelated")
        create_r = _create_contract_with_mock(client, consumer, provider["agent_name"])
        contract_id = create_r.json()["contract_id"]

        headers = _auth(unrelated["api_key"])
        r = client.get(f"/v2/sla/contracts/{contract_id}", headers=headers)

        assert r.status_code == 403

    def test_get_contract_404(self, client):
        agent = _register(client, "sla-get-404-a1")
        headers = _auth(agent["api_key"])
        fake_id = "00000000-0000-0000-0000-000000000001"

        r = client.get(f"/v2/sla/contracts/{fake_id}", headers=headers)

        assert r.status_code == 404


class TestAcceptContract:
    def test_accept_contract_200(self, client):
        consumer = _register(client, "sla-accept-c1")
        provider = _register(client, "sla-accept-p1")
        create_r = _create_contract_with_mock(client, consumer, provider["agent_name"])
        contract_id = create_r.json()["contract_id"]

        p_headers = _auth(provider["api_key"])
        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
        ):
            r = client.patch(f"/v2/sla/contracts/{contract_id}/accept", headers=p_headers)

        assert r.status_code == 200
        assert r.json()["state"] == "active"

    def test_accept_contract_403_consumer_cannot_accept(self, client):
        consumer = _register(client, "sla-accept-c2")
        provider = _register(client, "sla-accept-p2")
        create_r = _create_contract_with_mock(client, consumer, provider["agent_name"])
        contract_id = create_r.json()["contract_id"]

        c_headers = _auth(consumer["api_key"])
        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
        ):
            r = client.patch(f"/v2/sla/contracts/{contract_id}/accept", headers=c_headers)

        assert r.status_code == 403

    def test_accept_contract_404(self, client):
        agent = _register(client, "sla-accept-404")
        headers = _auth(agent["api_key"])
        fake_id = "00000000-0000-0000-0000-000000000002"

        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
        ):
            r = client.patch(f"/v2/sla/contracts/{fake_id}/accept", headers=headers)

        assert r.status_code == 404


class TestDeliverContract:
    def _setup_active_contract(self, client):
        consumer = _register(client, "sla-deliver-c1")
        provider = _register(client, "sla-deliver-p1")
        create_r = _create_contract_with_mock(client, consumer, provider["agent_name"])
        contract_id = create_r.json()["contract_id"]

        # Accept to move to ACTIVE
        p_headers = _auth(provider["api_key"])
        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
        ):
            client.patch(f"/v2/sla/contracts/{contract_id}/accept", headers=p_headers)

        return consumer, provider, contract_id

    def test_deliver_contract_200(self, client):
        consumer, provider, contract_id = self._setup_active_contract(client)
        p_headers = _auth(provider["api_key"])

        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
        ):
            r = client.patch(
                f"/v2/sla/contracts/{contract_id}/deliver",
                json={"result_hash": "abc123deadbeef"},
                headers=p_headers,
            )

        assert r.status_code == 200
        assert r.json()["state"] == "delivered"

    def test_deliver_contract_403_consumer_cannot_deliver(self, client):
        consumer, provider, contract_id = self._setup_active_contract(client)
        c_headers = _auth(consumer["api_key"])

        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
        ):
            r = client.patch(
                f"/v2/sla/contracts/{contract_id}/deliver",
                json={"result_hash": "abc123"},
                headers=c_headers,
            )

        assert r.status_code == 403

    def test_deliver_contract_validation_missing_hash(self, client):
        consumer, provider, contract_id = self._setup_active_contract(client)
        p_headers = _auth(provider["api_key"])

        r = client.patch(
            f"/v2/sla/contracts/{contract_id}/deliver",
            json={},
            headers=p_headers,
        )
        assert r.status_code == 422

    def test_deliver_contract_404(self, client):
        agent = _register(client, "sla-deliver-404")
        headers = _auth(agent["api_key"])
        fake_id = "00000000-0000-0000-0000-000000000003"

        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
        ):
            r = client.patch(
                f"/v2/sla/contracts/{fake_id}/deliver",
                json={"result_hash": "abc"},
                headers=headers,
            )

        assert r.status_code == 404


class TestVerifyContract:
    def _setup_delivered_contract(self, client):
        consumer = _register(client, "sla-verify-c1")
        provider = _register(client, "sla-verify-p1")
        create_r = _create_contract_with_mock(client, consumer, provider["agent_name"])
        contract_id = create_r.json()["contract_id"]

        p_headers = _auth(provider["api_key"])
        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
        ):
            client.patch(f"/v2/sla/contracts/{contract_id}/accept", headers=p_headers)
            client.patch(
                f"/v2/sla/contracts/{contract_id}/deliver",
                json={"result_hash": "hashvalue"},
                headers=p_headers,
            )

        return consumer, provider, contract_id

    def test_verify_contract_200(self, client):
        consumer, provider, contract_id = self._setup_delivered_contract(client)
        c_headers = _auth(consumer["api_key"])

        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
        ):
            r = client.patch(f"/v2/sla/contracts/{contract_id}/verify", headers=c_headers)

        assert r.status_code == 200
        assert r.json()["state"] == "completed"

    def test_verify_contract_403_provider_cannot_verify(self, client):
        consumer, provider, contract_id = self._setup_delivered_contract(client)
        p_headers = _auth(provider["api_key"])

        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
        ):
            r = client.patch(f"/v2/sla/contracts/{contract_id}/verify", headers=p_headers)

        assert r.status_code == 403

    def test_verify_contract_400_wrong_state(self, client):
        consumer = _register(client, "sla-verify-400-c1")
        provider = _register(client, "sla-verify-400-p1")
        create_r = _create_contract_with_mock(client, consumer, provider["agent_name"])
        contract_id = create_r.json()["contract_id"]

        # Contract is still PROPOSED — verify should fail with 400
        c_headers = _auth(consumer["api_key"])
        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
        ):
            r = client.patch(f"/v2/sla/contracts/{contract_id}/verify", headers=c_headers)

        assert r.status_code == 400

    def test_verify_contract_404(self, client):
        agent = _register(client, "sla-verify-404")
        headers = _auth(agent["api_key"])
        fake_id = "00000000-0000-0000-0000-000000000004"

        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
        ):
            r = client.patch(f"/v2/sla/contracts/{fake_id}/verify", headers=headers)

        assert r.status_code == 404


class TestDisputeContract:
    def test_dispute_contract_200_by_consumer(self, client):
        consumer = _register(client, "sla-dispute-c1")
        provider = _register(client, "sla-dispute-p1")
        create_r = _create_contract_with_mock(client, consumer, provider["agent_name"])
        contract_id = create_r.json()["contract_id"]

        # Move to ACTIVE first
        p_headers = _auth(provider["api_key"])
        c_headers = _auth(consumer["api_key"])
        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
        ):
            client.patch(f"/v2/sla/contracts/{contract_id}/accept", headers=p_headers)
            r = client.post(f"/v2/sla/contracts/{contract_id}/dispute", headers=c_headers)

        assert r.status_code == 200
        assert r.json()["state"] == "disputed"

    def test_dispute_contract_403_unrelated_agent(self, client):
        consumer = _register(client, "sla-dispute-c2")
        provider = _register(client, "sla-dispute-p2")
        unrelated = _register(client, "sla-dispute-unrelated-1")
        create_r = _create_contract_with_mock(client, consumer, provider["agent_name"])
        contract_id = create_r.json()["contract_id"]

        u_headers = _auth(unrelated["api_key"])
        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
        ):
            r = client.post(f"/v2/sla/contracts/{contract_id}/dispute", headers=u_headers)

        assert r.status_code == 403

    def test_dispute_contract_404(self, client):
        agent = _register(client, "sla-dispute-404")
        headers = _auth(agent["api_key"])
        fake_id = "00000000-0000-0000-0000-000000000005"

        with (
            patch("sthrip.services.sla_service.audit_log"),
            patch("sthrip.services.sla_service.queue_webhook"),
        ):
            r = client.post(f"/v2/sla/contracts/{fake_id}/dispute", headers=headers)

        assert r.status_code == 404
