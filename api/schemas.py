"""Pydantic request/response models for the Sthrip API."""

import os
import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from sthrip.services.url_validator import validate_url_target, SSRFBlockedError


class AgentRegistration(BaseModel):
    agent_name: str = Field(..., min_length=3, max_length=255, pattern=r"^[a-zA-Z0-9_-]+$")
    webhook_url: Optional[str] = None
    privacy_level: str = Field(default="medium", pattern=r"^(low|medium|high|paranoid)$")
    xmr_address: Optional[str] = None
    base_address: Optional[str] = None
    solana_address: Optional[str] = None

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            validate_url_target(v)
        except SSRFBlockedError as e:
            raise ValueError(f"webhook_url blocked: {e}")
        return v


class AgentResponse(BaseModel):
    agent_id: str
    agent_name: str
    tier: str
    api_key: str  # Shown once!
    created_at: str


class AgentSettingsUpdate(BaseModel):
    webhook_url: Optional[str] = None
    privacy_level: Optional[str] = Field(default=None, pattern=r"^(low|medium|high|paranoid)$")
    xmr_address: Optional[str] = None
    base_address: Optional[str] = None
    solana_address: Optional[str] = None

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url_setting(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            validate_url_target(v)
        except SSRFBlockedError as e:
            raise ValueError(f"webhook_url blocked: {e}")
        return v


class AgentProfileResponse(BaseModel):
    agent_name: str
    did: Optional[str]
    tier: str
    trust_score: int
    total_transactions: int
    xmr_address: Optional[str]
    base_address: Optional[str]
    verified_at: Optional[str]


class PaymentRequest(BaseModel):
    to_address: str = Field(..., description="Recipient Monero address")
    amount: float = Field(..., gt=0, description="Amount in XMR")
    memo: Optional[str] = Field(None, max_length=1000)
    privacy_level: Optional[str] = Field(None, pattern=r"^(low|medium|high|paranoid)$")
    use_hub_routing: bool = Field(False, description="Use hub routing for instant confirmation")


class HubPaymentRequest(BaseModel):
    to_agent_name: str = Field(..., min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$', description="Recipient agent name")
    amount: float = Field(..., gt=0, le=10000, description="Amount in XMR")
    memo: Optional[str] = Field(default=None, max_length=500)
    urgency: str = Field(default="normal", pattern=r"^(normal|urgent)$")


class EscrowCreateRequest(BaseModel):
    seller_address: str
    arbiter_address: Optional[str] = None
    amount: float = Field(..., gt=0)
    description: str = Field(..., min_length=1, max_length=1000)
    timeout_hours: int = Field(default=48, ge=1, le=720)


class DepositRequest(BaseModel):
    amount: Optional[float] = Field(default=None, gt=0, le=10000, description="Amount to deposit (required in ledger mode)")


class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp: str
    checks: dict


# Monero address validation
_BASE58_ALPHABET = re.compile(r"^[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]+$")
_MAINNET_PREFIXES = ("4", "8")
_STAGENET_PREFIXES = ("5", "7")
_TESTNET_PREFIXES = ("9", "B")
_NETWORK_PREFIXES = {
    "mainnet": _MAINNET_PREFIXES,
    "stagenet": _STAGENET_PREFIXES,
    "testnet": _TESTNET_PREFIXES,
}


def validate_monero_address(address: str) -> str:
    """Validate Monero address format: prefix, length, base58 alphabet, network."""
    network = os.getenv("MONERO_NETWORK", "stagenet")
    allowed = _NETWORK_PREFIXES.get(network, _MAINNET_PREFIXES + _STAGENET_PREFIXES + _TESTNET_PREFIXES)

    if not address.startswith(allowed):
        raise ValueError(f"Invalid address prefix for {network} network")
    if len(address) not in (95, 106):
        raise ValueError("Invalid Monero address length")
    if not _BASE58_ALPHABET.match(address):
        raise ValueError("Address contains invalid characters")
    return address


class WithdrawRequest(BaseModel):
    amount: float = Field(gt=0, le=10000, description="Amount to withdraw")
    address: str = Field(min_length=10, max_length=200, description="XMR address to withdraw to")

    @field_validator("address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        return validate_monero_address(v)
