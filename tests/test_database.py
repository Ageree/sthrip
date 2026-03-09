"""Tests for database module."""
import pytest
from unittest.mock import patch, MagicMock
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


class TestGetDatabaseUrl:
    def test_raises_without_env(self):
        with patch.dict("os.environ", {}, clear=True):
            # Remove DATABASE_URL if set
            import os
            old = os.environ.pop("DATABASE_URL", None)
            try:
                from sthrip.db.database import get_database_url
                with pytest.raises(ValueError, match="DATABASE_URL"):
                    get_database_url()
            finally:
                if old:
                    os.environ["DATABASE_URL"] = old

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


class TestDatabaseClass:
    def test_init_creates_engine(self):
        import sthrip.db.database as db_mod
        old_engine = db_mod._engine
        old_factory = db_mod._SessionFactory
        db_mod._engine = None
        db_mod._SessionFactory = None
        try:
            database = db_mod.Database("sqlite:///:memory:")
            assert database.engine is not None
            assert database.Session is not None
        finally:
            db_mod._engine = old_engine
            db_mod._SessionFactory = old_factory

    def test_session_returns_new_session(self):
        import sthrip.db.database as db_mod
        old_engine = db_mod._engine
        old_factory = db_mod._SessionFactory
        db_mod._engine = None
        db_mod._SessionFactory = None
        try:
            database = db_mod.Database("sqlite:///:memory:")
            session = database.session()
            assert session is not None
            session.close()
        finally:
            db_mod._engine = old_engine
            db_mod._SessionFactory = old_factory

    def test_transaction_context(self):
        import sthrip.db.database as db_mod
        old_engine = db_mod._engine
        old_factory = db_mod._SessionFactory
        db_mod._engine = None
        db_mod._SessionFactory = None
        try:
            database = db_mod.Database("sqlite:///:memory:")
            with database.transaction() as session:
                assert session is not None
        finally:
            db_mod._engine = old_engine
            db_mod._SessionFactory = old_factory


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
