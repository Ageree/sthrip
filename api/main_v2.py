"""
Sthrip API v2 — App factory
"""

import json
import asyncio
import logging
import pathlib
import time as _time
from contextlib import asynccontextmanager

from sqlalchemy.exc import OperationalError as _SqlaOperationalError, ProgrammingError as _SqlaProgrammingError

try:
    from alembic.config import Config as AlembicConfig
    from alembic import command as alembic_command
    _ALEMBIC_AVAILABLE = True
except ImportError:  # pragma: no cover
    AlembicConfig = None  # type: ignore[assignment,misc]
    alembic_command = None  # type: ignore[assignment]
    _ALEMBIC_AVAILABLE = False

from sthrip.logging_config import setup_logging
from sthrip.services.audit_logger import log_event as audit_log

setup_logging()
logger = logging.getLogger("sthrip")

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from sthrip.db.database import create_tables, get_db
from sthrip.services.monitoring import get_monitor, setup_default_monitoring
from sthrip.services.webhook_service import get_webhook_service
from sthrip.services.rate_limiter import get_rate_limiter, RateLimitExceeded

from api.middleware import configure_middleware
from api.helpers import get_hub_mode, get_wallet_service, create_deposit_monitor
from api.docs import setup_docs
from api.routers import health, agents, payments, balance, webhooks, admin, wellknown, escrow, spending_policy, webhook_endpoints, messages, reputation, multisig_escrow
from api.admin_ui.views import setup_admin_ui
from sthrip.config import get_settings

# Re-export for backward compatibility (existing tests patch these)
from api.deps import get_current_agent, verify_admin_key as _verify_admin_key  # noqa: F401


def _validate_required_env():
    """Validate that all required settings are available. Raises SystemExit on failure."""
    settings = get_settings()
    required = {"DATABASE_URL": settings.database_url, "ADMIN_API_KEY": settings.admin_api_key}
    missing = [var for var, val in required.items() if not val]
    if missing:
        for var in missing:
            logger.critical("REQUIRED setting %s is not set!", var)
        raise SystemExit(f"Missing required settings: {', '.join(missing)}")


def _init_sentry():
    """Initialize Sentry error tracking if sentry-sdk is installed and DSN is configured."""
    try:
        import sentry_sdk
        dsn = get_settings().sentry_dsn
        if dsn:
            import re

            _SENSITIVE_KEY_RE = re.compile(
                r"(auth|admin_key|api[_-]?key|password|secret|mnemonic|seed|token|"
                r"monero_rpc_pass|admin_api_key|backup_passphrase|"
                r"webhook_encryption_key|api_key_hmac_secret|hmac_secret)",
                re.IGNORECASE,
            )
            _SENSITIVE_VALUE_RE = re.compile(
                r"(api[_-]?key|password|secret|mnemonic|seed|admin_key|"
                r"MONERO_RPC_PASS|ADMIN_API_KEY|BACKUP_PASSPHRASE|"
                r"WEBHOOK_ENCRYPTION_KEY|API_KEY_HMAC_SECRET)"
                r"['\"]?\s*[:=]\s*['\"]?[^'\"\s,}]+",
                re.IGNORECASE,
            )

            def _scrub_value(obj):
                """Recursively scrub sensitive values from a data structure."""
                if isinstance(obj, dict):
                    return {
                        k: "[Filtered]" if _SENSITIVE_KEY_RE.search(k) else _scrub_value(v)
                        for k, v in obj.items()
                    }
                if isinstance(obj, list):
                    return [_scrub_value(item) for item in obj]
                if isinstance(obj, str) and _SENSITIVE_VALUE_RE.search(obj):
                    return "[Filtered]"
                return obj

            def _scrub_event(event, hint):
                import copy as _copy

                return _scrub_value(_copy.deepcopy(event))

            sentry_sdk.init(
                dsn=dsn,
                environment=get_settings().environment,
                traces_sample_rate=0.1,
                profiles_sample_rate=0.1,
                before_send=_scrub_event,
            )
            logger.info("Sentry initialized")
    except ImportError:
        logger.debug("sentry-sdk not installed, error tracking disabled")


def _validate_settings():
    """Validate environment, optional vars, placeholder secrets, and Monero RPC password."""
    settings = get_settings()

    _optional_checks = {
        "REDIS_URL": (settings.redis_url, "Rate limiting and idempotency will use local fallback"),
        "ALERT_WEBHOOK_URL": (settings.alert_webhook_url, "No alert notifications will be sent"),
        "CORS_ORIGINS": (settings.cors_origins, "CORS will block all cross-origin requests"),
    }
    for var, (value, msg) in _optional_checks.items():
        if not value:
            logger.info(f"Optional env var {var} not set — {msg}")

    # Reject known placeholder values for secrets
    _placeholder_values = {
        "change_me", "change_me_to_secure_random_string",
        "change_me_to_another_secure_random_string",
        "GENERATE_STRONG_RANDOM_KEY_HERE", "GENERATE_ANOTHER_STRONG_RANDOM_KEY",
    }
    admin_key = settings.admin_api_key
    if admin_key and admin_key in _placeholder_values:
        logger.critical(
            "ADMIN_API_KEY is set to a placeholder value! "
            "Generate a real key: openssl rand -hex 32"
        )
        if settings.environment != "dev":
            raise SystemExit("Refusing to start with placeholder ADMIN_API_KEY")

    # Validate ENVIRONMENT value
    env = settings.environment
    if env not in ("dev", "staging", "stagenet", "production"):
        logger.critical("ENVIRONMENT must be one of: dev, staging, stagenet, production. Got: %s", env)
        raise SystemExit(1)

    # Validate Monero RPC password in onchain mode
    if settings.hub_mode == "onchain":
        rpc_pass = settings.monero_rpc_pass
        if not rpc_pass or rpc_pass in ("rpc_password", "change_me", "password"):
            if settings.environment != "dev":
                logger.critical("MONERO_RPC_PASS is empty or placeholder — wallet is unprotected!")
                raise SystemExit(1)


def _run_database_migrations():
    """Run database migrations via Alembic, falling back to create_tables in dev.

    Only a SQLAlchemy OperationalError whose message contains "already exists"
    is silently accepted — it means the schema was already applied. All other
    exception types, and OperationalErrors with different messages, are treated
    as genuine failures and will either abort the process (production) or fall
    back to create_tables (dev).
    """
    settings = get_settings()
    alembic_ini = pathlib.Path(__file__).resolve().parent.parent / "alembic.ini"

    try:
        if alembic_ini.exists():
            alembic_cfg = AlembicConfig(str(alembic_ini))
            alembic_command.upgrade(alembic_cfg, "head")
            logger.info("Database migrations applied successfully")
        else:
            if settings.environment != "dev":
                raise SystemExit("alembic.ini not found in production — refusing to start")
            create_tables()
            logger.info("Database tables ready (no alembic.ini found, using create_tables)")
    except SystemExit:
        raise
    except (_SqlaOperationalError, _SqlaProgrammingError) as e:
        pgcode = getattr(getattr(e, "orig", None), "pgcode", None)
        err_str = str(e).lower()
        if pgcode == "42P07" or ("already exists" in err_str) or ("duplicate" in err_str):
            logger.warning("Migration skipped (schema already exists): %s", e)
            # Stamp alembic to prevent re-running on next startup
            try:
                if alembic_ini.exists():
                    alembic_cfg = AlembicConfig(str(alembic_ini))
                    alembic_command.stamp(alembic_cfg, "head")
                    logger.info("Stamped alembic version to 'head'")
            except Exception as stamp_err:
                logger.warning("Could not stamp alembic version: %s", stamp_err)
        elif settings.environment != "dev":
            logger.critical("DATABASE MIGRATION FAILED: %s", e, exc_info=True)
            raise SystemExit(f"Migration failed in production: {e}")
        else:
            logger.warning("Non-production: falling back to create_tables() after DB error: %s", e)
            create_tables()
    except Exception as e:
        if settings.environment != "dev":
            logger.critical("DATABASE MIGRATION FAILED: %s", e, exc_info=True)
            raise SystemExit(f"Migration failed in production: {e}")
        else:
            logger.warning("Non-production: falling back to create_tables()")
            create_tables()


def _recover_pending_withdrawals():
    """Recover stale pending withdrawals in onchain mode (non-fatal on failure)."""
    try:
        from sthrip.services.withdrawal_recovery import recover_pending_withdrawals
        from sthrip.db.repository import PendingWithdrawalRepository, BalanceRepository
        wallet_svc = get_wallet_service()
        with get_db() as db:
            pw_repo = PendingWithdrawalRepository(db)
            bal_repo = BalanceRepository(db)
            recovered = recover_pending_withdrawals(
                pw_repo=pw_repo,
                wallet_service=wallet_svc,
                balance_repo=bal_repo,
            )
            if recovered:
                logger.info("Recovered %d stale pending withdrawals", recovered)
    except Exception as e:
        logger.error("Withdrawal recovery failed (non-fatal): %s", e)


async def _escrow_resolution_loop():
    """Resolve expired escrows every 5 minutes."""
    from sthrip.services.escrow_service import EscrowService
    svc = EscrowService()
    while True:
        try:
            await asyncio.sleep(300)  # 5 minutes
            with get_db() as db:
                resolved = svc.resolve_expired(db)
                if resolved > 0:
                    logger.info("Escrow auto-resolution: resolved %d deals", resolved)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Escrow auto-resolution error")


def _startup_services(hub_mode):
    """Start health monitoring, webhook worker, and deposit monitor. Returns resources for shutdown."""
    monitor = setup_default_monitoring(include_wallet=(hub_mode == "onchain"))
    monitor.start_monitoring()
    logger.info("Health monitoring started")

    webhook_service = get_webhook_service()
    webhook_task = asyncio.create_task(webhook_service.start_worker())
    logger.info("Webhook worker started")

    deposit_monitor = create_deposit_monitor()
    deposit_task = None
    if deposit_monitor is not None:
        deposit_task = asyncio.create_task(deposit_monitor.start())
        logger.info("DepositMonitor started (onchain mode)")

    from sthrip.services.withdrawal_recovery import periodic_recovery_loop
    reconciliation_task = None
    if hub_mode == "onchain":
        wallet_svc = get_wallet_service()
        reconciliation_task = asyncio.create_task(
            periodic_recovery_loop(wallet_service=wallet_svc)
        )
        logger.info("Periodic withdrawal reconciliation started")

    # Escrow auto-resolution background task (runs every 5 minutes)
    escrow_resolution_task = asyncio.create_task(_escrow_resolution_loop())
    logger.info("Escrow auto-resolution task started")

    return {
        "monitor": monitor,
        "webhook_service": webhook_service,
        "webhook_task": webhook_task,
        "deposit_monitor": deposit_monitor,
        "deposit_task": deposit_task,
        "reconciliation_task": reconciliation_task,
        "escrow_resolution_task": escrow_resolution_task,
    }


def _populate_app_state(app: FastAPI, services: dict) -> None:
    """Store service singletons on app.state for DI via Depends().

    This is the I1 consolidation: all singletons are initialized once in the
    lifespan and attached to app.state. FastAPI DI providers in api/deps.py
    read from app.state rather than calling module-level get_*() functions on
    every request.

    The module-level get_*() functions remain available for background tasks
    and non-request contexts (backward compatibility).
    """
    from sthrip.services.rate_limiter import get_rate_limiter
    from sthrip.services.fee_collector import get_fee_collector
    from sthrip.services.agent_registry import get_registry
    from sthrip.services.idempotency import get_idempotency_store

    app.state.monitor = services["monitor"]
    app.state.webhook_service = services["webhook_service"]
    app.state.rate_limiter = get_rate_limiter()
    app.state.fee_collector = get_fee_collector()
    app.state.agent_registry = get_registry()
    app.state.idempotency_store = get_idempotency_store()


async def _shutdown_services(services):
    """Gracefully shut down all background services."""
    logger.info("Shutting down...")

    deposit_monitor = services["deposit_monitor"]
    deposit_task = services["deposit_task"]
    if deposit_monitor is not None:
        deposit_monitor.stop()
    if deposit_task is not None:
        deposit_task.cancel()
        try:
            await deposit_task
        except asyncio.CancelledError:
            pass

    reconciliation_task = services.get("reconciliation_task")
    if reconciliation_task is not None:
        reconciliation_task.cancel()
        try:
            await reconciliation_task
        except asyncio.CancelledError:
            pass

    escrow_task = services.get("escrow_resolution_task")
    if escrow_task is not None:
        escrow_task.cancel()
        try:
            await escrow_task
        except asyncio.CancelledError:
            pass

    services["monitor"].stop_monitoring()
    services["webhook_service"].stop_worker()
    await services["webhook_service"].close()
    services["webhook_task"].cancel()
    try:
        await services["webhook_task"]
    except asyncio.CancelledError:
        pass

    from sthrip.db.database import get_engine
    try:
        get_engine().dispose()
        logger.info("Database connections closed")
    except Exception as e:
        logger.warning("Failed to dispose database engine: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# LIFESPAN
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler"""
    logger.info("Starting Sthrip API v2")

    _init_sentry()
    _validate_required_env()
    _validate_settings()
    _run_database_migrations()

    hub_mode = get_hub_mode()
    if hub_mode == "onchain":
        _recover_pending_withdrawals()

    services = _startup_services(hub_mode)

    # I1 consolidation: store all service singletons on app.state so that
    # FastAPI DI providers in api/deps.py can pull them from request.app.state
    # without calling module-level get_*() on every request.
    _populate_app_state(app, services)

    yield

    await _shutdown_services(services)


# ═══════════════════════════════════════════════════════════════════════════════
# APP FACTORY
# ═══════════════════════════════════════════════════════════════════════════════

def create_app() -> FastAPI:
    application = FastAPI(
        title="Sthrip API",
        description="Production-ready anonymous payments for AI Agents",
        version="2.0.0",
        lifespan=lifespan,
        # Disable default docs — custom docs served via api.docs
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    @application.exception_handler(RateLimitExceeded)
    async def rate_limit_exception_handler(request: Request, exc: RateLimitExceeded):
        retry_after = max(1, int(exc.reset_at - _time.time()))
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded", "limit": exc.limit, "reset_at": exc.reset_at},
            headers={"Retry-After": str(retry_after)},
        )

    @application.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        # Handle RateLimitExceeded by name to be robust against class identity
        # mismatches that can occur with module reimporting
        if type(exc).__name__ == "RateLimitExceeded":
            limit = getattr(exc, "limit", None)
            reset_at = getattr(exc, "reset_at", None)
            retry_after = max(1, int((reset_at or _time.time()) - _time.time()))
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded", "limit": limit, "reset_at": reset_at},
                headers={"Retry-After": str(retry_after)},
            )
        logger.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    configure_middleware(application)

    application.include_router(health.router)
    application.include_router(wellknown.router)
    application.include_router(agents.router)
    application.include_router(payments.router)
    application.include_router(escrow.router)
    application.include_router(balance.router)
    application.include_router(webhooks.router)
    application.include_router(admin.router)
    application.include_router(spending_policy.router)
    application.include_router(webhook_endpoints.router)
    application.include_router(messages.router)
    application.include_router(reputation.router)
    application.include_router(multisig_escrow.router)
    setup_admin_ui(application)

    # Custom branded docs — available in all environments
    setup_docs(application)

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn
    from sthrip.config import get_settings
    uvicorn.run(app, host="0.0.0.0", port=get_settings().port)
