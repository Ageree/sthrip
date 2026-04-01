"""
Repository pattern for database operations.

NOTE on immutability: ORM objects are inherently mutable (SQLAlchemy's
unit-of-work pattern requires in-place mutation for change tracking).
Balance mutations (deposit, deduct, credit) modify the ORM object directly
under row-level locking.  This is an accepted exception to the project's
immutability guidelines — all other layers pass immutable dicts/Pydantic
models.

This module is a backward-compatible re-export facade.  Each repository class
lives in its own focused module under sthrip/db/:

    _repo_base.py              — shared constants (_MAX_QUERY_LIMIT)
    agent_repo.py              — AgentRepository, _get_hmac_secret
    transaction_repo.py        — TransactionRepository
    escrow_repo.py             — EscrowRepository
    channel_repo.py            — ChannelRepository
    webhook_repo.py            — WebhookRepository
    reputation_repo.py         — ReputationRepository
    balance_repo.py            — BalanceRepository
    pending_withdrawal_repo.py — PendingWithdrawalRepository
    system_state_repo.py       — SystemStateRepository

All existing imports from sthrip.db.repository continue to work unchanged.
"""

from ._repo_base import _MAX_QUERY_LIMIT

from .agent_repo import AgentRepository, _get_hmac_secret
from .transaction_repo import TransactionRepository
from .escrow_repo import EscrowRepository
from .milestone_repo import MilestoneRepository
from .channel_repo import ChannelRepository
from .webhook_repo import WebhookRepository
from .reputation_repo import ReputationRepository
from .balance_repo import BalanceRepository
from .pending_withdrawal_repo import PendingWithdrawalRepository
from .system_state_repo import SystemStateRepository
from .multisig_repo import MultisigEscrowRepository
from .sla_repo import SLATemplateRepository, SLAContractRepository
from .review_repo import ReviewRepository
from .matchmaking_repo import MatchmakingRepository
from .recurring_repo import RecurringPaymentRepository
from .stream_repo import PaymentStreamRepository
from .conversion_repo import ConversionRepository as _ConversionRepository
from .swap_repo import SwapRepository
from .treasury_repo import TreasuryRepository
from .credit_repo import CreditRepository
from .loan_repo import LoanRepository
from .conditional_payment_repo import ConditionalPaymentRepository
from .multi_party_repo import MultiPartyRepository

# Re-export under the canonical name
ConversionRepository = _ConversionRepository

__all__ = [
    "_MAX_QUERY_LIMIT",
    "_get_hmac_secret",
    "AgentRepository",
    "TransactionRepository",
    "EscrowRepository",
    "MilestoneRepository",
    "ChannelRepository",
    "WebhookRepository",
    "ReputationRepository",
    "BalanceRepository",
    "PendingWithdrawalRepository",
    "SystemStateRepository",
    "MultisigEscrowRepository",
    "SLATemplateRepository",
    "SLAContractRepository",
    "ReviewRepository",
    "MatchmakingRepository",
    "RecurringPaymentRepository",
    "PaymentStreamRepository",
    "ConversionRepository",
    "SwapRepository",
]
