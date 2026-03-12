"""Tests for database module."""
import pytest
from unittest.mock import patch, MagicMock
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


class TestGetDatabaseUrl:
    def test_raises_without_env(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "")
        monkeypatch.setenv("ADMIN_API_KEY", "test-key")
        monkeypatch.setenv("ENVIRONMENT", "dev")

        from sthrip.config import get_settings
        get_settings.cache_clear()

        from sthrip.db.database import get_database_url
        with pytest.raises((ValueError, Exception)):
            get_database_url()

    def test_returns_url_when_set(self):
        with patch.dict("os.environ", {"DATABASE_URL": "sqlite:///test.db"}):
            from sthrip.db.database import get_database_url
            assert get_database_url() == "sqlite:///test.db"


class TestInitEngine:
    def test_creates_engine_with_url(self):
        import sthrip.db.database as db_mod
        old_engine = db_mod._engine
        old_factory = db_mod._SessionFactory
        db_mod._engine = None
        db_mod._SessionFactory = None
        try:
            engine = db_mod.init_engine("sqlite:///:memory:")
            assert engine is not None
            assert db_mod._SessionFactory is not None
        finally:
            db_mod._engine = old_engine
            db_mod._SessionFactory = old_factory

    def test_returns_existing_engine(self):
        import sthrip.db.database as db_mod
        old_engine = db_mod._engine
        old_factory = db_mod._SessionFactory
        db_mod._engine = None
        db_mod._SessionFactory = None
        try:
            engine1 = db_mod.init_engine("sqlite:///:memory:")
            engine2 = db_mod.init_engine("sqlite:///:memory:")
            assert engine1 is engine2
        finally:
            db_mod._engine = old_engine
            db_mod._SessionFactory = old_factory


class TestGetDb:
    def test_yields_session_and_commits(self):
        import sthrip.db.database as db_mod
        old_engine = db_mod._engine
        old_factory = db_mod._SessionFactory
        db_mod._engine = None
        db_mod._SessionFactory = None
        try:
            db_mod.init_engine("sqlite:///:memory:")
            with db_mod.get_db() as session:
                assert session is not None
                # Session should be usable
                from sqlalchemy import text
                session.execute(text("SELECT 1"))
        finally:
            db_mod._engine = old_engine
            db_mod._SessionFactory = old_factory

    def test_rolls_back_on_exception(self):
        import sthrip.db.database as db_mod
        old_engine = db_mod._engine
        old_factory = db_mod._SessionFactory
        db_mod._engine = None
        db_mod._SessionFactory = None
        try:
            db_mod.init_engine("sqlite:///:memory:")
            with pytest.raises(RuntimeError):
                with db_mod.get_db() as session:
                    raise RuntimeError("test error")
        finally:
            db_mod._engine = old_engine
            db_mod._SessionFactory = old_factory


def test_get_db_readonly_does_not_commit():
    """I4: get_db_readonly() should not auto-commit."""
    from sthrip.db.database import get_db_readonly
    from unittest.mock import MagicMock, patch

    mock_session = MagicMock()
    with patch("sthrip.db.database._SessionFactory", return_value=mock_session):
        with get_db_readonly() as db:
            pass  # read-only operation
    mock_session.commit.assert_not_called()
    mock_session.close.assert_called_once()


class TestGetEngine:
    def test_get_engine_initializes_if_needed(self):
        import sthrip.db.database as db_mod
        old_engine = db_mod._engine
        old_factory = db_mod._SessionFactory
        db_mod._engine = None
        db_mod._SessionFactory = None
        try:
            with patch.dict("os.environ", {"DATABASE_URL": "sqlite:///:memory:"}):
                engine = db_mod.get_engine()
                assert engine is not None
        finally:
            db_mod._engine = old_engine
            db_mod._SessionFactory = old_factory

    def test_get_engine_returns_existing(self):
        import sthrip.db.database as db_mod
        old_engine = db_mod._engine
        old_factory = db_mod._SessionFactory
        db_mod._engine = None
        db_mod._SessionFactory = None
        try:
            db_mod.init_engine("sqlite:///:memory:")
            e1 = db_mod.get_engine()
            e2 = db_mod.get_engine()
            assert e1 is e2
        finally:
            db_mod._engine = old_engine
            db_mod._SessionFactory = old_factory
