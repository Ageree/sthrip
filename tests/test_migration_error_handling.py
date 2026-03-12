"""Tests for _run_database_migrations() error-handling precision.

After the GREEN fix the function:
- Only silences SQLAlchemy OperationalError when the message contains "already exists"
- Propagates (or exits) for OperationalError with different messages
- Propagates (or exits) for wrong exception types (RuntimeError, ValueError, etc.)
  even if their message contains "already exists"
"""

import pathlib
import logging
import pytest
from unittest.mock import patch, MagicMock

from sqlalchemy.exc import OperationalError

# Fernet key re-used from conftest so WebhookEncryption validator passes.
_TEST_ENCRYPTION_KEY = "uRWhVK_rogw9mlMJ6mYR1uCHU8zg1A0Q9TrHhHsu5jE="

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_op_error(msg: str) -> OperationalError:
    """Build a minimal SQLAlchemy OperationalError with the given message."""
    orig = Exception(msg)
    return OperationalError(statement=None, params=None, orig=orig)


def _alembic_ini_exists():
    """Patch pathlib.Path.exists() to return True (simulates alembic.ini present)."""
    return patch.object(pathlib.Path, "exists", return_value=True)


# Module-level patch targets (alembic symbols are now at module scope in main_v2)
_CFG_TARGET = "api.main_v2.AlembicConfig"
_CMD_TARGET = "api.main_v2.alembic_command"


# ---------------------------------------------------------------------------
# Fixture: full set of env vars that satisfy Settings in production mode
# ---------------------------------------------------------------------------

@pytest.fixture()
def production_env(monkeypatch):
    """Set every env var required for Settings to validate in ENVIRONMENT=production."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ADMIN_API_KEY", "a" * 32)           # not a placeholder
    monkeypatch.setenv("API_KEY_HMAC_SECRET", "b" * 32)     # not the dev default
    monkeypatch.setenv("WEBHOOK_ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY)
    monkeypatch.setenv("MONERO_RPC_HOST", "monero-wallet-rpc.railway.internal")
    monkeypatch.setenv("MONERO_RPC_PASS", "secure-rpc-password-xyz")
    monkeypatch.setenv("MONERO_NETWORK", "mainnet")
    monkeypatch.setenv("HUB_MODE", "onchain")
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/sthrip")

    from sthrip.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestMigrationSuccessCase:
    """Happy-path: upgrade() completes without error."""

    def test_successful_migration_runs_without_error(self):
        """When alembic upgrade succeeds, no exception is raised."""
        mock_cfg = MagicMock()
        mock_cmd = MagicMock()
        mock_cmd.upgrade.return_value = None

        with (
            _alembic_ini_exists(),
            patch(_CFG_TARGET, return_value=mock_cfg),
            patch(_CMD_TARGET, mock_cmd),
        ):
            from api.main_v2 import _run_database_migrations
            _run_database_migrations()  # must not raise

        mock_cmd.upgrade.assert_called_once_with(mock_cfg, "head")


# ---------------------------------------------------------------------------
# OperationalError with "already exists" — acceptable, must be swallowed
# ---------------------------------------------------------------------------

class TestOperationalErrorAlreadyExists:
    """OperationalError whose message contains 'already exists' must be caught."""

    def test_op_error_already_exists_is_caught_and_logged(self, caplog):
        """OperationalError('already exists') logs a warning and does not propagate."""
        error = _make_op_error("table foo already exists")
        mock_cfg = MagicMock()
        mock_cmd = MagicMock()
        mock_cmd.upgrade.side_effect = error

        with (
            _alembic_ini_exists(),
            patch(_CFG_TARGET, return_value=mock_cfg),
            patch(_CMD_TARGET, mock_cmd),
            caplog.at_level(logging.WARNING, logger="sthrip"),
        ):
            from api.main_v2 import _run_database_migrations
            _run_database_migrations()  # must NOT raise

        assert any("already exists" in r.message for r in caplog.records), (
            "Expected a warning log mentioning 'already exists'"
        )

    def test_op_error_already_exists_variant_relation(self, caplog):
        """OperationalError('relation X already exists') is silenced the same way."""
        error = _make_op_error('relation "agents" already exists')
        mock_cfg = MagicMock()
        mock_cmd = MagicMock()
        mock_cmd.upgrade.side_effect = error

        with (
            _alembic_ini_exists(),
            patch(_CFG_TARGET, return_value=mock_cfg),
            patch(_CMD_TARGET, mock_cmd),
            caplog.at_level(logging.WARNING, logger="sthrip"),
        ):
            from api.main_v2 import _run_database_migrations
            _run_database_migrations()  # must NOT raise


# ---------------------------------------------------------------------------
# OperationalError with unrelated messages — must NOT be swallowed
# ---------------------------------------------------------------------------

class TestOperationalErrorOtherMessages:
    """OperationalError with unrelated messages must not be silently swallowed."""

    def test_op_error_connection_refused_propagates_in_production(
        self, production_env
    ):
        """OperationalError('connection refused') causes SystemExit in production."""
        error = _make_op_error("could not connect to server: Connection refused")
        mock_cfg = MagicMock()
        mock_cmd = MagicMock()
        mock_cmd.upgrade.side_effect = error

        with (
            _alembic_ini_exists(),
            patch(_CFG_TARGET, return_value=mock_cfg),
            patch(_CMD_TARGET, mock_cmd),
        ):
            from api.main_v2 import _run_database_migrations
            with pytest.raises(SystemExit):
                _run_database_migrations()

    def test_op_error_connection_refused_not_logged_as_already_exists(
        self, production_env, caplog
    ):
        """Connection-refused OperationalError must not emit an 'already exists' warning."""
        error = _make_op_error("could not connect to server: Connection refused")
        mock_cfg = MagicMock()
        mock_cmd = MagicMock()
        mock_cmd.upgrade.side_effect = error

        with (
            _alembic_ini_exists(),
            patch(_CFG_TARGET, return_value=mock_cfg),
            patch(_CMD_TARGET, mock_cmd),
            caplog.at_level(logging.WARNING, logger="sthrip"),
        ):
            from api.main_v2 import _run_database_migrations
            try:
                _run_database_migrations()
            except SystemExit:
                pass

        assert not any(
            "already exists" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        ), "Connection-refused error must NOT be logged as 'already exists'"

    def test_op_error_disk_full_causes_system_exit_in_production(
        self, production_env
    ):
        """OperationalError('no space left on device') causes SystemExit in production."""
        error = _make_op_error("could not write to file: No space left on device")
        mock_cfg = MagicMock()
        mock_cmd = MagicMock()
        mock_cmd.upgrade.side_effect = error

        with (
            _alembic_ini_exists(),
            patch(_CFG_TARGET, return_value=mock_cfg),
            patch(_CMD_TARGET, mock_cmd),
        ):
            from api.main_v2 import _run_database_migrations
            with pytest.raises(SystemExit):
                _run_database_migrations()


# ---------------------------------------------------------------------------
# Wrong exception type with "already exists" — must NOT be swallowed
# ---------------------------------------------------------------------------

class TestWrongExceptionTypeWithAlreadyExists:
    """Non-OperationalError exceptions must not be silenced, regardless of message."""

    def test_runtime_error_already_exists_is_not_swallowed_in_production(
        self, production_env
    ):
        """RuntimeError('already exists') must not be treated as a safe migration skip."""
        error = RuntimeError("something already exists and it's broken")
        mock_cfg = MagicMock()
        mock_cmd = MagicMock()
        mock_cmd.upgrade.side_effect = error

        with (
            _alembic_ini_exists(),
            patch(_CFG_TARGET, return_value=mock_cfg),
            patch(_CMD_TARGET, mock_cmd),
        ):
            from api.main_v2 import _run_database_migrations
            # Production: RuntimeError escalates to SystemExit, not silent skip
            with pytest.raises((SystemExit, RuntimeError)):
                _run_database_migrations()

    def test_value_error_already_exists_is_not_swallowed_in_production(
        self, production_env
    ):
        """ValueError('already exists') must not be silently swallowed."""
        error = ValueError("key already exists in registry")
        mock_cfg = MagicMock()
        mock_cmd = MagicMock()
        mock_cmd.upgrade.side_effect = error

        with (
            _alembic_ini_exists(),
            patch(_CFG_TARGET, return_value=mock_cfg),
            patch(_CMD_TARGET, mock_cmd),
        ):
            from api.main_v2 import _run_database_migrations
            with pytest.raises((SystemExit, ValueError)):
                _run_database_migrations()


# ---------------------------------------------------------------------------
# Discrimination proof: type matters, not just message content
# ---------------------------------------------------------------------------

class TestAlreadyExistsCheckIsExceptionTypeBound:
    """Prove catching is scoped to OperationalError, not bare Exception."""

    def test_op_error_silenced_but_runtime_error_not_logged_as_skip(self, caplog):
        """OperationalError('already exists') is silenced; RuntimeError with the same
        message must not emit the 'Migration skipped (schema already exists)' log."""
        mock_cfg = MagicMock()

        # --- Part 1: OperationalError should be silenced ---
        op_error = _make_op_error("table agents already exists")
        mock_cmd_op = MagicMock()
        mock_cmd_op.upgrade.side_effect = op_error

        with (
            _alembic_ini_exists(),
            patch(_CFG_TARGET, return_value=mock_cfg),
            patch(_CMD_TARGET, mock_cmd_op),
            caplog.at_level(logging.WARNING, logger="sthrip"),
        ):
            from api.main_v2 import _run_database_migrations
            _run_database_migrations()  # must NOT raise

        assert any("already exists" in r.message for r in caplog.records)
        caplog.clear()

        # --- Part 2: RuntimeError must not produce the same "already exists" skip log ---
        rt_error = RuntimeError("table agents already exists")
        mock_cmd_rt = MagicMock()
        mock_cmd_rt.upgrade.side_effect = rt_error

        with (
            _alembic_ini_exists(),
            patch(_CFG_TARGET, return_value=mock_cfg),
            patch(_CMD_TARGET, mock_cmd_rt),
            patch("api.main_v2.create_tables"),  # dev fallback — no-op
            caplog.at_level(logging.WARNING, logger="sthrip"),
        ):
            _run_database_migrations()  # dev mode falls back; must not log "already exists"

        skip_logs = [
            r for r in caplog.records
            if "Migration skipped (schema already exists)" in r.message
        ]
        assert not skip_logs, (
            "RuntimeError must not trigger 'Migration skipped (schema already exists)' log; "
            f"got: {[r.message for r in skip_logs]}"
        )
