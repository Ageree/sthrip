"""
TSS Client for StealthPay Bridge

This module provides a Python client for the TSS gRPC service,
allowing integration with the Go-based TSS library.
"""

from .client import TSSClient
from .exceptions import TSSClientError, TSSConnectionError, TSSOperationError

__all__ = ["TSSClient", "TSSClientError", "TSSConnectionError", "TSSOperationError"]
