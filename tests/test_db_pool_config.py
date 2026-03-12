"""Tests that database uses configured pool sizes."""
import inspect

import pytest


def test_init_engine_uses_settings_pool_size():
    """init_engine must read pool_size from Settings, not hardcode."""
    from sthrip.db import database
    source = inspect.getsource(database.init_engine)
    assert "db_pool_size" in source or "settings.db_pool_size" in source, (
        "init_engine must use settings.db_pool_size instead of hardcoded value"
    )


def test_init_engine_uses_settings_max_overflow():
    """init_engine must read max_overflow from Settings, not hardcode."""
    from sthrip.db import database
    source = inspect.getsource(database.init_engine)
    assert "db_pool_overflow" in source or "settings.db_pool_overflow" in source, (
        "init_engine must use settings.db_pool_overflow instead of hardcoded value"
    )
