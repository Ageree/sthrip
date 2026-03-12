"""Tests that singleton factories are thread-safe."""
import importlib
import inspect

import pytest


@pytest.mark.parametrize("module_path,func_name", [
    ("sthrip.services.fee_collector", "get_fee_collector"),
    ("sthrip.services.agent_registry", "get_registry"),
    ("sthrip.services.webhook_service", "get_webhook_service"),
    ("sthrip.services.monitoring", "get_monitor"),
    ("sthrip.services.idempotency", "get_idempotency_store"),
])
def test_singleton_factory_uses_lock(module_path, func_name):
    """Each singleton factory must use a threading lock."""
    mod = importlib.import_module(module_path)
    source = inspect.getsource(getattr(mod, func_name))
    assert "_lock" in source or "Lock" in source, (
        f"{module_path}.{func_name} must use a threading Lock for thread safety"
    )
