"""
Sthrip API v2 — App factory
"""

import os
import json
import asyncio
import logging
from contextlib import asynccontextmanager

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
from api.routers import health, agents, payments, balance, webhooks, admin
from api.admin_ui.views import setup_admin_ui

# Re-export for backward compatibility (existing tests patch these)
from api.deps import get_current_agent, verify_admin_key as _verify_admin_key  # noqa: F401


# ═══════════════════════════════════════════════════════════════════════════════
# LIFESPAN
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler"""
    print("🚀 Starting Sthrip API v2...")

    # Initialize Sentry error tracking
    try:
        import sentry_sdk
        if dsn := os.getenv("SENTRY_DSN"):
            import re

            _SENSITIVE_PATTERNS = re.compile(
                r"(api[_-]?key|password|secret|mnemonic|seed|"
                r"MONERO_RPC_PASS|ADMIN_API_KEY|BACKUP_PASSPHRASE)"
                r"['\"]?\s*[:=]\s*['\"]?([^'\"\s,}]+)",
                re.IGNORECASE,
            )

            def _scrub_event(event, hint):
                if event.get("request", {}).get("headers"):
                    headers = event["request"]["headers"]
                    for key in list(headers.keys()):
                        if "auth" in key.lower() or "api" in key.lower() or "key" in key.lower():
                            headers[key] = "[Filtered]"
                raw = json.dumps(event)
                if _SENSITIVE_PATTERNS.search(raw):
                    event = json.loads(_SENSITIVE_PATTERNS.sub(r"\1=[Filtered]", raw))
                return event

            sentry_sdk.init(
                dsn=dsn,
                environment=os.getenv("ENVIRONMENT", "production"),
                traces_sample_rate=0.1,
                profiles_sample_rate=0.1,
                before_send=_scrub_event,
            )
            logger.info("Sentry initialized")
    except ImportError:
        logger.debug("sentry-sdk not installed, error tracking disabled")

    # Validate environment variables
    _required_env = ["DATABASE_URL"]
    _optional_env = {
        "ADMIN_API_KEY": "Admin endpoints will reject all requests",
        "REDIS_URL": "Rate limiting and idempotency will use local fallback",
        "ALERT_WEBHOOK_URL": "No alert notifications will be sent",
        "CORS_ORIGINS": "CORS will block all cross-origin requests",
    }
    for var in _required_env:
        if not os.getenv(var):
            logger.warning(f"REQUIRED env var {var} is not set!")
    for var, msg in _optional_env.items():
        if not os.getenv(var):
            logger.info(f"Optional env var {var} not set — {msg}")

    # Reject known placeholder values for secrets
    _placeholder_values = {
        "change_me", "change_me_to_secure_random_string",
        "change_me_to_another_secure_random_string",
        "GENERATE_STRONG_RANDOM_KEY_HERE", "GENERATE_ANOTHER_STRONG_RANDOM_KEY",
    }
    admin_key = os.getenv("ADMIN_API_KEY", "")
    if admin_key and admin_key in _placeholder_values:
        logger.critical(
            "ADMIN_API_KEY is set to a placeholder value! "
            "Generate a real key: openssl rand -hex 32"
        )
        if os.getenv("ENVIRONMENT", "production") != "dev":
            raise SystemExit("Refusing to start with placeholder ADMIN_API_KEY")

    # Validate ENVIRONMENT value
    env = os.getenv("ENVIRONMENT", "production")
    if env not in ("dev", "staging", "production"):
        logger.critical("ENVIRONMENT must be one of: dev, staging, production. Got: %s", env)
        raise SystemExit(1)

    # Validate Monero RPC password in onchain mode
    if os.getenv("HUB_MODE", "onchain") == "onchain":
        rpc_pass = os.getenv("MONERO_RPC_PASS", "")
        if not rpc_pass or rpc_pass in ("rpc_password", "change_me", "password"):
            if os.getenv("ENVIRONMENT", "production") != "dev":
                logger.critical("MONERO_RPC_PASS is empty or placeholder — wallet is unprotected!")
                raise SystemExit(1)

    # Run database migrations
    try:
        from sthrip.db.database import get_engine
        _tables_exist = False
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1 FROM agents LIMIT 0"))
                _tables_exist = True
        except Exception:
            pass

        if _tables_exist:
            logger.info("Database tables already exist, skipping migration")
        else:
            from alembic.config import Config as AlembicConfig
            from alembic import command as alembic_command
            import pathlib

            alembic_ini = pathlib.Path(__file__).resolve().parent.parent / "alembic.ini"
            if alembic_ini.exists():
                alembic_cfg = AlembicConfig(str(alembic_ini))
                alembic_command.upgrade(alembic_cfg, "head")
                logger.info("Database migrations applied successfully")
            else:
                if os.getenv("ENVIRONMENT", "production") != "dev":
                    raise SystemExit("alembic.ini not found in production — refusing to start")
                create_tables()
                logger.info("Database tables ready (no alembic.ini found, using create_tables)")
    except SystemExit:
        raise
    except Exception as e:
        if "already exists" in str(e):
            logger.warning("Migration skipped (schema already exists): %s", e)
        elif os.getenv("ENVIRONMENT", "production") != "dev":
            logger.critical("DATABASE MIGRATION FAILED: %s", e, exc_info=True)
            raise SystemExit(f"Migration failed in production: {e}")
        else:
            logger.warning("Non-production: falling back to create_tables()")
            create_tables()

    # Start health monitoring
    hub_mode = get_hub_mode()
    monitor = setup_default_monitoring(include_wallet=(hub_mode == "onchain"))
    monitor.start_monitoring()
    logger.info("Health monitoring started")

    # Start webhook worker
    webhook_service = get_webhook_service()
    webhook_task = asyncio.create_task(webhook_service.start_worker())
    logger.info("Webhook worker started")

    # Start deposit monitor (only in onchain mode)
    deposit_monitor = create_deposit_monitor()
    deposit_task = None
    if deposit_monitor is not None:
        deposit_task = asyncio.create_task(deposit_monitor.start())
        logger.info("DepositMonitor started (onchain mode)")

    yield

    # Shutdown
    logger.info("Shutting down...")
    if deposit_monitor is not None:
        deposit_monitor.stop()
    if deposit_task is not None:
        deposit_task.cancel()
        try:
            await deposit_task
        except asyncio.CancelledError:
            pass

    monitor.stop_monitoring()
    webhook_service.stop_worker()
    await webhook_service.close()
    webhook_task.cancel()
    try:
        await webhook_task
    except asyncio.CancelledError:
        pass

    from sthrip.db.database import get_engine
    try:
        get_engine().dispose()
        logger.info("Database connections closed")
    except Exception:
        pass


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
        openapi_url=None,
    )

    @application.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error("Unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    configure_middleware(application)

    application.include_router(health.router)
    application.include_router(agents.router)
    application.include_router(payments.router)
    application.include_router(payments.escrow_router)
    application.include_router(balance.router)
    application.include_router(webhooks.router)
    application.include_router(admin.router)
    setup_admin_ui(application)

    # Custom branded docs — available in all environments
    setup_docs(application)

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
