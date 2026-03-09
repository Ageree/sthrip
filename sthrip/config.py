"""Centralized configuration — single source of truth for all env vars."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
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
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    admin_api_key: str = Field(...)

    # Monero
    hub_mode: Literal["onchain", "ledger"] = "onchain"
    monero_rpc_host: str = "127.0.0.1"
    monero_rpc_port: int = 18082
    monero_rpc_user: str = ""
    monero_rpc_pass: str = ""
    monero_network: Literal["mainnet", "stagenet", "testnet"] = "stagenet"
    monero_min_confirmations: int = 10
    deposit_poll_interval: int = 30

    # CORS
    cors_origins: str = ""

    # Proxy
    trusted_proxy_hosts: str = "127.0.0.1"

    # Monitoring (optional)
    sentry_dsn: str = ""
    betterstack_token: str = ""

    @field_validator("admin_api_key")
    @classmethod
    def validate_admin_key(cls, v: str, info) -> str:
        env = info.data.get("environment", "production")
        if env != "dev" and v in ("change_me", "dev-admin-key", "test", ""):
            raise ValueError(
                "ADMIN_API_KEY must be set to a secure value in non-dev environments"
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

    model_config = {"env_prefix": "", "case_sensitive": False}


@lru_cache
def get_settings() -> Settings:
    return Settings()
