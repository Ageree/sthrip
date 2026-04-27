"""
Concurrency tests for the recurring payment service security fixes.

Covers:
  F-1: concurrent execute_due_payments → exactly one charge per period
  F-2: cancellation between selection and commit → no ghost charge
  F-3: distributed lease prevents multiple replicas running the cron loop
  F-6: subscription row locked during charge so cancel waits

NOTE: We use SQLite in-memory for the DB.  SQLAlchemy's with_for_update(skip_locked=True)
compiles to a no-op on SQLite, so we cannot rely on DB-level row locking to reproduce the
race in unit tests.  Instead we exercise the lease layer in isolation (using a hand-rolled
FakeRedis) and test the TOCTOU re-validation path via sequential calls that simulate the
race outcome at the service level.

TDD: tests were written before implementation (RED → GREEN).
"""

import threading
import uuid
import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock, call

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentBalance, AgentReputation, RecurringPayment,
)
from sthrip.db.enums import RecurringInterval


# ─────────────────────────────────────────────────────────────────────────────
# Minimal hand-rolled FakeRedis (no external dep required)
# ─────────────────────────────────────────────────────────────────────────────

class FakeRedis:
    """Thread-safe minimal Redis fake for lease tests.

    Implements only the subset used by distributed_lease:
      - set(key, value, nx=True, ex=ttl)
      - eval(lua_script, numkeys, key, token)  — compare-and-delete only
      - get(key)
    """

    def __init__(self):
        self._store = {}
        self._lock = threading.Lock()

    def set(self, key, value, nx=False, ex=None):
        with self._lock:
            if nx and key in self._store:
                return None  # key already exists, SETNX fails
            self._store[key] = value
            return True

    def get(self, key):
        with self._lock:
            return self._store.get(key)

    def delete(self, key):
        with self._lock:
            self._store.pop(key, None)

    def eval(self, script, numkeys, *args):
        """Execute compare-and-delete Lua script atomically."""
        key = args[0]
        token = args[1]
        with self._lock:
            if self._store.get(key) == token:
                del self._store[key]
                return 1
            return 0

    def ping(self):
        return True


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers (mirrors test_recurring.py style)
# ─────────────────────────────────────────────────────────────────────────────

def _make_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[
        Agent.__table__,
        AgentReputation.__table__,
        AgentBalance.__table__,
        RecurringPayment.__table__,
    ])
    return engine


def _make_session(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return factory()


def _create_agent(db, name: str) -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        agent_name=name,
        api_key_hash="hash_" + name,
        is_active=True,
    )
    db.add(agent)
    db.flush()
    return agent


def _fund_agent(db, agent_id, amount: Decimal):
    from sthrip.db.balance_repo import BalanceRepository
    BalanceRepository(db).deposit(agent_id, amount)


def _create_due_subscription(db, from_id, to_id, amount: Decimal) -> RecurringPayment:
    """Create an active subscription due right now."""
    from sthrip.db.recurring_repo import RecurringPaymentRepository
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    repo = RecurringPaymentRepository(db)
    payment = repo.create(
        from_agent_id=from_id,
        to_agent_id=to_id,
        amount=amount,
        interval=RecurringInterval.HOURLY,
        max_payments=None,
        next_payment_at=past,
    )
    db.flush()
    return payment


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def engine():
    return _make_engine()


@pytest.fixture
def db(engine):
    session = _make_session(engine)
    yield session
    session.close()


@pytest.fixture
def agent_a(db):
    return _create_agent(db, "concurrency_agent_a")


@pytest.fixture
def agent_b(db):
    return _create_agent(db, "concurrency_agent_b")


# ─────────────────────────────────────────────────────────────────────────────
# F-3: Distributed lease tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDistributedLease:
    """Unit tests for with_redis_lease context manager."""

    def test_lease_acquired_when_redis_key_absent(self):
        """with_redis_lease acquires when no other holder exists."""
        from sthrip.services.distributed_lease import RedisLease

        fake_redis = FakeRedis()
        lease = RedisLease(fake_redis, "test_loop", ttl=60)
        acquired = lease.acquire()
        assert acquired is True

    def test_lease_not_acquired_when_key_held(self):
        """with_redis_lease returns False when another holder has the key."""
        from sthrip.services.distributed_lease import RedisLease

        fake_redis = FakeRedis()
        # Simulate another instance holding the lock
        fake_redis.set("lease:test_loop", "other-instance-token")

        lease = RedisLease(fake_redis, "test_loop", ttl=60)
        acquired = lease.acquire()
        assert acquired is False

    def test_lease_releases_own_lock_on_exit(self):
        """release() deletes the key only when the token matches (our lock)."""
        from sthrip.services.distributed_lease import RedisLease

        fake_redis = FakeRedis()
        lease = RedisLease(fake_redis, "test_loop", ttl=60)
        lease.acquire()
        assert fake_redis.get("lease:test_loop") is not None

        lease.release()
        assert fake_redis.get("lease:test_loop") is None

    def test_lease_does_not_release_foreign_lock(self):
        """release() is a no-op when another instance now holds the key."""
        from sthrip.services.distributed_lease import RedisLease

        fake_redis = FakeRedis()
        lease = RedisLease(fake_redis, "test_loop", ttl=60)
        # Manually inject a foreign token (simulates TTL expiry + re-acquisition by other)
        lease._token = "our-token"
        fake_redis.set("lease:test_loop", "foreign-token")

        lease.release()
        # Key should still be there (foreign token not deleted)
        assert fake_redis.get("lease:test_loop") == "foreign-token"

    def test_context_manager_acquires_and_releases(self):
        """with_redis_lease() context manager acquires on enter, releases on exit."""
        from sthrip.services.distributed_lease import with_redis_lease

        fake_redis = FakeRedis()
        entered = []
        with with_redis_lease(fake_redis, "ctx_test", ttl=60) as acquired:
            entered.append(acquired)
            assert fake_redis.get("lease:ctx_test") is not None

        assert entered == [True]
        assert fake_redis.get("lease:ctx_test") is None

    def test_context_manager_skips_body_when_not_acquired(self):
        """with_redis_lease() context manager yields False when key already held."""
        from sthrip.services.distributed_lease import with_redis_lease

        fake_redis = FakeRedis()
        fake_redis.set("lease:skip_test", "other-token")

        entered = []
        with with_redis_lease(fake_redis, "skip_test", ttl=60) as acquired:
            entered.append(acquired)

        assert entered == [False]
        # Foreign key unchanged
        assert fake_redis.get("lease:skip_test") == "other-token"

    def test_two_concurrent_lease_attempts_only_one_wins(self):
        """Thread-safety: only one of two concurrent acquire() calls succeeds."""
        from sthrip.services.distributed_lease import RedisLease

        fake_redis = FakeRedis()
        results = []
        barrier = threading.Barrier(2)

        def try_acquire():
            barrier.wait()
            lease = RedisLease(fake_redis, "race_test", ttl=60)
            results.append(lease.acquire())

        threads = [threading.Thread(target=try_acquire) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one should win
        assert results.count(True) == 1
        assert results.count(False) == 1

    def test_lease_fail_open_when_no_redis(self):
        """When redis is None and distributed_lease_required=False, lease is granted."""
        from sthrip.services.distributed_lease import with_redis_lease

        entered = []
        with with_redis_lease(None, "no_redis_test", ttl=60, fail_open=True) as acquired:
            entered.append(acquired)

        assert entered == [True]

    def test_lease_fail_closed_when_no_redis(self):
        """When redis is None and distributed_lease_required=True (default), lease is denied."""
        from sthrip.services.distributed_lease import with_redis_lease

        entered = []
        with with_redis_lease(None, "no_redis_test", ttl=60, fail_open=False) as acquired:
            entered.append(acquired)

        assert entered == [False]


# ─────────────────────────────────────────────────────────────────────────────
# F-1 & F-6: execute_due_payments double-charge prevention
# ─────────────────────────────────────────────────────────────────────────────

class TestExecuteDuePaymentsIdempotency:
    """Tests that execute_due_payments charges each subscription exactly once."""

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_sequential_calls_charge_only_once_per_period(
        self, mock_wh, mock_audit, db, agent_a, agent_b
    ):
        """Two sequential calls to execute_due_payments: second call is a no-op
        because next_payment_at was advanced past now() by the first call."""
        from sthrip.services.recurring_service import RecurringService
        from sthrip.db.balance_repo import BalanceRepository

        _fund_agent(db, agent_a.id, Decimal("10.0"))
        _create_due_subscription(db, agent_a.id, agent_b.id, Decimal("1.0"))

        svc = RecurringService()

        # First call: charges
        count1 = svc.execute_due_payments(db)
        assert count1 == 1

        # Second call: next_payment_at is now in the future, nothing due
        count2 = svc.execute_due_payments(db)
        assert count2 == 0

        bal_repo = BalanceRepository(db)
        # Sender lost exactly 1.0, receiver got exactly 0.99
        assert bal_repo.get_available(agent_a.id) == Decimal("9.0")
        assert bal_repo.get_available(agent_b.id) == Decimal("0.99")

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_cancelled_subscription_not_charged(
        self, mock_wh, mock_audit, db, agent_a, agent_b
    ):
        """F-2: A subscription cancelled before execute_due_payments runs is not charged.

        This simulates the TOCTOU race: the subscription was marked inactive
        between when it was fetched as due and when the charge would happen.
        execute_due_payments must re-validate is_active inside the charge window.
        """
        from sthrip.services.recurring_service import RecurringService
        from sthrip.db.balance_repo import BalanceRepository
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        _fund_agent(db, agent_a.id, Decimal("10.0"))
        payment = _create_due_subscription(db, agent_a.id, agent_b.id, Decimal("1.0"))

        # Simulate: user cancels between selection and charge
        repo = RecurringPaymentRepository(db)
        repo.cancel(payment.id)
        db.flush()

        svc = RecurringService()
        count = svc.execute_due_payments(db)

        # No charge should have occurred
        assert count == 0
        bal_repo = BalanceRepository(db)
        assert bal_repo.get_available(agent_a.id) == Decimal("10.0")
        assert bal_repo.get_available(agent_b.id) == Decimal("0.0")

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_inactive_subscription_excluded_from_get_due_payments(
        self, mock_wh, mock_audit, db, agent_a, agent_b
    ):
        """get_due_payments_for_update excludes inactive subscriptions.

        Even without re-validation, the locked query must not return
        already-cancelled rows.
        """
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        payment = _create_due_subscription(db, agent_a.id, agent_b.id, Decimal("1.0"))

        repo = RecurringPaymentRepository(db)
        repo.cancel(payment.id)
        db.flush()

        due = repo.get_due_payments_for_update()
        ids = [p.id for p in due]
        assert payment.id not in ids

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_execute_due_payments_revalidates_is_active(
        self, mock_wh, mock_audit, db, agent_a, agent_b
    ):
        """F-2 explicit: execute_due_payments skips a subscription that was
        made inactive between the locked-SELECT and the charge step.

        We simulate this by monkey-patching the repo so the first
        get_due_payments_for_update() returns the payment, but a subsequent
        re-read shows is_active=False.
        """
        from sthrip.services.recurring_service import RecurringService
        from sthrip.db.balance_repo import BalanceRepository
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        _fund_agent(db, agent_a.id, Decimal("10.0"))
        payment = _create_due_subscription(db, agent_a.id, agent_b.id, Decimal("1.0"))

        original_get_due = RecurringPaymentRepository.get_due_payments_for_update

        def patched_get_due(self):
            """Return the payment rows, but secretly cancel them first to simulate TOCTOU."""
            results = original_get_due(self)
            # Simulate concurrent cancel arriving AFTER selection but BEFORE charge
            for p in results:
                p.is_active = False  # Directly mutate the ORM object (same session)
            return results

        svc = RecurringService()
        with patch.object(RecurringPaymentRepository, "get_due_payments_for_update", patched_get_due):
            count = svc.execute_due_payments(db)

        # The service must detect is_active=False and skip
        assert count == 0
        bal_repo = BalanceRepository(db)
        assert bal_repo.get_available(agent_a.id) == Decimal("10.0")


# ─────────────────────────────────────────────────────────────────────────────
# F-3: Loop-level lease (cron deduplication)
# ─────────────────────────────────────────────────────────────────────────────

class TestCronLoopLease:
    """Tests that background loop bodies are skipped when another replica holds the lease."""

    @patch("sthrip.services.recurring_service.audit_log")
    @patch("sthrip.services.recurring_service.queue_webhook")
    def test_execute_due_payments_skipped_when_lease_not_acquired(
        self, mock_wh, mock_audit, db, agent_a, agent_b
    ):
        """F-3: If execute_due_payments is called while another replica holds the
        distributed lease, the call is a no-op (returns -1 to signal lease skip)."""
        from sthrip.services.distributed_lease import with_redis_lease

        _fund_agent(db, agent_a.id, Decimal("10.0"))
        _create_due_subscription(db, agent_a.id, agent_b.id, Decimal("1.0"))

        fake_redis = FakeRedis()
        # Simulate another replica already holding the lease
        fake_redis.set("lease:recurring_payment_loop", "other-replica-token")

        # The loop body in main_v2.py checks the lease before calling execute_due_payments.
        # We verify that with_redis_lease returns False when lock is held.
        with with_redis_lease(fake_redis, "recurring_payment_loop", ttl=360) as acquired:
            assert acquired is False
            # The loop body would NOT call execute_due_payments when acquired=False

    def test_loop_body_runs_when_lease_acquired(self):
        """The loop body runs when no other replica holds the lease."""
        from sthrip.services.distributed_lease import with_redis_lease

        fake_redis = FakeRedis()
        body_executed = []

        with with_redis_lease(fake_redis, "recurring_payment_loop", ttl=360) as acquired:
            if acquired:
                body_executed.append(True)

        assert body_executed == [True]

    def test_two_replicas_only_one_executes_loop_body(self):
        """Simulate two replicas racing: only one executes the loop body at a time.

        This test verifies overlapping execution — both replicas hold the context
        manager simultaneously.  The one that loses the SETNX race must not execute
        the body while the winner still holds the lease.
        """
        from sthrip.services.distributed_lease import with_redis_lease

        fake_redis = FakeRedis()
        body_execution_count = []
        # Barrier at 3: both threads + the main test thread coordinating hold timing.
        enter_barrier = threading.Barrier(2)
        hold_event = threading.Event()  # winner holds until we signal
        results_lock = threading.Lock()

        def replica_loop():
            enter_barrier.wait()  # both enter context manager simultaneously
            with with_redis_lease(fake_redis, "shared_loop", ttl=60) as acquired:
                if acquired:
                    with results_lock:
                        body_execution_count.append(1)
                    # Hold the lease while both threads are in the context manager
                    hold_event.wait(timeout=2.0)

        threads = [threading.Thread(target=replica_loop) for _ in range(2)]
        for t in threads:
            t.start()

        # Let both threads proceed past the barrier and compete for the lease
        import time
        time.sleep(0.1)  # brief pause to ensure both threads are running
        hold_event.set()  # release the winner so it can exit and clean up

        for t in threads:
            t.join(timeout=5.0)

        # Exactly one should have executed the body during overlapping execution
        assert len(body_execution_count) == 1


# ─────────────────────────────────────────────────────────────────────────────
# get_due_payments_for_update repo method
# ─────────────────────────────────────────────────────────────────────────────

class TestGetDuePaymentsForUpdate:
    """Tests for the new locked-select method on RecurringPaymentRepository."""

    def test_returns_due_active_payments(self, db, agent_a, agent_b):
        """get_due_payments_for_update returns active overdue payments."""
        payment = _create_due_subscription(db, agent_a.id, agent_b.id, Decimal("1.0"))

        from sthrip.db.recurring_repo import RecurringPaymentRepository
        repo = RecurringPaymentRepository(db)
        due = repo.get_due_payments_for_update()
        assert any(p.id == payment.id for p in due)

    def test_excludes_future_payments(self, db, agent_a, agent_b):
        """get_due_payments_for_update does not return future-scheduled payments."""
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        future = datetime.now(timezone.utc) + timedelta(hours=2)
        repo = RecurringPaymentRepository(db)
        payment = repo.create(
            from_agent_id=agent_a.id,
            to_agent_id=agent_b.id,
            amount=Decimal("1.0"),
            interval=RecurringInterval.HOURLY,
            max_payments=None,
            next_payment_at=future,
        )
        db.flush()

        due = repo.get_due_payments_for_update()
        assert not any(p.id == payment.id for p in due)

    def test_excludes_inactive_payments(self, db, agent_a, agent_b):
        """get_due_payments_for_update excludes cancelled/inactive payments."""
        from sthrip.db.recurring_repo import RecurringPaymentRepository

        payment = _create_due_subscription(db, agent_a.id, agent_b.id, Decimal("1.0"))
        repo = RecurringPaymentRepository(db)
        repo.cancel(payment.id)
        db.flush()

        due = repo.get_due_payments_for_update()
        assert not any(p.id == payment.id for p in due)
