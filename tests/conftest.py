"""Shared test fixtures — ensures get_settings() works in all tests.

Also provides reusable db_engine, db_session_factory, and client fixtures
for API integration tests that need the standard ExitStack+patches pattern.
"""

import os
import contextlib
import pytest
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance,
    HubRoute, FeeCollection, PendingWithdrawal, Transaction,
)

# Stable Fernet key for tests (base64url-encoded 32-byte key).
_TEST_ENCRYPTION_KEY = "uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE="

# Superset of SQLite-compatible tables used by most API integration tests.
_COMMON_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
    PendingWithdrawal.__table__,
    Transaction.__table__,
]

# Modules where get_db must be patched.
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
]

# Modules where get_rate_limiter must be patched.
_RATE_LIMITER_MODULES = [
    "sthrip.services.rate_limiter",
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
]

# Modules where audit_log must be patched.
_AUDIT_LOG_MODULES = [
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
    "api.routers.payments",
    "api.routers.balance",
    "api.routers.admin",
]


@pytest.fixture(autouse=True)
def _ensure_settings_env(monkeypatch):
    """Set required env vars for Settings and clear the lru_cache between tests."""
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key-for-tests")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("WEBHOOK_ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY)

    # Clear the lru_cache so each test gets fresh Settings from its own env
    from sthrip.config import get_settings
    get_settings.cache_clear()

    # Reset the Fernet singleton so each test uses the env key above
    import sthrip.crypto as _crypto
    _crypto._fernet_instance = None

    yield

    get_settings.cache_clear()
    import sthrip.crypto as _crypto
    _crypto._fernet_instance = None


# ---------------------------------------------------------------------------
# Monero test address helper + checksum bypass for synthetic addresses
# ---------------------------------------------------------------------------

def generate_test_monero_address(network_byte: int = 24) -> str:
    """Generate a valid Monero stagenet address with correct checksum.

    Args:
        network_byte: 24 for stagenet standard (prefix '5'),
                      36 for stagenet subaddress (prefix '7').
    """
    from api.schemas import _keccak256

    _ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    _FULL_BLOCK = 8
    _FULL_ENCODED = 11
    _ENCODED_SIZES = [0, 2, 3, 5, 6, 7, 9, 10, 11]

    def _encode_block(data, target_size):
        num = int.from_bytes(data, "big")
        result = []
        for _ in range(target_size):
            num, r = divmod(num, 58)
            result.append(_ALPHABET[r])
        return "".join(reversed(result))

    payload = bytes([network_byte]) + os.urandom(32) + os.urandom(32)
    checksum = _keccak256(payload)[:4]
    raw = payload + checksum

    encoded = ""
    full_blocks = len(raw) // _FULL_BLOCK
    last_size = len(raw) % _FULL_BLOCK

    for i in range(full_blocks):
        block = raw[i * _FULL_BLOCK:(i + 1) * _FULL_BLOCK]
        encoded += _encode_block(block, _FULL_ENCODED)

    if last_size > 0:
        last_block = raw[full_blocks * _FULL_BLOCK:]
        encoded += _encode_block(last_block, _ENCODED_SIZES[last_size])

    return encoded


# ---------------------------------------------------------------------------
# Reusable database fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_engine():
    """In-memory SQLite engine with common test tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_COMMON_TEST_TABLES)
    return engine


@pytest.fixture
def db_session_factory(db_engine):
    """Session factory bound to the in-memory test engine."""
    return sessionmaker(bind=db_engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Reusable API client fixture (ledger mode, standard patches)
# ---------------------------------------------------------------------------

@pytest.fixture
def client(db_engine, db_session_factory):
    """FastAPI test client with all common dependencies mocked (HUB_MODE=ledger).

    Patches: get_db (11 modules), rate limiter, monitoring, webhook service,
    queue_webhook, audit_log, and create_tables.
    """

    @contextmanager
    def get_test_db():
        session = db_session_factory()
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

        # Database patches
        for mod in _GET_DB_MODULES:
            stack.enter_context(patch(f"{mod}.get_db", side_effect=get_test_db))
        stack.enter_context(patch("sthrip.db.database.create_tables"))

        # Rate limiter patches
        for mod in _RATE_LIMITER_MODULES:
            stack.enter_context(
                patch(f"{mod}.get_rate_limiter", return_value=mock_limiter)
            )

        # Audit log patches
        for mod in _AUDIT_LOG_MODULES:
            stack.enter_context(patch(f"{mod}.audit_log"))

        # Monitoring & webhook patches
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

        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)
