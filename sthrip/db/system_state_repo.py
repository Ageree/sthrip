"""
SystemStateRepository — key-value store for system-level state.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from . import models


class SystemStateRepository:
    """Key-value store for system-level state."""

    def __init__(self, db: Session):
        self.db = db

    def get(self, key: str) -> Optional[str]:
        """Get a system state value by key."""
        row = self.db.query(models.SystemState).filter(
            models.SystemState.key == key
        ).first()
        return row.value if row else None

    def set(self, key: str, value: str):
        """Set a system state value (upsert)."""
        is_sqlite = self.db.bind and self.db.bind.dialect.name == "sqlite"
        if is_sqlite:
            row = self.db.query(models.SystemState).filter(
                models.SystemState.key == key
            ).first()
            if row:
                row.value = value
                row.updated_at = datetime.now(timezone.utc)
            else:
                try:
                    row = models.SystemState(key=key, value=value)
                    self.db.add(row)
                    self.db.flush()
                except IntegrityError:
                    self.db.rollback()
                    row = self.db.query(models.SystemState).filter(
                        models.SystemState.key == key
                    ).first()
                    if row:
                        row.value = value
                        row.updated_at = datetime.now(timezone.utc)
        else:
            from sqlalchemy.dialects.postgresql import insert
            stmt = insert(models.SystemState).values(key=key, value=value)
            stmt = stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={"value": value, "updated_at": datetime.now(timezone.utc)},
            )
            self.db.execute(stmt)
        return self.db.query(models.SystemState).filter(
            models.SystemState.key == key
        ).first()
