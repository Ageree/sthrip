"""
Database connection and session management
"""

import os
import threading
from contextlib import contextmanager

from sthrip.config import get_settings
from typing import Generator, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import QueuePool

# Global engine and session factory
_engine = None
_SessionFactory = None
_engine_lock = threading.Lock()


def get_database_url() -> str:
    """Get database URL from settings. Raises if not configured."""
    url = get_settings().database_url
    if not url:
        raise ValueError("DATABASE_URL environment variable is required")
    return url


def init_engine(database_url: Optional[str] = None):
    """Initialize database engine (thread-safe with double-checked locking)."""
    global _engine, _SessionFactory

    if _engine is not None:
        return _engine

    with _engine_lock:
        if _engine is not None:
            return _engine

        url = database_url or get_database_url()
        settings = get_settings()

        _engine = create_engine(
            url,
            poolclass=QueuePool,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_pool_overflow,
            pool_pre_ping=True,  # Verify connections before using
            pool_recycle=3600,   # Recycle connections after 1 hour
            echo=settings.sql_echo,
            **({"connect_args": {
                "connect_timeout": 10,
                "options": "-c statement_timeout=30000",
            }} if "postgresql" in url else {}),
        )

        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)

        return _engine


def get_engine():
    """Get or create engine"""
    if _engine is None:
        return init_engine()
    return _engine


def create_tables():
    """Create all tables"""
    from .models import Base
    engine = get_engine()
    Base.metadata.create_all(bind=engine)


def drop_tables():
    """Drop all tables. Only allowed in dev/test environments."""
    from .models import Base
    settings = get_settings()
    if settings.environment not in ("dev", "test"):
        raise RuntimeError(
            f"drop_tables() is disabled in '{settings.environment}' environment"
        )
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Get database session as context manager"""
    if _SessionFactory is None:
        init_engine()
    
    db = _SessionFactory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def get_db_readonly() -> Generator[Session, None, None]:
    """Get read-only database session (no auto-commit, writes rejected)."""
    if _SessionFactory is None:
        init_engine()
    db = _SessionFactory()
    db.info["readonly"] = True
    db.autoflush = False
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
