"""
Pytest configuration for integration tests
"""

import pytest


def pytest_configure(config):
    """Configure pytest markers"""
    config.addinivalue_line("markers", "integration: mark test as integration test")


def pytest_collection_modifyitems(config, items):
    """Modify test collection"""
    skip_integration = pytest.mark.skip(reason="Integration test - use --integration to run")
    
    if not config.getoption("--integration"):
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_integration)


def pytest_addoption(parser):
    """Add custom command line options"""
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests (requires running nodes)"
    )
    parser.addoption(
        "--bitcoin-network",
        default="regtest",
        choices=["regtest", "testnet", "mainnet"],
        help="Bitcoin network for tests"
    )
    parser.addoption(
        "--monero-network",
        default="stagenet",
        choices=["stagenet", "testnet", "mainnet"],
        help="Monero network for tests"
    )
