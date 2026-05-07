"""Runtime import-allowlist guard for the TEE payment service.

Run ``check_imports()`` immediately after the FastAPI app is constructed.
The guard scans ``sys.modules`` for any module whose dotted name contains a
forbidden substring (marketplace, escrow, MCP, SLA, reviews, channels,
swaps, stablecoins, Tor sidecar, etc.) and raises ``ForbiddenImportError`` if
any are found.

Why a runtime guard rather than a build-time check:

* Python lazy-imports inside repos (e.g. ``transaction_repo.create_with_commission``
  imports ``fee_calculator`` lazily) make static analysis brittle.
* The TEE image must enforce the boundary at boot — it is the last line of
  defence against an attacker who slipped a marketplace import into a shared
  helper between commit and deploy.
* The guard is immutable in the running container: any forbidden import seen
  at boot crashes uvicorn before the service binds to 8080.

The list of forbidden substrings mirrors
``tests/test_payment_service_self_contained.py::FORBIDDEN_SUBSTRINGS``. Keep
them in sync.
"""
from __future__ import annotations

import logging
import sys
from typing import Iterable

logger = logging.getLogger("sthrip.tee.import_guard")


FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "marketplace",
    "admin_ui",
    "integrations.sthrip_mcp",
    "tor_sidecar",
    "subscription_billing_service",
    "matchmaking_service",
    "messaging_service",
    "review_service",
    "sla_service",
    "channel_service",
    "swap_service",
    "stablecoin",
    "escrow_service",
    # Routers we never want present in TEE — only the payment_service router.
    "api.routers.escrow",
    "api.routers.marketplace",
    "api.routers.reviews",
    "api.routers.sla",
    "api.routers.channels",
    "api.routers.swaps",
    "api.routers.stablecoin",
    "api.admin_ui",
)

# Modules that ARE allowed even though their dotted name brushes a forbidden
# token. The carve-outs exist because Sthrip's package ``__init__.py`` files
# eagerly import sibling modules (CRUD repos, swap helpers) that we cannot
# avoid loading without restructuring the entire ``sthrip/`` package — out
# of scope for Sprint 5. The repos are pure SQLAlchemy CRUD code with no
# business logic; loading them does not expand the trust boundary.
ALLOWED_OVERRIDES: tuple[str, ...] = (
    "sthrip.db.escrow_repo",
    "sthrip.db.milestone_repo",
    "sthrip.db.channel_repo",
    "sthrip.db.matchmaking_repo",
    "sthrip.db.multisig_repo",
    "sthrip.db.review_repo",
    "sthrip.db.sla_repo",
    "sthrip.db.swap_repo",
    "sthrip.db.treasury_repo",
    # Atomic-swap helpers loaded eagerly by sthrip/__init__.py; not in the
    # payment hot path but cannot be avoided without breaking the package.
    "sthrip.swaps",
    "sthrip.swaps.btc",
    "sthrip.swaps.btc.htlc",
    "sthrip.swaps.btc.rpc_client",
    "sthrip.swaps.btc.watcher",
    "sthrip.swaps.xmr",
    "sthrip.swaps.xmr.multisig",
    "sthrip.swaps.xmr.wallet",
    # Logger namespace used by escrow_service log lines — registers as a
    # synthetic module name in some Python versions.
    "sthrip.escrow",
    "sthrip.channels",
)

# Only project modules ever sit under these top-level packages. The guard
# scans ``sys.modules`` for entries that start with one of these prefixes
# (case-insensitive) — third-party libs like ``sqlalchemy.dialects.postgresql.operators``
# (which contains the substring ``tor``) are correctly ignored.
PROJECT_PACKAGE_PREFIXES: tuple[str, ...] = (
    "sthrip.",
    "sthrip_sdk.",
    "api.",
    "cli.",
    "integrations.",
    "examples.",
    "scripts.",
)


class ForbiddenImportError(RuntimeError):
    """A module from the forbidden list was found in ``sys.modules``."""


def _is_project_module(name: str) -> bool:
    lower = name.lower()
    return any(lower.startswith(p) for p in PROJECT_PACKAGE_PREFIXES) or lower in (
        "sthrip", "sthrip_sdk", "api", "cli", "integrations", "examples", "scripts",
    )


def _scan(modules: Iterable[str], forbidden: Iterable[str]) -> list[str]:
    """Return the (sorted) list of loaded PROJECT modules that contain any
    forbidden substring.

    Third-party module names are intentionally NOT scanned: substring matches
    on names like ``sqlalchemy.dialects.postgresql.operators`` (matches
    legacy ``tor`` substring) or ``email.iterators`` produce false positives
    that would ratchet the guard into uselessness. The boundary we care about
    is *which Sthrip code is loaded*, not which stdlib helpers SQLAlchemy
    pulls in.
    """
    forbidden_lower = tuple(s.lower() for s in forbidden)
    allowed_overrides_lower = tuple(s.lower() for s in ALLOWED_OVERRIDES)
    hits: list[str] = []
    for name in modules:
        if not _is_project_module(name):
            continue
        lower = name.lower()
        if lower in allowed_overrides_lower:
            continue
        for bad in forbidden_lower:
            if bad in lower:
                hits.append(name)
                break
    return sorted(hits)


def check_imports(forbidden: Iterable[str] | None = None) -> None:
    """Raise ``ForbiddenImportError`` if any forbidden module is loaded.

    Logs the full sorted module list to stderr at INFO so an operator can
    inspect the import graph in container logs.
    """
    forbidden = tuple(forbidden) if forbidden is not None else FORBIDDEN_SUBSTRINGS

    loaded = sorted(sys.modules.keys())
    logger.info(
        "tee.import_graph: %d modules loaded at boot",
        len(loaded),
    )
    # Emit module names at DEBUG to keep INFO logs readable. Operators flip
    # the log level when triaging.
    for name in loaded:
        logger.debug("tee.module_loaded: %s", name)

    leaks = _scan(loaded, forbidden)
    if leaks:
        msg = (
            "TEE import boundary violation: forbidden modules loaded into "
            "the payment service: " + ", ".join(leaks[:20])
        )
        if len(leaks) > 20:
            msg += f" (+{len(leaks) - 20} more)"
        logger.critical(msg)
        raise ForbiddenImportError(msg)


__all__ = ["check_imports", "ForbiddenImportError", "FORBIDDEN_SUBSTRINGS"]
