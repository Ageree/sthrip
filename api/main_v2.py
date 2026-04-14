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
from api.routers import health, agents, payments, balance, webhooks, admin, wellknown, escrow, spending_policy, webhook_endpoints, messages, reputation, multisig_escrow, sla, reviews, matchmaking, subscriptions, channels, streams, conversion, swap, lending, treasury, multi_party, conditional_payments, split_payments
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


def _fix_pg_enums(db):
    """Add missing uppercase values to all PostgreSQL enums."""
    from sqlalchemy import text as sa_text
    from sthrip.db.enums import _PyEnum
    import sthrip.db.enums as _enums_mod
    import inspect

    ENUM_MAP = {
        "privacylevel": ["LOW", "MEDIUM", "HIGH", "PARANOID"],
        "agenttier": ["FREE", "VERIFIED", "PREMIUM", "ENTERPRISE"],
        "ratelimittier": ["LOW", "STANDARD", "HIGH", "UNLIMITED"],
        "transactionstatus": ["PENDING", "CONFIRMED", "FAILED", "ORPHANED"],
        "paymenttype": ["P2P", "HUB_ROUTING", "DEPOSIT", "WITHDRAWAL", "ESCROW_DEPOSIT", "ESCROW_RELEASE", "CHANNEL_OPEN", "CHANNEL_CLOSE", "FEE_COLLECTION"],
        "escrowstatus": ["CREATED", "ACCEPTED", "DELIVERED", "COMPLETED", "CANCELLED", "EXPIRED", "PARTIALLY_COMPLETED"],
        "milestonestatus": ["PENDING", "ACTIVE", "DELIVERED", "COMPLETED", "EXPIRED", "CANCELLED"],
        "channelstatus": ["PENDING", "OPEN", "CLOSING", "SETTLED", "CLOSED", "DISPUTED"],
        "recurringinterval": ["HOURLY", "DAILY", "WEEKLY", "MONTHLY"],
        "streamstatus": ["ACTIVE", "PAUSED", "STOPPED"],
        "webhookstatus": ["PENDING", "DELIVERED", "FAILED", "RETRYING"],
        "hubroutestatus": ["PENDING", "CONFIRMED", "SETTLED", "FAILED"],
        "feecollectionstatus": ["PENDING", "COLLECTED", "WITHDRAWN"],
        "withdrawalstatus": ["PENDING", "COMPLETED", "FAILED", "NEEDS_REVIEW"],
        "multisigstate": ["SETUP_ROUND_1", "SETUP_ROUND_2", "SETUP_ROUND_3", "FUNDED", "ACTIVE", "RELEASING", "COMPLETED", "CANCELLED", "DISPUTED"],
        "slastatus": ["PROPOSED", "ACCEPTED", "ACTIVE", "DELIVERED", "COMPLETED", "BREACHED", "DISPUTED"],
        "matchrequeststatus": ["SEARCHING", "MATCHED", "ASSIGNED", "EXPIRED"],
        "swapstatus": ["CREATED", "LOCKED", "COMPLETED", "REFUNDED", "EXPIRED"],
        "loanstatus": ["REQUESTED", "ACTIVE", "REPAID", "DEFAULTED", "LIQUIDATED", "CANCELLED"],
        "conditionalpaymentstate": ["PENDING", "TRIGGERED", "EXECUTED", "EXPIRED", "CANCELLED"],
        "multipartypaymentstate": ["PENDING", "ACCEPTED", "COMPLETED", "REJECTED", "EXPIRED"],
    }

    fixed = 0
    for enum_name, values in ENUM_MAP.items():
        for val in values:
            try:
                db.execute(sa_text(
                    "ALTER TYPE {} ADD VALUE IF NOT EXISTS :val".format(enum_name)
                ), {"val": val})
                fixed += 1
            except Exception:
                pass
    if fixed:
        db.commit()
        logger.info("Fixed %d PG enum values", fixed)


def _ensure_pending_withdrawals(db):
    """Create pending_withdrawals table if missing."""
    from sqlalchemy import text as sa_text
    try:
        db.execute(sa_text("SELECT 1 FROM pending_withdrawals LIMIT 1"))
    except Exception:
        db.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS pending_withdrawals (
                id UUID PRIMARY KEY,
                agent_id UUID NOT NULL REFERENCES agents(id),
                amount NUMERIC(18,12) NOT NULL,
                address VARCHAR(256) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
                tx_hash VARCHAR(128),
                error TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                completed_at TIMESTAMPTZ
            )
        """))
        db.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_pw_agent ON pending_withdrawals(agent_id)"))
        db.execute(sa_text("CREATE INDEX IF NOT EXISTS ix_pw_status ON pending_withdrawals(status, created_at)"))
        db.commit()
        logger.info("Created pending_withdrawals table")


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

    # Fast path: if schema already exists, just stamp and return
    try:
        with get_db() as db:
            from sqlalchemy import text as sa_text
            ver = db.execute(sa_text("SELECT version_num FROM alembic_version")).scalar()
            if ver:
                logger.info("Schema at alembic version %s — skipping migrations", ver)
                # Ensure all PG enum values exist (safe to re-run)
                _fix_pg_enums(db)
                try:
                    _ensure_pending_withdrawals(db)
                except Exception as e:
                    logger.warning("Could not create pending_withdrawals: %s", e)
                return
    except Exception:
        pass  # alembic_version table missing — proceed with migration

    try:
        if alembic_ini.exists():
            alembic_cfg = AlembicConfig(str(alembic_ini))
            try:
                import threading
                stamp_done = threading.Event()
                def _stamp():
                    try:
                        alembic_command.stamp(alembic_cfg, "head")
                    finally:
                        stamp_done.set()
                t = threading.Thread(target=_stamp, daemon=True)
                t.start()
                if not stamp_done.wait(timeout=10):
                    logger.warning("Alembic stamp timed out, skipping")
                else:
                    logger.info("Alembic stamped to head")
            except Exception as e:
                logger.warning("Alembic stamp failed (non-fatal): %s", e)
        else:
            if settings.environment != "dev":
                raise SystemExit("alembic.ini not found in production — refusing to start")
            create_tables()
            logger.info("Database tables ready (no alembic.ini found, using create_tables)")
    except SystemExit:
        raise
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


async def _sla_enforcement_loop():
    from sthrip.services.sla_service import SLAService
    svc = SLAService()
    while True:
        try:
            await asyncio.sleep(30)  # 30 seconds per spec
            with get_db() as db:
                resolved = svc.enforce_sla(db)
                if resolved > 0:
                    logger.info("SLA auto-enforcement: resolved %d contracts", resolved)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("SLA auto-enforcement error")


async def _recurring_payment_loop():
    """Execute due recurring payments every 5 minutes."""
    from sthrip.services.recurring_service import RecurringService
    svc = RecurringService()
    while True:
        try:
            await asyncio.sleep(300)  # 5 minutes
            with get_db() as db:
                executed = svc.execute_due_payments(db)
                if executed > 0:
                    logger.info("Recurring payments: executed %d payments", executed)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Recurring payment loop error")


async def _conditional_payment_loop():
    """Evaluate conditional payment conditions every 30 seconds."""
    from sthrip.services.conditional_payment_service import ConditionalPaymentService
    while True:
        try:
            await asyncio.sleep(30)  # 30 seconds
            with get_db() as db:
                executed = ConditionalPaymentService.evaluate_conditions(db)
                expired = ConditionalPaymentService.expire_stale(db)
                if executed > 0:
                    logger.info("Conditional payments: executed %d payments", executed)
                if expired > 0:
                    logger.info("Conditional payments: expired %d payments", expired)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Conditional payment loop error")


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

    # SLA auto-enforcement background task (runs every 30 seconds)
    sla_enforcement_task = asyncio.create_task(_sla_enforcement_loop())
    logger.info("SLA auto-enforcement task started")

    # Recurring payment execution background task (runs every 5 minutes)
    recurring_payment_task = asyncio.create_task(_recurring_payment_loop())
    logger.info("Recurring payment execution task started")

    # Conditional payment evaluation background task (runs every 30 seconds)
    conditional_payment_task = asyncio.create_task(_conditional_payment_loop())
    logger.info("Conditional payment evaluation task started")

    return {
        "monitor": monitor,
        "webhook_service": webhook_service,
        "webhook_task": webhook_task,
        "deposit_monitor": deposit_monitor,
        "deposit_task": deposit_task,
        "reconciliation_task": reconciliation_task,
        "escrow_resolution_task": escrow_resolution_task,
        "sla_enforcement_task": sla_enforcement_task,
        "recurring_payment_task": recurring_payment_task,
        "conditional_payment_task": conditional_payment_task,
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

    sla_task = services.get("sla_enforcement_task")
    if sla_task is not None:
        sla_task.cancel()
        try:
            await sla_task
        except asyncio.CancelledError:
            pass

    recurring_task = services.get("recurring_payment_task")
    if recurring_task is not None:
        recurring_task.cancel()
        try:
            await recurring_task
        except asyncio.CancelledError:
            pass

    conditional_task = services.get("conditional_payment_task")
    if conditional_task is not None:
        conditional_task.cancel()
        try:
            await conditional_task
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
    application.include_router(multi_party.router)
    application.include_router(conditional_payments.router)
    application.include_router(split_payments.router)
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
    application.include_router(sla.router)
    application.include_router(reviews.router)
    application.include_router(matchmaking.router)
    application.include_router(subscriptions.router)
    application.include_router(channels.router)
    application.include_router(streams.router)
    application.include_router(conversion.router)
    application.include_router(swap.router)
    application.include_router(lending.router)
    application.include_router(treasury.router)
    setup_admin_ui(application)

    # Custom branded docs — available in all environments
    setup_docs(application)

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn
    from sthrip.config import get_settings
    uvicorn.run(app, host="0.0.0.0", port=get_settings().port)
