"""fix all postgres enum values - add missing uppercase names

Revision ID: n5o6p7q8r9s0
Revises: m4n5o6p7q8r9
"""
from alembic import op

revision = 'n5o6p7q8r9s0'
down_revision = 'm4n5o6p7q8r9'
branch_labels = None
depends_on = None

# All enum definitions: PG name -> uppercase values
ENUMS = {
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


def upgrade():
    for enum_name, values in ENUMS.items():
        for val in values:
            op.execute(
                f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{val}'"
            )


def downgrade():
    # Can't easily remove enum values in PostgreSQL, so this is a no-op
    pass
