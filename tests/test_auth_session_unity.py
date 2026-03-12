"""Tests that auth and request handler share the same DB session."""
import inspect

import pytest


def test_get_current_agent_accepts_db_param():
    """get_current_agent must accept a db session parameter."""
    from api.deps import get_current_agent
    sig = inspect.signature(get_current_agent)
    assert "db" in sig.parameters, "get_current_agent must accept a 'db' parameter"


def test_get_current_agent_does_not_call_get_db_directly():
    """get_current_agent must NOT call get_db() directly — it should use injected session."""
    from api import deps
    source = inspect.getsource(deps.get_current_agent)
    assert "with get_db()" not in source, (
        "get_current_agent must use injected db session, not call get_db() directly"
    )
