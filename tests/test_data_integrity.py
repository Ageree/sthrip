"""Tests for Phase 4 — HIGH data integrity fixes (HIGH-1 through HIGH-6)."""

import threading
from decimal import Decimal
from unittest.mock import MagicMock, patch, PropertyMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance, SystemState,
    AgentTier, RateLimitTier, PrivacyLevel,
)
from sthrip.db.repository import BalanceRepository, SystemStateRepository

_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    SystemState.__table__,
]


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=_TEST_TABLES)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def agent(db_session):
    agent = Agent(
        agent_name="test-agent",
        api_key_hash="testhash",
        tier=AgentTier.FREE,
        rate_limit_tier=RateLimitTier.STANDARD,
        privacy_level=PrivacyLevel.MEDIUM,
        is_active=True,
        xmr_address="test_address_123",
    )
    db_session.add(agent)
    db_session.flush()
    return agent


# ── HIGH-1: get_or_create race condition ──────────────────────────────────


class TestGetOrCreateRaceCondition:
    """BalanceRepository.get_or_create must handle IntegrityError on concurrent insert."""

    def test_handles_integrity_error_on_concurrent_insert(self):
        """When flush raises IntegrityError, should rollback and re-query."""
        import inspect
        source = inspect.getsource(BalanceRepository.get_or_create)
        # Verify the method has IntegrityError handling with rollback
        assert "IntegrityError" in source, "get_or_create must catch IntegrityError"
        assert "rollback" in source, "get_or_create must rollback on IntegrityError"

    def test_integrity_error_path_with_mock_session(self):
        """Verify the IntegrityError fallback path uses savepoint and returns re-queried result."""
        mock_balance = MagicMock(spec=AgentBalance)
        mock_balance.agent_id = uuid4()
        mock_balance.token = "XMR"

        mock_query = MagicMock()
        mock_filter = MagicMock()
        # First call: .first() returns None (race: not yet inserted)
        # Second call (after savepoint rollback): .first() returns the balance
        mock_filter.first.side_effect = [None, mock_balance]
        mock_query.filter.return_value = mock_filter

        mock_savepoint = MagicMock()
        mock_session = MagicMock()
        mock_session.query.return_value = mock_query
        mock_session.flush.side_effect = IntegrityError("dup", {}, None)
        mock_session.begin_nested.return_value = mock_savepoint

        repo = BalanceRepository(mock_session)
        result = repo.get_or_create(mock_balance.agent_id, "XMR")

        assert result is mock_balance
        mock_session.begin_nested.assert_called_once()
        mock_savepoint.rollback.assert_called_once()
        assert mock_session.add.called

    def test_normal_path_still_works(self, db_session, agent):
        """get_or_create still works in the normal (no race) path."""
        repo = BalanceRepository(db_session)
        balance = repo.get_or_create(agent.id)
        assert balance.agent_id == agent.id
        assert balance.token == "XMR"

    def test_returns_existing_without_create(self, db_session, agent):
        """If balance already exists, returns it without creating."""
        repo = BalanceRepository(db_session)
        b1 = repo.get_or_create(agent.id)
        b2 = repo.get_or_create(agent.id)
        assert b1 is b2


# ── HIGH-2: SystemState upsert ────────────────────────────────────────────


class TestSystemStateUpsert:
    """SystemStateRepository.set must handle concurrent upserts safely."""

    def test_set_creates_new_key(self, db_session):
        repo = SystemStateRepository(db_session)
        result = repo.set("test_key", "value1")
        assert result.key == "test_key"
        assert result.value == "value1"

    def test_set_updates_existing_key(self, db_session):
        repo = SystemStateRepository(db_session)
        repo.set("test_key", "value1")
        db_session.flush()
        result = repo.set("test_key", "value2")
        assert result.value == "value2"

    def test_set_handles_integrity_error_on_sqlite(self, db_session):
        """When concurrent insert causes IntegrityError in SQLite path,
        should rollback and update existing row."""
        repo = SystemStateRepository(db_session)

        # Pre-insert the row
        row = SystemState(key="race_key", value="original")
        db_session.add(row)
        db_session.flush()

        # The set method should handle finding the existing row and updating it
        result = repo.set("race_key", "updated")
        assert result.value == "updated"

    def test_get_returns_none_for_missing(self, db_session):
        repo = SystemStateRepository(db_session)
        assert repo.get("nonexistent") is None

    def test_get_returns_value(self, db_session):
        repo = SystemStateRepository(db_session)
        repo.set("my_key", "my_value")
        db_session.flush()
        assert repo.get("my_key") == "my_value"

    def test_set_returns_row_for_final_query(self, db_session):
        """set() should return the row from final re-query."""
        repo = SystemStateRepository(db_session)
        result = repo.set("k", "v")
        assert result is not None
        assert result.key == "k"


# ── HIGH-4: Decimal precision loss ────────────────────────────────────────


class TestDecimalPrecision:
    """wallet.transfer() and wallet_service must not lose Decimal precision."""

    def test_wallet_transfer_accepts_decimal(self):
        """MoneroWalletRPC.transfer should accept Decimal amount without float conversion."""
        from sthrip.wallet import MoneroWalletRPC

        wallet = MoneroWalletRPC.__new__(MoneroWalletRPC)
        wallet.url = "http://localhost:18082/json_rpc"
        wallet.auth = None
        wallet.timeout = 30
        wallet.headers = {"Content-Type": "application/json"}

        # Mock _call to capture the params
        captured = {}

        def fake_call(method, params=None):
            captured["params"] = params
            return {"tx_hash": "abc123", "fee": 1000000, "tx_key": "key123"}

        wallet._call = fake_call

        amount = Decimal("0.123456789012")
        wallet.transfer("dest_addr", amount)

        # Check that the atomic amount preserves precision
        dest = captured["params"]["destinations"][0]
        expected_atomic = int(Decimal("0.123456789012") * Decimal("1000000000000"))
        assert dest["amount"] == expected_atomic

    def test_wallet_service_passes_decimal_not_float(self):
        """WalletService.send_withdrawal should pass Decimal, not float(amount)."""
        from sthrip.services.wallet_service import WalletService

        mock_wallet = MagicMock()
        mock_wallet.transfer.return_value = {
            "tx_hash": "abc",
            "fee": 1000000,
        }

        mock_wallet.get_address.return_value = {
            "address": "hub_primary_addr",
            "addresses": [{"address": "hub_primary_addr", "address_index": 0}],
        }

        import threading
        service = WalletService.__new__(WalletService)
        service.wallet = mock_wallet
        service._account_index = 0
        service._hub_addr_cache = None
        service._hub_addr_cache_time = 0.0
        service._hub_addr_cache_ttl = 300
        service._hub_addr_lock = threading.Lock()

        amount = Decimal("1.123456789012")
        service.send_withdrawal("addr", amount)

        # The amount passed to wallet.transfer should be Decimal, not float
        call_kwargs = mock_wallet.transfer.call_args
        passed_amount = call_kwargs[1].get("amount") or call_kwargs[0][1] if len(call_kwargs[0]) > 1 else call_kwargs[1]["amount"]
        assert isinstance(passed_amount, Decimal), f"Expected Decimal, got {type(passed_amount)}"


# ── HIGH-5: stealth.py mark_used catching only WalletRPCError ────────────


class TestStealthMarkUsed:
    """mark_used should only catch WalletRPCError, not swallow all exceptions."""

    def test_catches_wallet_rpc_error(self):
        from sthrip.stealth import StealthAddressManager
        from sthrip.wallet import WalletRPCError

        mock_wallet = MagicMock()
        mock_wallet.get_address_index.side_effect = WalletRPCError("not found")

        mgr = StealthAddressManager.__new__(StealthAddressManager)
        mgr.wallet = mock_wallet
        mgr._cache = {}

        # Should not raise
        mgr.mark_used("some_address")

    def test_does_not_catch_other_exceptions(self):
        from sthrip.stealth import StealthAddressManager

        mock_wallet = MagicMock()
        mock_wallet.get_address_index.side_effect = RuntimeError("unexpected error")

        mgr = StealthAddressManager.__new__(StealthAddressManager)
        mgr.wallet = mock_wallet
        mgr._cache = {}

        with pytest.raises(RuntimeError, match="unexpected error"):
            mgr.mark_used("some_address")


# ── HIGH-6: idempotency singleton thread safety ──────────────────────────


class TestIdempotencySingletonThreadSafety:
    """get_idempotency_store must use a lock for thread-safe initialization."""

    def test_singleton_uses_lock(self):
        """The module must have a _store_lock for thread safety."""
        import sthrip.services.idempotency as mod
        assert hasattr(mod, "_store_lock"), (
            "idempotency module must define _store_lock for thread-safe singleton"
        )

    def test_concurrent_access_returns_same_instance(self):
        """Multiple threads calling get_idempotency_store get the same instance."""
        import sthrip.services.idempotency as mod

        mod._store = None
        orig = mod.REDIS_AVAILABLE
        results = []
        errors = []

        def get_store():
            try:
                store = mod.get_idempotency_store()
                results.append(id(store))
            except Exception as e:
                errors.append(e)

        try:
            mod.REDIS_AVAILABLE = False
            threads = [threading.Thread(target=get_store) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            mod.REDIS_AVAILABLE = orig
            mod._store = None

        assert not errors, f"Threads raised errors: {errors}"
        assert len(set(results)) == 1, "All threads must get the same store instance"


# ── Task 11: DateTime timezone consistency ─────────────────────────────


class TestDateTimeTimezoneConsistency:
    """All DateTime columns across all models must use timezone=True."""

    def test_all_datetime_columns_are_timezone_aware(self):
        from sqlalchemy import DateTime as _DT, inspect as sa_inspect

        non_tz_columns = []
        for table in Base.metadata.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, _DT):
                    if not col.type.timezone:
                        non_tz_columns.append(f"{table.name}.{col.name}")

        assert non_tz_columns == [], (
            f"DateTime columns without timezone=True: {non_tz_columns}"
        )


# ── Task 12: Missing database indexes ──────────────────────────────────


class TestRequiredIndexes:
    """Critical tables must have indexes for query performance."""

    def test_fee_collections_has_status_created_at_index(self):
        """fee_collections needs an index on (status, created_at) for filtered queries."""
        from sthrip.db.models import FeeCollection

        index_columns = set()
        for idx in FeeCollection.__table__.indexes:
            cols = tuple(c.name for c in idx.columns)
            index_columns.add(cols)

        assert ("status", "created_at") in index_columns, (
            f"fee_collections missing index on (status, created_at). "
            f"Found indexes: {index_columns}"
        )

    def test_high_volume_tables_have_required_indexes(self):
        """Tables that are queried with status filters must have appropriate indexes."""
        from sthrip.db.models import Transaction, FeeCollection

        # transactions: queried by status + created_at in admin/list views
        tx_idx_cols = [
            tuple(c.name for c in idx.columns)
            for idx in Transaction.__table__.indexes
        ]
        assert any("status" in cols and "created_at" in cols for cols in tx_idx_cols), (
            f"transactions missing composite index including (status, created_at). "
            f"Found: {tx_idx_cols}"
        )
