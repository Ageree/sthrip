"""Tests for SQLAlchemy model defaults — timezone-aware timestamps."""


def _call_column_default(column):
    """Call a column's default callable, handling SQLAlchemy context arg."""
    default = column.default
    assert default is not None and default.is_callable, (
        f"Column {column.name} must have a callable default"
    )
    # SQLAlchemy may pass a context dict to the callable
    try:
        return default.arg({})
    except TypeError:
        return default.arg()


def test_system_state_updated_at_is_timezone_aware():
    """SystemState.updated_at default must produce timezone-aware datetime."""
    from sthrip.db.models import SystemState

    col = SystemState.__table__.columns["updated_at"]
    val = _call_column_default(col)
    assert val.tzinfo is not None, "updated_at default produces naive datetime"


def test_agent_balance_timestamps_are_timezone_aware():
    """AgentBalance.created_at and updated_at must produce tz-aware datetimes."""
    from sthrip.db.models import AgentBalance

    for col_name in ("created_at", "updated_at"):
        col = AgentBalance.__table__.columns[col_name]
        val = _call_column_default(col)
        assert val.tzinfo is not None, f"{col_name} default produces naive datetime"
