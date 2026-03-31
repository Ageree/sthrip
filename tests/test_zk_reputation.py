"""Tests for ZK reputation proof service and API endpoints."""

import base64
import json
import os
import uuid
import contextlib
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import Base, Agent, AgentReputation, AgentBalance
from sthrip.services.zk_reputation_service import ZKReputationService


# ---------------------------------------------------------------------------
# Unit tests — ZKReputationService
# ---------------------------------------------------------------------------


class TestZKReputationServiceUnit:
    """Pure unit tests for the ZK reputation service (no DB, no HTTP)."""

    def setup_method(self) -> None:
        self.svc = ZKReputationService()

    # -- create_commitment -------------------------------------------------

    def test_create_commitment_returns_hex_strings(self) -> None:
        commitment, blinding = self.svc.create_commitment(75)
        assert isinstance(commitment, str) and len(commitment) == 64
        assert isinstance(blinding, str) and len(blinding) == 64

    def test_create_commitment_deterministic_with_same_blinding(self) -> None:
        """Given the same score and blinding, the commitment is deterministic."""
        _, blinding = self.svc.create_commitment(50)
        c1 = self.svc._compute_commitment(50, blinding)
        c2 = self.svc._compute_commitment(50, blinding)
        assert c1 == c2

    def test_create_commitment_different_scores_differ(self) -> None:
        c1, _ = self.svc.create_commitment(50)
        c2, _ = self.svc.create_commitment(51)
        # Different blinding factors are used, so they should differ
        assert c1 != c2

    def test_create_commitment_rejects_invalid_score(self) -> None:
        with pytest.raises(ValueError, match="between 0 and 100"):
            self.svc.create_commitment(-1)
        with pytest.raises(ValueError, match="between 0 and 100"):
            self.svc.create_commitment(101)

    # -- generate_proof + verify_proof -------------------------------------

    def test_generate_and_verify_proof(self) -> None:
        """Score 75 can prove >= 50."""
        commitment, blinding = self.svc.create_commitment(75)
        proof = self.svc.generate_proof(75, blinding, 50)
        assert self.svc.verify_proof(commitment, proof, 50)

    def test_proof_fails_for_insufficient_score(self) -> None:
        """Score 40 cannot prove >= 50."""
        _, blinding = self.svc.create_commitment(40)
        with pytest.raises(ValueError, match="below threshold"):
            self.svc.generate_proof(40, blinding, 50)

    def test_different_thresholds(self) -> None:
        """Score 60 can prove >= 50 and >= 60 but NOT >= 61."""
        commitment, blinding = self.svc.create_commitment(60)

        proof_50 = self.svc.generate_proof(60, blinding, 50)
        assert self.svc.verify_proof(commitment, proof_50, 50)

        proof_60 = self.svc.generate_proof(60, blinding, 60)
        assert self.svc.verify_proof(commitment, proof_60, 60)

        with pytest.raises(ValueError, match="below threshold"):
            self.svc.generate_proof(60, blinding, 61)

    def test_verify_rejects_tampered_proof(self) -> None:
        """Tampering with the proof payload invalidates the proof."""
        commitment, blinding = self.svc.create_commitment(75)
        proof = self.svc.generate_proof(75, blinding, 50)

        # Decode, tamper with the score, re-encode
        payload = json.loads(base64.b64decode(proof))
        payload["score"] = 30  # lower than threshold
        tampered = base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode()
        assert not self.svc.verify_proof(commitment, tampered, 50)

    def test_verify_rejects_wrong_commitment(self) -> None:
        """Proof verified against a different commitment returns False."""
        _, blinding = self.svc.create_commitment(75)
        proof = self.svc.generate_proof(75, blinding, 50)
        wrong_commitment = "a" * 64
        assert not self.svc.verify_proof(wrong_commitment, proof, 50)

    def test_verify_rejects_garbage_proof(self) -> None:
        assert not self.svc.verify_proof("abc", "not-base64!", 50)

    def test_verify_rejects_higher_threshold_than_proven(self) -> None:
        """A proof generated for threshold 50 fails verification at threshold 80
        if the score is 60."""
        commitment, blinding = self.svc.create_commitment(60)
        proof = self.svc.generate_proof(60, blinding, 50)
        # The proof embeds score=60 which is < 80, so verification must fail
        assert not self.svc.verify_proof(commitment, proof, 80)

    def test_boundary_score_zero(self) -> None:
        commitment, blinding = self.svc.create_commitment(0)
        proof = self.svc.generate_proof(0, blinding, 0)
        assert self.svc.verify_proof(commitment, proof, 0)

    def test_boundary_score_hundred(self) -> None:
        commitment, blinding = self.svc.create_commitment(100)
        proof = self.svc.generate_proof(100, blinding, 100)
        assert self.svc.verify_proof(commitment, proof, 100)

    def test_generate_proof_rejects_invalid_threshold(self) -> None:
        _, blinding = self.svc.create_commitment(50)
        with pytest.raises(ValueError, match="Threshold must be"):
            self.svc.generate_proof(50, blinding, -1)
        with pytest.raises(ValueError, match="Threshold must be"):
            self.svc.generate_proof(50, blinding, 101)


# ---------------------------------------------------------------------------
# Integration tests — API endpoints
# ---------------------------------------------------------------------------

# Stable Fernet key for tests
_TEST_ENCRYPTION_KEY = "uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE="

# Modules where get_db must be patched (same list as conftest + reputation)
_GET_DB_MODULES = [
    "sthrip.db.database",
    "sthrip.services.agent_registry",
    "sthrip.services.fee_collector",
    "sthrip.services.webhook_service",
    "api.main_v2",
    "api.deps",
    "api.routers.health",
    "api.routers.agents",
    "api.routers.payments",
    "api.routers.balance",
    "api.routers.webhooks",
    "api.routers.spending_policy",
    "api.routers.webhook_endpoints",
    "api.routers.reputation",
    "api.routers.messages",
]

_RATE_LIMITER_MODULES = [
    "sthrip.services.rate_limiter",
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
]

_AUDIT_LOG_MODULES = [
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
    "api.routers.payments",
    "api.routers.balance",
    "api.routers.admin",
]

_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
]


@pytest.fixture
def _rep_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_TABLES)
    return engine


@pytest.fixture
def _rep_session_factory(_rep_engine):
    return sessionmaker(bind=_rep_engine, expire_on_commit=False)


def _seed_agent_with_reputation(session_factory, trust_score: int = 75):
    """Insert an agent + reputation row and return (agent_id, api_key_hash)."""
    import hashlib
    import hmac as _hmac

    agent_id = uuid.uuid4()
    api_key = "test-api-key-reputation"
    settings = __import__("sthrip.config", fromlist=["get_settings"]).get_settings()
    key_hash = _hmac.new(
        settings.api_key_hmac_secret.encode(),
        api_key.encode(),
        hashlib.sha256,
    ).hexdigest()

    session = session_factory()
    agent = Agent(
        id=agent_id,
        agent_name="zk-test-agent",
        api_key_hash=key_hash,
        is_active=True,
    )
    session.add(agent)
    session.flush()
    rep = AgentReputation(agent_id=agent_id, trust_score=trust_score)
    session.add(rep)
    session.commit()
    session.close()
    return agent_id, api_key


@pytest.fixture
def rep_client(_rep_engine, _rep_session_factory):
    """TestClient wired to the in-memory DB with an agent+reputation seeded."""

    @contextmanager
    def get_test_db():
        session = _rep_session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    mock_limiter = MagicMock()
    mock_limiter.check_rate_limit.return_value = None
    mock_limiter.check_ip_rate_limit.return_value = None
    mock_limiter.get_limit_status.return_value = {"requests_remaining": 100}

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy",
        "timestamp": "2026-03-03T00:00:00",
        "checks": {},
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HUB_MODE": "ledger"}))

        for mod in _GET_DB_MODULES:
            stack.enter_context(patch(f"{mod}.get_db", side_effect=get_test_db))
        stack.enter_context(patch("sthrip.db.database.create_tables"))

        for mod in _RATE_LIMITER_MODULES:
            stack.enter_context(
                patch(f"{mod}.get_rate_limiter", return_value=mock_limiter)
            )

        for mod in _AUDIT_LOG_MODULES:
            stack.enter_context(patch(f"{mod}.audit_log"))

        stack.enter_context(
            patch(
                "sthrip.services.monitoring.get_monitor",
                return_value=mock_monitor,
            )
        )
        stack.enter_context(
            patch(
                "sthrip.services.monitoring.setup_default_monitoring",
                return_value=mock_monitor,
            )
        )
        stack.enter_context(
            patch(
                "sthrip.services.webhook_service.get_webhook_service",
                return_value=mock_webhook,
            )
        )
        stack.enter_context(
            patch("sthrip.services.webhook_service.queue_webhook")
        )

        agent_id, api_key = _seed_agent_with_reputation(_rep_session_factory, trust_score=75)

        from api.main_v2 import app

        client = TestClient(app, raise_server_exceptions=False)
        client._test_api_key = api_key  # type: ignore[attr-defined]
        yield client


def _auth_headers(client: TestClient) -> dict:
    return {"Authorization": f"Bearer {client._test_api_key}"}  # type: ignore[attr-defined]


class TestReputationProofAPI:
    """Integration tests for reputation proof endpoints."""

    def test_generate_proof_success(self, rep_client: TestClient) -> None:
        resp = rep_client.post(
            "/v2/me/reputation-proof",
            json={"threshold": 50},
            headers=_auth_headers(rep_client),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "commitment" in data
        assert "proof" in data
        assert data["threshold"] == 50

    def test_generate_proof_at_exact_score(self, rep_client: TestClient) -> None:
        resp = rep_client.post(
            "/v2/me/reputation-proof",
            json={"threshold": 75},
            headers=_auth_headers(rep_client),
        )
        assert resp.status_code == 200, resp.text

    def test_generate_proof_exceeds_score(self, rep_client: TestClient) -> None:
        resp = rep_client.post(
            "/v2/me/reputation-proof",
            json={"threshold": 80},
            headers=_auth_headers(rep_client),
        )
        assert resp.status_code == 400, resp.text
        assert "below threshold" in resp.json()["detail"]

    def test_verify_valid_proof(self, rep_client: TestClient) -> None:
        # Generate
        gen_resp = rep_client.post(
            "/v2/me/reputation-proof",
            json={"threshold": 50},
            headers=_auth_headers(rep_client),
        )
        assert gen_resp.status_code == 200
        gen_data = gen_resp.json()

        # Verify (public, no auth)
        verify_resp = rep_client.post(
            "/v2/verify-reputation",
            json={
                "commitment": gen_data["commitment"],
                "proof": gen_data["proof"],
                "threshold": 50,
            },
        )
        assert verify_resp.status_code == 200
        assert verify_resp.json()["valid"] is True

    def test_verify_rejects_wrong_threshold(self, rep_client: TestClient) -> None:
        gen_resp = rep_client.post(
            "/v2/me/reputation-proof",
            json={"threshold": 50},
            headers=_auth_headers(rep_client),
        )
        gen_data = gen_resp.json()

        verify_resp = rep_client.post(
            "/v2/verify-reputation",
            json={
                "commitment": gen_data["commitment"],
                "proof": gen_data["proof"],
                "threshold": 80,
            },
        )
        assert verify_resp.status_code == 200
        assert verify_resp.json()["valid"] is False

    def test_verify_rejects_garbage(self, rep_client: TestClient) -> None:
        resp = rep_client.post(
            "/v2/verify-reputation",
            json={
                "commitment": "0" * 64,
                "proof": "bm90LWEtdmFsaWQtcHJvb2Y=",
                "threshold": 50,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["valid"] is False

    def test_generate_requires_auth(self, rep_client: TestClient) -> None:
        resp = rep_client.post(
            "/v2/me/reputation-proof",
            json={"threshold": 50},
        )
        assert resp.status_code == 401

    def test_threshold_validation(self, rep_client: TestClient) -> None:
        resp = rep_client.post(
            "/v2/me/reputation-proof",
            json={"threshold": 150},
            headers=_auth_headers(rep_client),
        )
        assert resp.status_code == 422  # Pydantic validation
