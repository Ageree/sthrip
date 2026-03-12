"""
Shared constants for all repository modules.

NOTE on immutability: ORM objects are inherently mutable (SQLAlchemy's
unit-of-work pattern requires in-place mutation for change tracking).
Balance mutations (deposit, deduct, credit) modify the ORM object directly
under row-level locking.  This is an accepted exception to the project's
immutability guidelines — all other layers pass immutable dicts/Pydantic
models.
"""

# Hard cap on rows returned by any list query to prevent accidental full-table
# scans in production.
_MAX_QUERY_LIMIT: int = 500
