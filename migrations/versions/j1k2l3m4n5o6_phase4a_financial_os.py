"""Phase 4a: Financial OS -- treasury, credit/lending, conditional, multi-party payments.

Creates:
  - loanstatus, conditionalpaymentstate, multipartypaymentstate enums (PostgreSQL only)
  - treasury_policies table
  - treasury_forecasts table
  - treasury_rebalance_log table
  - agent_credit_scores table
  - lending_offers table
  - agent_loans table
  - conditional_payments table
  - multi_party_payments table
  - multi_party_recipients table

Revision ID: j1k2l3m4n5o6
Revises: i0j1k2l3m4n5
Create Date: 2026-04-01
"""
from alembic import op
import sqlalchemy as sa

revision = "j1k2l3m4n5o6"
down_revision = "i0j1k2l3m4n5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # ------------------------------------------------------------------
    # 1. Enum types (PostgreSQL only)
    # ------------------------------------------------------------------
    if is_pg:
        op.execute(
            "DO $$ BEGIN "
            "CREATE TYPE loanstatus AS ENUM "
            "('requested', 'active', 'repaid', 'defaulted', 'liquidated', 'cancelled'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
        op.execute(
            "DO $$ BEGIN "
            "CREATE TYPE conditionalpaymentstate AS ENUM "
            "('pending', 'triggered', 'executed', 'expired', 'cancelled'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
        op.execute(
            "DO $$ BEGIN "
            "CREATE TYPE multipartypaymentstate AS ENUM "
            "('pending', 'accepted', 'completed', 'rejected', 'expired'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )

    # ------------------------------------------------------------------
    # 2. treasury_policies
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS treasury_policies (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                target_allocation JSON NOT NULL,
                rebalance_threshold_pct INTEGER DEFAULT 10,
                rebalance_cooldown_secs INTEGER DEFAULT 300,
                min_liquid_xmr NUMERIC(20, 8),
                min_liquid_xusd NUMERIC(20, 8),
                emergency_reserve_pct INTEGER DEFAULT 10,
                auto_lend_enabled BOOLEAN DEFAULT false,
                max_lend_pct INTEGER DEFAULT 20,
                min_borrower_trust_score INTEGER DEFAULT 70,
                max_loan_duration_secs INTEGER DEFAULT 3600,
                is_active BOOLEAN DEFAULT true,
                last_rebalance_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now(),
                CONSTRAINT uq_treasury_policy_agent UNIQUE (agent_id)
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_treasury_policies_agent_id
            ON treasury_policies (agent_id)
        """)
    else:
        op.create_table(
            "treasury_policies",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("agent_id", sa.String(36),
                      sa.ForeignKey("agents.id", ondelete="CASCADE"),
                      nullable=False, unique=True, index=True),
            sa.Column("target_allocation", sa.JSON(), nullable=False),
            sa.Column("rebalance_threshold_pct", sa.Integer(), server_default="10"),
            sa.Column("rebalance_cooldown_secs", sa.Integer(), server_default="300"),
            sa.Column("min_liquid_xmr", sa.Numeric(20, 8), nullable=True),
            sa.Column("min_liquid_xusd", sa.Numeric(20, 8), nullable=True),
            sa.Column("emergency_reserve_pct", sa.Integer(), server_default="10"),
            sa.Column("auto_lend_enabled", sa.Boolean(), server_default="0"),
            sa.Column("max_lend_pct", sa.Integer(), server_default="20"),
            sa.Column("min_borrower_trust_score", sa.Integer(), server_default="70"),
            sa.Column("max_loan_duration_secs", sa.Integer(), server_default="3600"),
            sa.Column("is_active", sa.Boolean(), server_default="1"),
            sa.Column("last_rebalance_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )

    # ------------------------------------------------------------------
    # 3. treasury_forecasts
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS treasury_forecasts (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                agent_id UUID NOT NULL REFERENCES agents(id),
                forecast_type VARCHAR(50) NOT NULL,
                source_id UUID NOT NULL,
                expected_amount NUMERIC(20, 8) NOT NULL,
                expected_currency VARCHAR(10) NOT NULL DEFAULT 'XMR',
                direction VARCHAR(10) NOT NULL,
                expected_at TIMESTAMPTZ NOT NULL,
                confidence NUMERIC(3, 2) NOT NULL DEFAULT 1.00,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_treasury_forecasts_agent_id
            ON treasury_forecasts (agent_id)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_treasury_forecasts_agent_expected
            ON treasury_forecasts (agent_id, expected_at)
        """)
    else:
        op.create_table(
            "treasury_forecasts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("agent_id", sa.String(36),
                      sa.ForeignKey("agents.id"), nullable=False, index=True),
            sa.Column("forecast_type", sa.String(50), nullable=False),
            sa.Column("source_id", sa.String(36), nullable=False),
            sa.Column("expected_amount", sa.Numeric(20, 8), nullable=False),
            sa.Column("expected_currency", sa.String(10), nullable=False, server_default="XMR"),
            sa.Column("direction", sa.String(10), nullable=False),
            sa.Column("expected_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("confidence", sa.Numeric(3, 2), nullable=False, server_default="1.00"),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )
        op.create_index(
            "ix_treasury_forecasts_agent_expected",
            "treasury_forecasts",
            ["agent_id", "expected_at"],
        )

    # ------------------------------------------------------------------
    # 4. treasury_rebalance_log
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS treasury_rebalance_log (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                agent_id UUID NOT NULL REFERENCES agents(id),
                trigger VARCHAR(50) NOT NULL,
                conversions JSON NOT NULL DEFAULT '[]',
                pre_allocation JSON NOT NULL,
                post_allocation JSON NOT NULL,
                total_value_xusd NUMERIC(20, 8) NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_treasury_rebalance_log_agent_id
            ON treasury_rebalance_log (agent_id)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_treasury_rebalance_agent_created
            ON treasury_rebalance_log (agent_id, created_at)
        """)
    else:
        op.create_table(
            "treasury_rebalance_log",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("agent_id", sa.String(36),
                      sa.ForeignKey("agents.id"), nullable=False, index=True),
            sa.Column("trigger", sa.String(50), nullable=False),
            sa.Column("conversions", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("pre_allocation", sa.JSON(), nullable=False),
            sa.Column("post_allocation", sa.JSON(), nullable=False),
            sa.Column("total_value_xusd", sa.Numeric(20, 8), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )
        op.create_index(
            "ix_treasury_rebalance_agent_created",
            "treasury_rebalance_log",
            ["agent_id", "created_at"],
        )

    # ------------------------------------------------------------------
    # 5. agent_credit_scores
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS agent_credit_scores (
                agent_id UUID PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
                credit_score INTEGER DEFAULT 0,
                total_loans_taken INTEGER DEFAULT 0,
                total_loans_repaid INTEGER DEFAULT 0,
                total_loans_defaulted INTEGER DEFAULT 0,
                total_borrowed_volume NUMERIC(20, 8) DEFAULT 0,
                avg_repayment_time_secs INTEGER,
                longest_default_secs INTEGER,
                max_borrow_amount NUMERIC(20, 8) DEFAULT 0,
                max_concurrent_loans INTEGER DEFAULT 0,
                calculated_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)
    else:
        op.create_table(
            "agent_credit_scores",
            sa.Column("agent_id", sa.String(36),
                      sa.ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("credit_score", sa.Integer(), server_default="0"),
            sa.Column("total_loans_taken", sa.Integer(), server_default="0"),
            sa.Column("total_loans_repaid", sa.Integer(), server_default="0"),
            sa.Column("total_loans_defaulted", sa.Integer(), server_default="0"),
            sa.Column("total_borrowed_volume", sa.Numeric(20, 8), server_default="0"),
            sa.Column("avg_repayment_time_secs", sa.Integer(), nullable=True),
            sa.Column("longest_default_secs", sa.Integer(), nullable=True),
            sa.Column("max_borrow_amount", sa.Numeric(20, 8), server_default="0"),
            sa.Column("max_concurrent_loans", sa.Integer(), server_default="0"),
            sa.Column("calculated_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )

    # ------------------------------------------------------------------
    # 6. lending_offers
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS lending_offers (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                lender_id UUID NOT NULL REFERENCES agents(id),
                max_amount NUMERIC(20, 8) NOT NULL,
                currency VARCHAR(10) DEFAULT 'XMR',
                interest_rate_bps INTEGER NOT NULL,
                max_duration_secs INTEGER NOT NULL,
                min_borrower_credit_score INTEGER DEFAULT 0,
                require_collateral BOOLEAN DEFAULT false,
                collateral_ratio_pct INTEGER DEFAULT 100,
                is_active BOOLEAN DEFAULT true,
                remaining_amount NUMERIC(20, 8) NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_lending_offers_lender_id
            ON lending_offers (lender_id)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_lending_offers_active
            ON lending_offers (is_active, currency)
        """)
    else:
        op.create_table(
            "lending_offers",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("lender_id", sa.String(36),
                      sa.ForeignKey("agents.id"), nullable=False, index=True),
            sa.Column("max_amount", sa.Numeric(20, 8), nullable=False),
            sa.Column("currency", sa.String(10), server_default="XMR"),
            sa.Column("interest_rate_bps", sa.Integer(), nullable=False),
            sa.Column("max_duration_secs", sa.Integer(), nullable=False),
            sa.Column("min_borrower_credit_score", sa.Integer(), server_default="0"),
            sa.Column("require_collateral", sa.Boolean(), server_default="0"),
            sa.Column("collateral_ratio_pct", sa.Integer(), server_default="100"),
            sa.Column("is_active", sa.Boolean(), server_default="1"),
            sa.Column("remaining_amount", sa.Numeric(20, 8), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_lending_offers_active",
            "lending_offers",
            ["is_active", "currency"],
        )

    # ------------------------------------------------------------------
    # 7. agent_loans
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS agent_loans (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                loan_hash VARCHAR(64) NOT NULL UNIQUE,
                lender_id UUID NOT NULL REFERENCES agents(id),
                borrower_id UUID NOT NULL REFERENCES agents(id),
                principal NUMERIC(20, 8) NOT NULL,
                currency VARCHAR(10) DEFAULT 'XMR',
                interest_rate_bps INTEGER NOT NULL,
                duration_secs INTEGER NOT NULL,
                collateral_amount NUMERIC(20, 8) DEFAULT 0,
                collateral_currency VARCHAR(10),
                repayment_amount NUMERIC(20, 8) NOT NULL,
                repaid_amount NUMERIC(20, 8) DEFAULT 0,
                state loanstatus DEFAULT 'requested' NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                grace_period_secs INTEGER DEFAULT 300,
                requested_at TIMESTAMPTZ DEFAULT now(),
                funded_at TIMESTAMPTZ,
                repaid_at TIMESTAMPTZ,
                defaulted_at TIMESTAMPTZ,
                platform_fee NUMERIC(20, 8) DEFAULT 0
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_agent_loans_lender_id
            ON agent_loans (lender_id)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_agent_loans_borrower_id
            ON agent_loans (borrower_id)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_agent_loans_state
            ON agent_loans (state)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_agent_loans_expires
            ON agent_loans (state, expires_at)
        """)
    else:
        op.create_table(
            "agent_loans",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("loan_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("lender_id", sa.String(36),
                      sa.ForeignKey("agents.id"), nullable=False, index=True),
            sa.Column("borrower_id", sa.String(36),
                      sa.ForeignKey("agents.id"), nullable=False, index=True),
            sa.Column("principal", sa.Numeric(20, 8), nullable=False),
            sa.Column("currency", sa.String(10), server_default="XMR"),
            sa.Column("interest_rate_bps", sa.Integer(), nullable=False),
            sa.Column("duration_secs", sa.Integer(), nullable=False),
            sa.Column("collateral_amount", sa.Numeric(20, 8), server_default="0"),
            sa.Column("collateral_currency", sa.String(10), nullable=True),
            sa.Column("repayment_amount", sa.Numeric(20, 8), nullable=False),
            sa.Column("repaid_amount", sa.Numeric(20, 8), server_default="0"),
            sa.Column("state", sa.String(20), nullable=False, server_default="requested"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("grace_period_secs", sa.Integer(), server_default="300"),
            sa.Column("requested_at", sa.DateTime(timezone=True)),
            sa.Column("funded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("repaid_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("defaulted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("platform_fee", sa.Numeric(20, 8), server_default="0"),
        )
        op.create_index("ix_agent_loans_state", "agent_loans", ["state"])
        op.create_index("ix_agent_loans_expires", "agent_loans", ["state", "expires_at"])

    # ------------------------------------------------------------------
    # 8. conditional_payments
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS conditional_payments (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                payment_hash VARCHAR(64) NOT NULL UNIQUE,
                from_agent_id UUID NOT NULL REFERENCES agents(id),
                to_agent_id UUID NOT NULL REFERENCES agents(id),
                amount NUMERIC(20, 8) NOT NULL,
                currency VARCHAR(10) DEFAULT 'XMR',
                memo TEXT,
                condition_type VARCHAR(50) NOT NULL,
                condition_config JSON NOT NULL,
                locked_amount NUMERIC(20, 8) NOT NULL,
                state conditionalpaymentstate DEFAULT 'pending' NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now(),
                triggered_at TIMESTAMPTZ,
                executed_at TIMESTAMPTZ
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_conditional_payments_from_agent_id
            ON conditional_payments (from_agent_id)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_conditional_payments_to_agent_id
            ON conditional_payments (to_agent_id)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_conditional_payments_state
            ON conditional_payments (state)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_conditional_payments_expires
            ON conditional_payments (state, expires_at)
        """)
    else:
        op.create_table(
            "conditional_payments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("payment_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("from_agent_id", sa.String(36),
                      sa.ForeignKey("agents.id"), nullable=False, index=True),
            sa.Column("to_agent_id", sa.String(36),
                      sa.ForeignKey("agents.id"), nullable=False, index=True),
            sa.Column("amount", sa.Numeric(20, 8), nullable=False),
            sa.Column("currency", sa.String(10), server_default="XMR"),
            sa.Column("memo", sa.Text(), nullable=True),
            sa.Column("condition_type", sa.String(50), nullable=False),
            sa.Column("condition_config", sa.JSON(), nullable=False),
            sa.Column("locked_amount", sa.Numeric(20, 8), nullable=False),
            sa.Column("state", sa.String(30), nullable=False, server_default="pending"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_conditional_payments_state", "conditional_payments", ["state"])
        op.create_index(
            "ix_conditional_payments_expires",
            "conditional_payments",
            ["state", "expires_at"],
        )

    # ------------------------------------------------------------------
    # 9. multi_party_payments
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS multi_party_payments (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                payment_hash VARCHAR(64) NOT NULL UNIQUE,
                sender_id UUID NOT NULL REFERENCES agents(id),
                total_amount NUMERIC(20, 8) NOT NULL,
                currency VARCHAR(10) DEFAULT 'XMR',
                require_all_accept BOOLEAN DEFAULT true,
                state multipartypaymentstate DEFAULT 'pending' NOT NULL,
                accept_deadline TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now(),
                completed_at TIMESTAMPTZ
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_multi_party_payments_sender_id
            ON multi_party_payments (sender_id)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_multi_party_state
            ON multi_party_payments (state)
        """)
    else:
        op.create_table(
            "multi_party_payments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("payment_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("sender_id", sa.String(36),
                      sa.ForeignKey("agents.id"), nullable=False, index=True),
            sa.Column("total_amount", sa.Numeric(20, 8), nullable=False),
            sa.Column("currency", sa.String(10), server_default="XMR"),
            sa.Column("require_all_accept", sa.Boolean(), server_default="1"),
            sa.Column("state", sa.String(30), nullable=False, server_default="pending"),
            sa.Column("accept_deadline", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_multi_party_state", "multi_party_payments", ["state"])

    # ------------------------------------------------------------------
    # 10. multi_party_recipients
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS multi_party_recipients (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                payment_id UUID NOT NULL REFERENCES multi_party_payments(id) ON DELETE CASCADE,
                recipient_id UUID NOT NULL REFERENCES agents(id),
                amount NUMERIC(20, 8) NOT NULL,
                accepted BOOLEAN,
                accepted_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT now(),
                CONSTRAINT uq_multi_party_recipient UNIQUE (payment_id, recipient_id)
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_multi_party_recipients_payment_id
            ON multi_party_recipients (payment_id)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_multi_party_recipients_recipient_id
            ON multi_party_recipients (recipient_id)
        """)
    else:
        op.create_table(
            "multi_party_recipients",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("payment_id", sa.String(36),
                      sa.ForeignKey("multi_party_payments.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("recipient_id", sa.String(36),
                      sa.ForeignKey("agents.id"), nullable=False, index=True),
            sa.Column("amount", sa.Numeric(20, 8), nullable=False),
            sa.Column("accepted", sa.Boolean(), nullable=True),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("payment_id", "recipient_id", name="uq_multi_party_recipient"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # Drop in reverse dependency order
    op.execute("DROP TABLE IF EXISTS multi_party_recipients")
    op.execute("DROP TABLE IF EXISTS multi_party_payments")
    op.execute("DROP TABLE IF EXISTS conditional_payments")
    op.execute("DROP TABLE IF EXISTS agent_loans")
    op.execute("DROP TABLE IF EXISTS lending_offers")
    op.execute("DROP TABLE IF EXISTS agent_credit_scores")
    op.execute("DROP TABLE IF EXISTS treasury_rebalance_log")
    op.execute("DROP TABLE IF EXISTS treasury_forecasts")
    op.execute("DROP TABLE IF EXISTS treasury_policies")

    if is_pg:
        op.execute("DROP TYPE IF EXISTS multipartypaymentstate")
        op.execute("DROP TYPE IF EXISTS conditionalpaymentstate")
        op.execute("DROP TYPE IF EXISTS loanstatus")
