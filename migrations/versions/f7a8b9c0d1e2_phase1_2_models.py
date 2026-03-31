"""Phase 1-2: spending policies, webhook endpoints, messaging, multisig escrow, ZK reputation, PoW.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-03-31
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSON

revision = "f7a8b9c0d1e2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- spending_policies ---
    op.execute("""
        CREATE TABLE IF NOT EXISTS spending_policies (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            max_per_tx NUMERIC(20, 8),
            max_per_session NUMERIC(20, 8),
            daily_limit NUMERIC(20, 8),
            allowed_agents JSON,
            blocked_agents JSON,
            require_escrow_above NUMERIC(20, 8),
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_spending_policy_agent UNIQUE (agent_id)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_spending_policies_agent_id
        ON spending_policies (agent_id)
    """)

    # --- webhook_endpoints ---
    op.execute("""
        CREATE TABLE IF NOT EXISTS webhook_endpoints (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            url VARCHAR(2048) NOT NULL,
            description VARCHAR(256),
            secret_encrypted TEXT NOT NULL,
            event_filters JSON,
            is_active BOOLEAN DEFAULT TRUE,
            failure_count INTEGER DEFAULT 0,
            disabled_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_agent_webhook_url UNIQUE (agent_id, url)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_webhook_endpoints_agent_id
        ON webhook_endpoints (agent_id)
    """)

    # --- Agent: encryption_public_key column ---
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'agents' AND column_name = 'encryption_public_key'
            ) THEN
                ALTER TABLE agents ADD COLUMN encryption_public_key TEXT;
            END IF;
        END $$
    """)

    # --- message_relays ---
    op.execute("""
        CREATE TABLE IF NOT EXISTS message_relays (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            from_agent_id UUID NOT NULL REFERENCES agents(id),
            to_agent_id UUID NOT NULL REFERENCES agents(id),
            payment_id VARCHAR(64),
            ciphertext TEXT NOT NULL,
            nonce VARCHAR(64) NOT NULL,
            sender_public_key VARCHAR(64) NOT NULL,
            size_bytes INTEGER NOT NULL,
            delivered_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_message_relays_from_agent_id
        ON message_relays (from_agent_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_message_relays_to_agent_id
        ON message_relays (to_agent_id)
    """)

    # --- AgentReputation: ZK columns ---
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'agent_reputation' AND column_name = 'reputation_commitment'
            ) THEN
                ALTER TABLE agent_reputation ADD COLUMN reputation_commitment TEXT;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'agent_reputation' AND column_name = 'reputation_blinding'
            ) THEN
                ALTER TABLE agent_reputation ADD COLUMN reputation_blinding TEXT;
            END IF;
        END $$
    """)

    # --- multisig_escrows ---
    op.execute("""
        CREATE TABLE IF NOT EXISTS multisig_escrows (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            escrow_deal_id UUID NOT NULL REFERENCES escrow_deals(id) UNIQUE,
            multisig_address VARCHAR(255),
            buyer_wallet_id VARCHAR(255),
            seller_wallet_id VARCHAR(255),
            hub_wallet_id VARCHAR(255),
            state VARCHAR(50) DEFAULT 'setup_round_1',
            fee_collected NUMERIC(20, 8) DEFAULT 0,
            funded_amount NUMERIC(20, 8),
            funded_tx_hash VARCHAR(255),
            timeout_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
    """)

    # --- multisig_rounds ---
    op.execute("""
        CREATE TABLE IF NOT EXISTS multisig_rounds (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            multisig_escrow_id UUID NOT NULL REFERENCES multisig_escrows(id),
            round_number INTEGER NOT NULL,
            participant VARCHAR(20) NOT NULL,
            multisig_info TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_multisig_round_participant
                UNIQUE (multisig_escrow_id, round_number, participant)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_multisig_rounds_escrow_id
        ON multisig_rounds (multisig_escrow_id)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS multisig_rounds")
    op.execute("DROP TABLE IF EXISTS multisig_escrows")
    op.execute("DROP TABLE IF EXISTS message_relays")
    op.execute("DROP TABLE IF EXISTS webhook_endpoints")
    op.execute("DROP TABLE IF EXISTS spending_policies")
    op.execute("""
        ALTER TABLE agents DROP COLUMN IF EXISTS encryption_public_key
    """)
    op.execute("""
        ALTER TABLE agent_reputation DROP COLUMN IF EXISTS reputation_commitment
    """)
    op.execute("""
        ALTER TABLE agent_reputation DROP COLUMN IF EXISTS reputation_blinding
    """)
