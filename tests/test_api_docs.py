"""Tests for API documentation endpoints (Phase 2).

TDD: Write tests FIRST, then implement.
Tests cover:
- Custom Redoc page at /docs
- Swagger UI at /docs/playground
- OpenAPI schema enrichment (tags, examples, descriptions)
- Getting Started section
- Rate limit info in descriptions
"""

import os
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from contextlib import ExitStack


@pytest.fixture
def docs_client():
    """Test client with docs endpoints enabled."""
    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy", "timestamp": "2026-03-09T00:00:00", "checks": {}
    }
    mock_monitor.get_alerts.return_value = []
    mock_monitor.start_monitoring.return_value = None
    mock_monitor.stop_monitoring.return_value = None

    mock_webhook = MagicMock()
    mock_webhook.start_worker = MagicMock(return_value=MagicMock())
    mock_webhook.stop_worker.return_value = None
    mock_webhook.close = MagicMock(return_value=MagicMock())

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "HUB_MODE": "ledger",
            "DATABASE_URL": "sqlite:///:memory:",
            "ENVIRONMENT": "dev",
            "ADMIN_API_KEY": "test-admin-key-123",
        }))
        stack.enter_context(patch("sthrip.services.monitoring.get_monitor", return_value=mock_monitor))
        stack.enter_context(patch("sthrip.services.monitoring.setup_default_monitoring", return_value=mock_monitor))
        stack.enter_context(patch("sthrip.services.webhook_service.get_webhook_service", return_value=mock_webhook))
        stack.enter_context(patch("sthrip.db.database.create_tables"))
        stack.enter_context(patch("sthrip.db.database.get_engine", return_value=MagicMock()))

        # Need to reimport to pick up env changes
        import importlib
        import api.main_v2 as main_mod
        importlib.reload(main_mod)

        app = main_mod.create_app()
        client = TestClient(app, raise_server_exceptions=False)
        yield client


class TestRedocEndpoint:
    """Test custom Redoc documentation page at /docs."""

    def test_docs_returns_200(self, docs_client):
        """GET /docs should return HTML page with Redoc."""
        resp = docs_client.get("/docs")
        assert resp.status_code == 200

    def test_docs_returns_html(self, docs_client):
        """GET /docs should return HTML content type."""
        resp = docs_client.get("/docs")
        assert "text/html" in resp.headers["content-type"]

    def test_docs_contains_redoc(self, docs_client):
        """GET /docs should contain Redoc library reference."""
        resp = docs_client.get("/docs")
        assert "redoc" in resp.text.lower()

    def test_docs_contains_branding(self, docs_client):
        """GET /docs should contain Sthrip branding."""
        resp = docs_client.get("/docs")
        assert "Sthrip" in resp.text

    def test_docs_references_openapi(self, docs_client):
        """GET /docs should reference the OpenAPI spec URL."""
        resp = docs_client.get("/docs")
        assert "/openapi.json" in resp.text


class TestSwaggerPlayground:
    """Test Swagger UI at /docs/playground."""

    def test_playground_returns_200(self, docs_client):
        """GET /docs/playground should return Swagger UI."""
        resp = docs_client.get("/docs/playground")
        assert resp.status_code == 200

    def test_playground_returns_html(self, docs_client):
        """GET /docs/playground should return HTML."""
        resp = docs_client.get("/docs/playground")
        assert "text/html" in resp.headers["content-type"]

    def test_playground_contains_swagger(self, docs_client):
        """GET /docs/playground should contain Swagger UI."""
        resp = docs_client.get("/docs/playground")
        assert "swagger" in resp.text.lower()


class TestOpenAPISchema:
    """Test enriched OpenAPI schema."""

    def test_openapi_returns_200(self, docs_client):
        """GET /openapi.json should return the schema."""
        resp = docs_client.get("/openapi.json")
        assert resp.status_code == 200

    def test_openapi_has_info(self, docs_client):
        """OpenAPI schema should have title and description."""
        schema = docs_client.get("/openapi.json").json()
        assert schema["info"]["title"] == "Sthrip API"
        assert "AI Agent" in schema["info"]["description"] or "agent" in schema["info"]["description"].lower()

    def test_openapi_has_tags(self, docs_client):
        """OpenAPI schema should define tag groups."""
        schema = docs_client.get("/openapi.json").json()
        assert "tags" in schema
        tag_names = [t["name"] for t in schema["tags"]]
        # All required tag groups present
        for expected in ["Registration", "Payments", "Balance", "Discovery", "Admin"]:
            assert expected in tag_names, f"Missing tag: {expected}"

    def test_openapi_tags_have_descriptions(self, docs_client):
        """Each tag should have a description."""
        schema = docs_client.get("/openapi.json").json()
        for tag in schema["tags"]:
            assert "description" in tag and len(tag["description"]) > 10, \
                f"Tag '{tag['name']}' missing description"

    def test_openapi_has_security_scheme(self, docs_client):
        """OpenAPI should define Bearer token security scheme."""
        schema = docs_client.get("/openapi.json").json()
        components = schema.get("components", {})
        security_schemes = components.get("securitySchemes", {})
        assert "BearerAuth" in security_schemes or "HTTPBearer" in security_schemes
        # Find the bearer scheme
        bearer = security_schemes.get("BearerAuth") or security_schemes.get("HTTPBearer")
        assert bearer["type"] == "http"
        assert bearer["scheme"] == "bearer"

    def test_openapi_has_error_responses(self, docs_client):
        """OpenAPI should define common error response schemas."""
        schema = docs_client.get("/openapi.json").json()
        components = schema.get("components", {})
        schemas = components.get("schemas", {})
        # Should have error response schemas
        assert "ErrorResponse" in schemas or "HTTPValidationError" in schemas

    def test_openapi_description_has_getting_started(self, docs_client):
        """OpenAPI description should include Getting Started guide."""
        schema = docs_client.get("/openapi.json").json()
        description = schema["info"]["description"]
        # Should contain getting started content
        assert "Getting Started" in description or "getting started" in description.lower()

    def test_openapi_description_has_register_flow(self, docs_client):
        """Getting Started should mention registration flow."""
        schema = docs_client.get("/openapi.json").json()
        description = schema["info"]["description"]
        assert "register" in description.lower()

    def test_openapi_description_has_payment_flow(self, docs_client):
        """Getting Started should mention payment flow."""
        schema = docs_client.get("/openapi.json").json()
        description = schema["info"]["description"]
        assert "payment" in description.lower()

    def test_openapi_description_has_mcp_info(self, docs_client):
        """Getting Started should mention MCP server."""
        schema = docs_client.get("/openapi.json").json()
        description = schema["info"]["description"]
        assert "MCP" in description

    def test_openapi_has_request_examples(self, docs_client):
        """At least some endpoints should have request body examples."""
        schema = docs_client.get("/openapi.json").json()
        paths = schema.get("paths", {})
        # Check registration endpoint has examples
        register_path = paths.get("/v2/agents/register", {})
        post = register_path.get("post", {})
        if "requestBody" in post:
            content = post["requestBody"].get("content", {})
            json_content = content.get("application/json", {})
            # Either has example or examples
            has_example = "example" in json_content or "examples" in json_content
            # Or schema has example
            schema_ref = json_content.get("schema", {})
            has_example = has_example or "example" in schema_ref
            assert has_example, "Registration endpoint should have request examples"


class TestDocsAvailableInAllEnvironments:
    """Docs should be available in all environments (not just dev)."""

    def test_openapi_available_in_production(self):
        """OpenAPI endpoint should be available even in production."""
        # The custom docs setup should override FastAPI's default
        # docs_url=None behavior and always serve /openapi.json
        mock_monitor = MagicMock()
        mock_monitor.get_health_report.return_value = {
            "status": "healthy", "timestamp": "2026-03-09T00:00:00", "checks": {}
        }
        mock_monitor.get_alerts.return_value = []
        mock_monitor.start_monitoring.return_value = None
        mock_monitor.stop_monitoring.return_value = None

        mock_webhook = MagicMock()
        mock_webhook.start_worker = MagicMock(return_value=MagicMock())
        mock_webhook.stop_worker.return_value = None
        mock_webhook.close = MagicMock(return_value=MagicMock())

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "HUB_MODE": "ledger",
                "DATABASE_URL": "sqlite:///:memory:",
                "ENVIRONMENT": "production",
                "ADMIN_API_KEY": "real-prod-key-not-placeholder-xyz",
            }))
            stack.enter_context(patch("sthrip.services.monitoring.get_monitor", return_value=mock_monitor))
            stack.enter_context(patch("sthrip.services.monitoring.setup_default_monitoring", return_value=mock_monitor))
            stack.enter_context(patch("sthrip.services.webhook_service.get_webhook_service", return_value=mock_webhook))
            stack.enter_context(patch("sthrip.db.database.create_tables"))
            stack.enter_context(patch("sthrip.db.database.get_engine", return_value=MagicMock()))

            import importlib
            import api.main_v2 as main_mod
            importlib.reload(main_mod)

            app = main_mod.create_app()
            client = TestClient(app, raise_server_exceptions=False)

            # Custom docs endpoints should work
            resp = client.get("/docs")
            assert resp.status_code == 200

            resp = client.get("/docs/playground")
            assert resp.status_code == 200

            resp = client.get("/openapi.json")
            assert resp.status_code == 200
