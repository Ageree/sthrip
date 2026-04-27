"""Centralized configuration — single source of truth for all env vars."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Core
    environment: Literal["dev", "staging", "stagenet", "production"] = "production"
    log_level: str = "INFO"
    port: int = 8000

    # Database
    database_url: str = "postgresql://localhost/sthrip"
    db_pool_size: int = 10
    db_pool_overflow: int = 20

    # Redis
    redis_url: str = ""

    # Auth
    admin_api_key: str = Field(...)
    api_key_hmac_secret: str = Field(default="dev-hmac-secret-change-in-prod")
    webhook_encryption_key: str = Field(default="")

    # Monero
    hub_mode: Literal["onchain", "ledger"] = "onchain"
    monero_rpc_host: str = "127.0.0.1"
    monero_rpc_port: int = 18082
    monero_rpc_user: str = ""
    monero_rpc_pass: str = ""
    monero_rpc_use_ssl: bool = False
    monero_network: Literal["mainnet", "stagenet", "testnet"] = "stagenet"
    monero_min_confirmations: int = 10
    deposit_poll_interval: int = 30
    wallet_rpc_timeout: int = 15

    # Rate limiting
    rate_limit_fail_open: bool = False

    # CORS
    cors_origins: str = ""

    # Proxy
    # When deployed behind a reverse proxy (Railway, Fly.io, nginx, ...) this
    # MUST list the proxy IPs whose ``X-Forwarded-For`` header should be
    # trusted, or ``"*"`` to trust the immediate upstream peer.  Without this,
    # ``request.client.host`` will be the proxy edge IP and any feature that
    # binds to client IP (admin sessions, rate limits) will misbehave.
    trusted_proxy_hosts: str = "127.0.0.1"

    # Logging
    log_format: str = "text"

    # Database
    sql_echo: bool = False

    # Monitoring (optional)
    sentry_dsn: str = ""
    betterstack_token: str = ""

    # Alerting (optional)
    alert_webhook_url: str = ""

    # Bitcoin regtest (legacy CLI/swap tooling — not used by active API)
    btc_regtest_host: str = "localhost"
    btc_regtest_port: int = 18443
    btc_regtest_user: str = ""
    btc_regtest_pass: str = ""

    @field_validator("api_key_hmac_secret")
    @classmethod
    def validate_hmac_secret(cls, v: str, info) -> str:
        env = info.data.get("environment", "production")
        if env not in ("dev",) and v == "dev-hmac-secret-change-in-prod":
            raise ValueError(
                "API_KEY_HMAC_SECRET must be set to a secure random value in production"
            )
        if env not in ("dev",) and len(v) < 32:
            raise ValueError(
                "API_KEY_HMAC_SECRET must be at least 32 characters in non-dev environments"
            )
        return v

    @field_validator("admin_api_key")
    @classmethod
    def validate_admin_key(cls, v: str, info) -> str:
        env = info.data.get("environment", "production")
        if env != "dev" and v in ("change_me", "dev-admin-key", "test", ""):
            raise ValueError(
                "ADMIN_API_KEY must be set to a secure value in non-dev environments"
            )
        if env != "dev" and len(v) < 32:
            raise ValueError(
                "ADMIN_API_KEY must be at least 32 characters in non-dev environments"
            )
        return v

    @field_validator("webhook_encryption_key")
    @classmethod
    def validate_encryption_key(cls, v: str, info) -> str:
        env = info.data.get("environment", "production")
        if env not in ("dev",) and not v:
            raise ValueError(
                "WEBHOOK_ENCRYPTION_KEY must be set in production. "
                "Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        return v

    @field_validator("monero_rpc_host")
    @classmethod
    def validate_rpc_host(cls, v: str, info) -> str:
        """Validate Monero RPC host is not loopback in non-dev environments."""
        import ipaddress
        env = info.data.get("environment", "production")
        if env == "dev":
            return v
        try:
            addr = ipaddress.ip_address(v)
            if addr.is_loopback:
                raise ValueError(
                    "Monero RPC must not use loopback address in non-dev environments"
                )
        except ValueError as e:
            if "loopback" in str(e):
                raise
            # Not an IP literal (hostname), that's fine
            pass
        return v

    @field_validator("monero_network")
    @classmethod
    def validate_network(cls, v: str, info) -> str:
        env = info.data.get("environment", "production")
        if env == "production" and v != "mainnet":
            raise ValueError(
                f"MONERO_NETWORK must be 'mainnet' in production, got '{v}'"
            )
        return v

    @field_validator("monero_rpc_pass")
    @classmethod
    def validate_rpc_pass(cls, v: str, info) -> str:
        env = info.data.get("environment", "production")
        hub_mode = info.data.get("hub_mode", "onchain")
        if hub_mode == "onchain" and env != "dev" and v in (
            "",
            "rpc_password",
            "change_me",
        ):
            raise ValueError(
                "MONERO_RPC_PASS must be set when HUB_MODE=onchain in non-dev"
            )
        return v

    @model_validator(mode="after")
    def _reject_sql_echo_in_production(self) -> "Settings":
        if self.sql_echo and self.environment == "production":
            raise SystemExit("SQL_ECHO must not be enabled in production")
        return self

    model_config = {"env_prefix": "", "case_sensitive": False}


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings (singleton).

    NOTE: Settings are cached for the lifetime of the process via @lru_cache.
    Any changes to environment variables (including secret rotation for
    WEBHOOK_ENCRYPTION_KEY, API_KEY_HMAC_SECRET, ADMIN_API_KEY) require
    a process restart to take effect.  This is by design — hot-reloading
    secrets would add complexity without meaningful benefit given that
    Railway and similar platforms perform rolling restarts on redeploy.
    """
    return Settings()  # type: ignore[call-arg]  # pydantic-settings loads from env
