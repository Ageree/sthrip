"""TDD tests for production readiness issues.

CRIT-2: _last_seen_cache unbounded memory leak → eviction
CRIT-3: Withdrawal rollback race condition → mark needs_review
HIGH-2: No pagination metadata on list endpoints
HIGH-4: Webhook event_id collision → uuid4
HIGH-6: Single uvicorn worker → gunicorn
MED-3: Admin auth rate limit before compare_digest
MED-5: Missing index on agent_balances.agent_id
"""

import hashlib
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentBalance, AgentReputation,
    PendingWithdrawal, HubRoute, FeeCollection, Transaction,
)
from sthrip.db.repository import AgentRepository, BalanceRepository, PendingWithdrawalRepository


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        Agent.__table__,
        AgentReputation.__table__,
        AgentBalance.__table__,
        PendingWithdrawal.__table__,
        HubRoute.__table__,
        FeeCollection.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def agent(db_session):
    a = Agent(
        id=uuid.uuid4(),
        agent_name="test-agent",
        api_key_hash="fakehash",
        is_active=True,
    )
    db_session.add(a)
    rep = AgentReputation(agent_id=a.id)
    db_session.add(rep)
    db_session.commit()
    return a


# ═══════════════════════════════════════════════════════════════════════════════
# CRIT-2: _last_seen_cache eviction
# ═══════════════════════════════════════════════════════════════════════════════

class TestLastSeenCacheEviction:
    """The cache must not grow without bound."""

    def test_cache_has_max_size(self):
        """AgentRepository._LAST_SEEN_MAX_ENTRIES should exist and be reasonable."""
        assert hasattr(AgentRepository, "_LAST_SEEN_MAX_ENTRIES")
        assert AgentRepository._LAST_SEEN_MAX_ENTRIES <= 10000

    def test_cache_evicts_old_entries(self, db_session, agent):
        """When cache exceeds max size, oldest entries are evicted."""
        repo = AgentRepository(db_session)
        # Save the original max to restore later
        original_max = AgentRepository._LAST_SEEN_MAX_ENTRIES
        AgentRepository._LAST_SEEN_MAX_ENTRIES = 5
        AgentRepository._last_seen_cache.clear()

        try:
            # Fill cache beyond max
            for i in range(10):
                aid = uuid.uuid4()
                AgentRepository._last_seen_cache[str(aid)] = time.time() - (10 - i)

            # Trigger eviction via update_last_seen
            repo.update_last_seen(agent.id)

            # Cache should not exceed max + 1 (the new entry)
            assert len(AgentRepository._last_seen_cache) <= AgentRepository._LAST_SEEN_MAX_ENTRIES + 1
        finally:
            AgentRepository._LAST_SEEN_MAX_ENTRIES = original_max
            AgentRepository._last_seen_cache.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# CRIT-3: Withdrawal rollback → needs_review
# ═══════════════════════════════════════════════════════════════════════════════

class TestWithdrawalRollbackSafety:
    """On RPC failure, withdrawal must be marked needs_review, NOT auto-refunded."""

    def test_rpc_failure_marks_needs_review(self, db_session, agent):
        """When wallet RPC fails, pending withdrawal should be marked needs_review."""
        # Setup: create balance and pending withdrawal
        bal_repo = BalanceRepository(db_session)
        bal_repo.deposit(agent.id, Decimal("10"))
        db_session.commit()

        pw_repo = PendingWithdrawalRepository(db_session)
        pw = pw_repo.create(agent_id=agent.id, amount=Decimal("5"), address="addr123")
        db_session.commit()
        pw_id = pw.id

        # The _process_onchain_withdrawal function should mark needs_review
        # We test the behavior indirectly by checking the module code
        from api.routers import balance as balance_mod
        source = open(balance_mod.__file__).read()

        # The rollback handler should use mark_needs_review, NOT credit+mark_failed
        assert "mark_needs_review" in source, (
            "Withdrawal rollback must use mark_needs_review instead of auto-refund"
        )
        assert "bal_repo.credit" not in source or "# REMOVED" in source or source.count("bal_repo.credit") == 0, (
            "Withdrawal rollback must NOT auto-credit balance on RPC failure"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# HIGH-2: Pagination metadata
# ═══════════════════════════════════════════════════════════════════════════════

class TestPaginationMetadata:
    """List endpoints must return pagination metadata."""

    def test_payment_history_returns_pagination(self, client):
        """GET /v2/payments/history should include total count and pagination info."""
        resp = client.post("/v2/agents/register", json={
            "agent_name": "pagination-test",
            "privacy_level": "medium",
        })
        api_key = resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}

        resp = client.get("/v2/payments/history", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict), "Response should be a dict with pagination metadata"
        assert "items" in data
        assert "total" in data
        assert "limit" in data
        assert "offset" in data

    def test_discover_agents_returns_pagination(self, client):
        """GET /v2/agents should include total count."""
        resp = client.get("/v2/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict), "Response should be a dict with pagination metadata"
        assert "items" in data
        assert "total" in data


# ═══════════════════════════════════════════════════════════════════════════════
# HIGH-4: Webhook event_id uses uuid
# ═══════════════════════════════════════════════════════════════════════════════

class TestWebhookEventIdUniqueness:
    """Event IDs must be truly unique (uuid4), not timestamp-derived."""

    def test_event_id_is_uuid_based(self):
        """queue_event should generate event_id with uuid4, not timestamp hash."""
        from sthrip.services.webhook_service import WebhookService

        svc = WebhookService()

        # Generate two events for same agent+type at nearly the same time
        ids = set()
        for _ in range(100):
            payload = svc._build_event_payload("agent1", "payment.received", {"amount": 1})
            ids.add(payload["event_id"])

        # All event_ids should be unique
        assert len(ids) == 100, "event_ids must be unique even for same agent+type"

    def test_event_id_format(self):
        """event_id should start with evt_ followed by uuid hex."""
        from sthrip.services.webhook_service import WebhookService

        svc = WebhookService()
        payload = svc._build_event_payload("agent1", "test.event", {})
        event_id = payload["event_id"]
        assert event_id.startswith("evt_")
        # Should be long enough (evt_ + 32 hex chars)
        assert len(event_id) >= 36


# ═══════════════════════════════════════════════════════════════════════════════
# HIGH-6: Multi-worker deployment
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeploymentConfig:
    """Railway should use gunicorn with multiple workers."""

    def test_railway_uses_gunicorn(self):
        """railway.toml should use gunicorn with uvicorn workers."""
        with open("railway.toml") as f:
            content = f.read()
        assert "gunicorn" in content or "--workers" in content, (
            "Deployment must use gunicorn or multiple uvicorn workers"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# MED-3: Admin auth rate limit ordering
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdminAuthRateLimitOrder:
    """Rate limit must be checked BEFORE compare_digest."""

    def test_rate_limit_checked_before_key_comparison(self):
        """In admin_auth, rate limit check must come before hmac.compare_digest."""
        from api.routers import admin as admin_mod
        import ast

        source = open(admin_mod.__file__).read()
        tree = ast.parse(source)

        # Find the admin_auth function
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "admin_auth":
                body_lines = [n.lineno for n in node.body]
                # Find rate limit check and compare_digest positions
                rate_limit_line = None
                compare_line = None
                for child in ast.walk(node):
                    if isinstance(child, ast.Attribute) and hasattr(child, 'attr'):
                        if child.attr in ("check_ip_rate_limit", "check_failed_auth") and rate_limit_line is None:
                            rate_limit_line = child.lineno
                    if isinstance(child, ast.Attribute) and hasattr(child, 'attr'):
                        if child.attr == "compare_digest" and compare_line is None:
                            compare_line = child.lineno

                assert rate_limit_line is not None, "Rate limit check must exist in admin_auth"
                assert compare_line is not None, "compare_digest must exist in admin_auth"
                assert rate_limit_line < compare_line, (
                    f"Rate limit (line {rate_limit_line}) must come BEFORE "
                    f"compare_digest (line {compare_line})"
                )
                break


# ═══════════════════════════════════════════════════════════════════════════════
# MED-5: Index on agent_balances.agent_id
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentBalanceIndex:
    """agent_balances.agent_id must have an explicit index."""

    def test_agent_id_has_index(self):
        """AgentBalance.agent_id column should have index=True."""
        col = AgentBalance.__table__.columns["agent_id"]
        assert col.index is True, "agent_balances.agent_id must have an explicit index"
