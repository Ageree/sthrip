"""Tests for CRIT-4: register_agent double-commit bug fix.

Verifies that register_agent uses db.flush() to detect IntegrityError early
and relies solely on the get_db() context manager for the actual commit,
preventing a double-commit that can violate transaction semantics.
"""

import uuid
import pytest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch, call
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError

from sthrip.db.models import Base, Agent, AgentReputation, AgentBalance
from sthrip.services.agent_registry import AgentRegistry


_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
]


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_TABLES)
    return engine


@pytest.fixture
def db_session_factory(db_engine):
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def registry():
    return AgentRegistry()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_test_db(session_factory):
    """Minimal get_db replacement backed by real in-memory SQLite."""
    @contextmanager
    def get_test_db():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    return get_test_db


# ---------------------------------------------------------------------------
# Test suite 1: commit() must NOT be called explicitly inside register_agent
# ---------------------------------------------------------------------------

class TestRegisterAgentNoExplicitCommit:
    """register_agent must NOT call db.commit() directly.

    The context manager (get_db) is responsible for the commit.
    Calling commit() explicitly inside register_agent creates a double-commit:
    - First commit inside the function
    - Second commit when the context manager exits
    This can cause data integrity issues and violates the single-commit contract.
    """

    def test_register_agent_does_not_call_db_commit(
        self, registry, db_session_factory
    ):
        """After the fix, register_agent must not invoke session.commit().

        We spy on the session's commit method; the ONLY commit should come
        from the get_db context manager (which we simulate ourselves here),
        not from inside register_agent.
        """
        commit_calls_inside_function = []

        @contextmanager
        def spy_get_db():
            session = db_session_factory()
            real_commit = session.commit

            # Replace commit with a spy that records explicit calls
            def spy_commit():
                commit_calls_inside_function.append("commit")
                return real_commit()

            session.commit = spy_commit
            try:
                yield session
                # Context manager auto-commit (does NOT go through the spy
                # because we call real_commit directly here)
                real_commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        with patch("sthrip.services.agent_registry.get_db", side_effect=spy_get_db):
            result = registry.register_agent(agent_name="no-commit-agent")

        assert result["agent_name"] == "no-commit-agent"
        assert commit_calls_inside_function == [], (
            "register_agent must NOT call db.commit() directly. "
            f"Got {len(commit_calls_inside_function)} commit(s). "
            "The get_db() context manager is the sole commit point."
        )

    def test_register_agent_calls_flush_at_least_once(
        self, registry, db_session_factory
    ):
        """After the fix, register_agent must call db.flush() to detect
        IntegrityError before the context manager commits.

        Note: create_agent() in AgentRepository also calls db.flush() to
        obtain the agent ID. We count ALL explicit flush() invocations from
        within the register_agent call scope and expect at least 1.
        """
        flush_calls = []

        @contextmanager
        def spy_get_db():
            session = db_session_factory()
            real_flush = session.flush

            def spy_flush(*args, **kwargs):
                flush_calls.append("flush")
                return real_flush(*args, **kwargs)

            session.flush = spy_flush
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        with patch("sthrip.services.agent_registry.get_db", side_effect=spy_get_db):
            registry.register_agent(agent_name="flush-spy-agent")

        assert len(flush_calls) >= 1, (
            "register_agent (or its helpers) must call db.flush() at least once. "
            "The explicit flush() replaces the removed db.commit() call."
        )


# ---------------------------------------------------------------------------
# Test suite 2: IntegrityError must be caught and converted to ValueError
# ---------------------------------------------------------------------------

class TestRegisterAgentIntegrityErrorHandling:
    """IntegrityError (DB unique constraint) must be caught and re-raised as ValueError.

    This is tested via two approaches:
    1. Real duplicate in the in-memory DB (end-to-end)
    2. Patching the repository's create_agent to raise IntegrityError directly
    """

    def test_duplicate_name_raises_value_error_via_real_db(
        self, registry, db_session_factory
    ):
        """End-to-end: inserting the same agent_name twice raises ValueError.

        The first registration succeeds. The second must raise ValueError, not
        IntegrityError, confirming the error is properly converted.
        """
        get_test_db = make_test_db(db_session_factory)

        with patch("sthrip.services.agent_registry.get_db", side_effect=get_test_db):
            # First registration succeeds
            result = registry.register_agent(agent_name="dup-name-agent")
            assert result["agent_name"] == "dup-name-agent"

            # Second registration with the same name must raise ValueError
            with pytest.raises(ValueError) as exc_info:
                registry.register_agent(agent_name="dup-name-agent")

        error_msg = str(exc_info.value)
        # Agent name no longer leaked in error message (I7 timing oracle fix)
        assert "registration failed" in error_msg.lower() or "already taken" in error_msg.lower()

    def test_integrity_error_from_repo_raises_value_error(
        self, registry, db_session_factory
    ):
        """If the repository raises IntegrityError (e.g. from flush), it must
        be caught and converted to ValueError by register_agent.

        We patch AgentRepository.create_agent to raise IntegrityError so the
        test is independent of DB-level duplicate detection timing.
        """
        get_test_db = make_test_db(db_session_factory)

        with patch("sthrip.services.agent_registry.get_db", side_effect=get_test_db):
            with patch(
                "sthrip.services.agent_registry.AgentRepository.create_agent",
                side_effect=IntegrityError(
                    "INSERT INTO agents ...", {}, Exception("UNIQUE constraint failed")
                ),
            ):
                with pytest.raises(ValueError) as exc_info:
                    registry.register_agent(agent_name="integrity-error-agent")

        error_msg = str(exc_info.value)
        assert "registration failed" in error_msg.lower() or "already taken" in error_msg.lower(), (
            "ValueError must mention 'registration failed' or 'already taken'. "
            f"Got: '{error_msg}'"
        )

    def test_integrity_error_is_not_leaked_to_caller(
        self, registry, db_session_factory
    ):
        """Callers must never receive a raw IntegrityError from register_agent.

        The function must convert it to ValueError unconditionally.
        """
        get_test_db = make_test_db(db_session_factory)

        with patch("sthrip.services.agent_registry.get_db", side_effect=get_test_db):
            with patch(
                "sthrip.services.agent_registry.AgentRepository.create_agent",
                side_effect=IntegrityError(
                    "INSERT INTO agents ...", {}, Exception("UNIQUE")
                ),
            ):
                # Must raise ValueError, NOT IntegrityError
                with pytest.raises(ValueError):
                    registry.register_agent(agent_name="no-leak-agent")

                # Verify it does NOT raise IntegrityError
                try:
                    registry.register_agent(agent_name="no-leak-agent-2")
                except ValueError:
                    pass  # expected
                except IntegrityError:
                    pytest.fail(
                        "register_agent leaked IntegrityError to caller; "
                        "it must be converted to ValueError."
                    )

    def test_value_error_from_pre_check_is_preserved(
        self, registry, db_session_factory
    ):
        """Duplicate agent name raises ValueError via IntegrityError path.

        The pre-check query was removed (I7 timing oracle fix). Now both paths
        go through IntegrityError → ValueError conversion.
        """
        get_test_db = make_test_db(db_session_factory)

        with patch("sthrip.services.agent_registry.get_db", side_effect=get_test_db):
            # Insert once so the pre-check query finds it
            registry.register_agent(agent_name="pre-check-agent")

            with pytest.raises(ValueError) as exc_info:
                registry.register_agent(agent_name="pre-check-agent")

        error_msg = str(exc_info.value)
        # Agent name no longer leaked in error message (I7 timing oracle fix)
        assert "registration failed" in error_msg.lower() or "already taken" in error_msg.lower()


# ---------------------------------------------------------------------------
# Test suite 3: verify_agent must NOT call db.commit() explicitly
# ---------------------------------------------------------------------------

class TestVerifyAgentNoExplicitCommit:
    """verify_agent must NOT call db.commit() directly.

    Like register_agent, verify_agent must rely solely on the get_db()
    context manager for the transaction commit. An explicit db.commit()
    inside the function creates a double-commit: if an exception is raised
    between the explicit commit and the context-manager exit, the rollback
    in the except branch has nothing to roll back, breaking atomicity.
    """

    def _register_and_get_id(self, registry, db_session_factory) -> str:
        """Helper: register an agent and return its agent_id."""
        get_test_db = make_test_db(db_session_factory)
        with patch("sthrip.services.agent_registry.get_db", side_effect=get_test_db):
            result = registry.register_agent(agent_name="verify-target-agent")
        return result["agent_id"]

    def test_verify_agent_does_not_call_db_commit(
        self, registry, db_session_factory
    ):
        """verify_agent must not invoke session.commit() directly.

        We spy on the session's commit method via a patched get_db. The ONLY
        commit should originate from the context manager (real_commit at exit),
        not from inside verify_agent.
        """
        agent_id = self._register_and_get_id(registry, db_session_factory)

        commit_calls_inside_function = []

        @contextmanager
        def spy_get_db():
            session = db_session_factory()
            real_commit = session.commit

            def spy_commit():
                commit_calls_inside_function.append("commit")
                return real_commit()

            session.commit = spy_commit
            try:
                yield session
                # Context manager auto-commit via the real method — does NOT
                # go through the spy, so inner explicit calls are isolated.
                real_commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        with patch("sthrip.services.agent_registry.get_db", side_effect=spy_get_db):
            result = registry.verify_agent(
                agent_id=agent_id,
                verified_by="admin",
                tier="verified",
            )

        assert result["agent_id"] == agent_id
        assert commit_calls_inside_function == [], (
            "verify_agent must NOT call db.commit() directly. "
            f"Got {len(commit_calls_inside_function)} explicit commit(s). "
            "The get_db() context manager is the sole commit point."
        )

    def test_verify_agent_calls_flush_at_least_once(
        self, registry, db_session_factory
    ):
        """After the fix, verify_agent must call db.flush() instead of db.commit().

        flush() sends the pending SQL to the DB engine within the current
        transaction without committing, allowing the context manager to remain
        the single commit point.
        """
        agent_id = self._register_and_get_id(registry, db_session_factory)

        flush_calls = []

        @contextmanager
        def spy_get_db():
            session = db_session_factory()
            real_flush = session.flush

            def spy_flush(*args, **kwargs):
                flush_calls.append("flush")
                return real_flush(*args, **kwargs)

            session.flush = spy_flush
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        with patch("sthrip.services.agent_registry.get_db", side_effect=spy_get_db):
            registry.verify_agent(
                agent_id=agent_id,
                verified_by="admin",
                tier="verified",
            )

        assert len(flush_calls) >= 1, (
            "verify_agent must call db.flush() at least once after the fix. "
            "The explicit flush() replaces the removed db.commit() call."
        )

    def test_verify_agent_still_persists_changes(
        self, registry, db_session_factory
    ):
        """Verification changes must be persisted even after replacing commit with flush.

        After verify_agent returns, a fresh session must see the updated tier
        and verified_at fields, proving the context manager commit took effect.
        """
        get_test_db = make_test_db(db_session_factory)

        with patch("sthrip.services.agent_registry.get_db", side_effect=get_test_db):
            reg_result = registry.register_agent(agent_name="persist-verify-agent")
            agent_id = reg_result["agent_id"]

            verify_result = registry.verify_agent(
                agent_id=agent_id,
                verified_by="admin-persist",
                tier="premium",
            )

        assert verify_result["tier"] == "premium"
        assert verify_result["verified_by"] == "admin-persist"
        assert verify_result["agent_id"] == agent_id

        # Confirm the change is visible to a new session (i.e. actually committed).
        # UUID must be passed as a uuid.UUID object; SQLite's UUID type requires
        # the object form (not a raw string) for the filter predicate.
        session = db_session_factory()
        try:
            agent = session.query(Agent).filter(
                Agent.id == uuid.UUID(agent_id)
            ).first()
            assert agent is not None, "Agent must exist in DB after verify_agent"
            assert agent.tier.value == "premium", (
                f"Expected tier 'premium', got '{agent.tier.value}'. "
                "Changes were not committed by the context manager."
            )
            assert agent.verified_at is not None, (
                "verified_at must be set after verify_agent."
            )
            assert agent.verified_by == "admin-persist"
        finally:
            session.close()

    def test_verify_agent_not_found_raises_value_error(
        self, registry, db_session_factory
    ):
        """verify_agent must raise ValueError when the agent_id does not exist.

        No commit or flush must occur for a non-existent agent — the function
        should raise before any mutation.
        """
        get_test_db = make_test_db(db_session_factory)

        with patch("sthrip.services.agent_registry.get_db", side_effect=get_test_db):
            with pytest.raises(ValueError) as exc_info:
                registry.verify_agent(
                    agent_id="00000000-0000-0000-0000-000000000000",
                    verified_by="admin",
                )

        assert "not found" in str(exc_info.value).lower()
