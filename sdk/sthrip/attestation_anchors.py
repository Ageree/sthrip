"""Pinned TEE attestation anchors (Phase 3 Sprint 7).

This module is the SDK's source of truth for which TEE container images
are considered "known good" — the operator appends a sha256 entry here
after each Confidential VM deploy that has been audited.

Usage from the SDK
------------------
When ``Sthrip(verify_tee=True)`` is set without an explicit
``expected_image_hash``, the SDK falls back to the env var
``STHRIP_TEE_IMAGE_HASH``.  An offline verifier can additionally check
that the attested hash appears in :data:`KNOWN_GOOD_HASHES` to enforce
a *deploy-time pin* rather than a *runtime env pin*.

Operator playbook
-----------------
After ``docker push`` of a new TEE image:

1. Capture the digest:

   ::

       docker inspect gcr.io/<project>/sthrip-payment-tee:vX.Y.Z \
           --format '{{index .RepoDigests 0}}'

2. Append the digest to :data:`KNOWN_GOOD_HASHES` in the form
   ``"sha256:<64-hex>"`` and ship a new SDK release.
3. Set ``STHRIP_TEE_IMAGE_HASH`` on the production VM to the same value.

This file ships **empty** in Phase 3 Sprint 7 — the operator populates
it after the first staged deploy.  An empty list means the SDK relies
solely on the per-instance ``expected_image_hash`` parameter / env var
for image pinning.
"""
from __future__ import annotations

# Append-only list of operator-audited image digests.
#
# Format: "sha256:<64 lowercase hex chars>"
#
# Example (DO NOT uncomment without an actual audited deploy):
#
#     "sha256:0000000000000000000000000000000000000000000000000000000000000000",  # 2026-XX-XX vX.Y.Z
#
KNOWN_GOOD_HASHES: list[str] = []


def is_pinned(image_hash: str) -> bool:
    """Return True if ``image_hash`` appears in :data:`KNOWN_GOOD_HASHES`.

    Comparison is exact — ``sha256:`` prefix included.  The empty default
    returns ``False`` for every input, which the SDK callers treat as
    "no deploy-time pin enforced" (the env / kwarg pin still applies).
    """
    return image_hash in KNOWN_GOOD_HASHES
