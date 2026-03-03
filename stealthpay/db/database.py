"""
Database connection and session management
"""

import os
from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import QueuePool

# Global engine and session factory
_engine = None
_SessionFactory = None


def get_database_url() -> str:
    """Get database URL from environment"""
    return os.getenv(
        "DATABASE_URL",
        "postgresql://stealthpay:stealthpay@localhost:5432/stealthpay"
    )


def init_engine(database_url: Optional[str] = None):
    """Initialize database engine"""
    global _engine, _SessionFactory
    
    if _engine is not None:
        return _engine
    
    url = database_url or get_database_url()
    
    _engine = create_engine(
        url,
        poolclass=QueuePool,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,  # Verify connections before using
        pool_recycle=3600,   # Recycle connections after 1 hour
        echo=os.getenv("SQL_ECHO", "false").lower() == "true"
    )
    
    _SessionFactory = sessionmaker(bind=_engine)
    
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
    """Drop all tables (DANGER!)"""
    from .models import Base
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


class Database:
    """Database helper class"""
    
    def __init__(self, database_url: Optional[str] = None):
        self.engine = init_engine(database_url)
        self.Session = _SessionFactory
    
    def create_tables(self):
        """Create all tables"""
        from .models import Base
        Base.metadata.create_all(bind=self.engine)
    
    def drop_tables(self):
        """Drop all tables"""
        from .models import Base
        Base.metadata.drop_all(bind=self.engine)
    
    def session(self) -> Session:
        """Get new session"""
        return self.Session()
    
    @contextmanager
    def transaction(self) -> Generator[Session, None, None]:
        """Execute in transaction"""
        with get_db() as db:
            yield db


# Initialize on import
init_engine()
