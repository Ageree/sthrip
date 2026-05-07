"""GCP Confidential VM payment-service deploy package.

Phase 3 Sprint 5 (kickoff of TEE migration). Holds:

* ``payment_service``  — minimal FastAPI app exposing only the payment hot path.
* ``import_guard``     — startup-time enforcement of the import allowlist.

The deploy artefacts (Dockerfile, setup-vm.sh, mtls/, README.md) live in the
same directory and are intentionally not Python imports.

> Boundary policy: this package MUST NOT import marketplace, escrow, MCP,
> SLA, reviews, channels, swaps, stablecoins, Tor, multisig, or admin UI
> code. The import_guard verifies this at runtime; the test suite verifies it
> at CI time via ``tests/test_payment_service_self_contained.py``.
"""
