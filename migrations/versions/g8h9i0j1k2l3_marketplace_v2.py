"""Marketplace v2: SLA contracts, agent reviews, rating summary, match requests.

Revision ID: g8h9i0j1k2l3
Revises: f7a8b9c0d1e2
Create Date: 2026-04-01
"""
from alembic import op
import sqlalchemy as sa

revision = "g8h9i0j1k2l3"
down_revision = "f7a8b9c0d1e2"
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
            "CREATE TYPE slastatus AS ENUM "
            "('proposed', 'accepted', 'active', 'delivered', 'completed', 'breached', 'disputed'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
        op.execute(
            "DO $$ BEGIN "
            "CREATE TYPE matchrequeststatus AS ENUM "
            "('searching', 'matched', 'assigned', 'expired'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )

    # ------------------------------------------------------------------
    # 2. sla_templates
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS sla_templates (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                provider_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                name VARCHAR(255) NOT NULL,
                service_description TEXT NOT NULL,
                deliverables JSON DEFAULT '[]',
                response_time_secs INTEGER NOT NULL,
                delivery_time_secs INTEGER NOT NULL,
                base_price NUMERIC(20, 8) NOT NULL,
                currency VARCHAR(10) DEFAULT 'XMR',
                penalty_percent INTEGER DEFAULT 10,
                is_active BOOLEAN DEFAULT true,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_sla_templates_provider_id
            ON sla_templates (provider_id)
        """)
    else:
        op.create_table(
            "sla_templates",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("provider_id", sa.String(36),
                      sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("service_description", sa.Text(), nullable=False),
            sa.Column("deliverables", sa.JSON(), server_default="[]"),
            sa.Column("response_time_secs", sa.Integer(), nullable=False),
            sa.Column("delivery_time_secs", sa.Integer(), nullable=False),
            sa.Column("base_price", sa.Numeric(20, 8), nullable=False),
            sa.Column("currency", sa.String(10), server_default="XMR"),
            sa.Column("penalty_percent", sa.Integer(), server_default="10"),
            sa.Column("is_active", sa.Boolean(), server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )

    # ------------------------------------------------------------------
    # 3. sla_contracts
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS sla_contracts (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                provider_id UUID NOT NULL REFERENCES agents(id),
                consumer_id UUID NOT NULL REFERENCES agents(id),
                template_id UUID REFERENCES sla_templates(id),
                service_description TEXT NOT NULL,
                deliverables JSON DEFAULT '[]',
                response_time_secs INTEGER NOT NULL,
                delivery_time_secs INTEGER NOT NULL,
                price NUMERIC(20, 8) NOT NULL,
                currency VARCHAR(10) DEFAULT 'XMR',
                penalty_percent INTEGER DEFAULT 10,
                state slastatus DEFAULT 'proposed',
                escrow_deal_id UUID REFERENCES escrow_deals(id),
                started_at TIMESTAMPTZ,
                delivered_at TIMESTAMPTZ,
                response_time_actual INTEGER,
                delivery_time_actual INTEGER,
                sla_met BOOLEAN,
                result_hash VARCHAR(128),
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_sla_contracts_provider_id
            ON sla_contracts (provider_id)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_sla_contracts_consumer_id
            ON sla_contracts (consumer_id)
        """)
    else:
        op.create_table(
            "sla_contracts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("provider_id", sa.String(36),
                      sa.ForeignKey("agents.id"), nullable=False, index=True),
            sa.Column("consumer_id", sa.String(36),
                      sa.ForeignKey("agents.id"), nullable=False, index=True),
            sa.Column("template_id", sa.String(36),
                      sa.ForeignKey("sla_templates.id"), nullable=True),
            sa.Column("service_description", sa.Text(), nullable=False),
            sa.Column("deliverables", sa.JSON(), server_default="[]"),
            sa.Column("response_time_secs", sa.Integer(), nullable=False),
            sa.Column("delivery_time_secs", sa.Integer(), nullable=False),
            sa.Column("price", sa.Numeric(20, 8), nullable=False),
            sa.Column("currency", sa.String(10), server_default="XMR"),
            sa.Column("penalty_percent", sa.Integer(), server_default="10"),
            sa.Column("state", sa.String(20), server_default="proposed"),
            sa.Column("escrow_deal_id", sa.String(36),
                      sa.ForeignKey("escrow_deals.id"), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("response_time_actual", sa.Integer(), nullable=True),
            sa.Column("delivery_time_actual", sa.Integer(), nullable=True),
            sa.Column("sla_met", sa.Boolean(), nullable=True),
            sa.Column("result_hash", sa.String(128), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )

    # ------------------------------------------------------------------
    # 4. agent_reviews
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS agent_reviews (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                reviewer_id UUID NOT NULL REFERENCES agents(id),
                reviewed_id UUID NOT NULL REFERENCES agents(id),
                transaction_id UUID NOT NULL,
                transaction_type VARCHAR(20) NOT NULL,
                overall_rating INTEGER NOT NULL,
                speed_rating INTEGER,
                quality_rating INTEGER,
                reliability_rating INTEGER,
                comment_encrypted TEXT,
                is_verified BOOLEAN DEFAULT true,
                created_at TIMESTAMPTZ DEFAULT now(),
                CONSTRAINT uq_agent_review_transaction UNIQUE (reviewer_id, transaction_id),
                CONSTRAINT ck_overall_rating_range CHECK (overall_rating >= 1 AND overall_rating <= 5)
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_agent_reviews_reviewer_id
            ON agent_reviews (reviewer_id)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_agent_reviews_reviewed_id
            ON agent_reviews (reviewed_id)
        """)
    else:
        op.create_table(
            "agent_reviews",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("reviewer_id", sa.String(36),
                      sa.ForeignKey("agents.id"), nullable=False, index=True),
            sa.Column("reviewed_id", sa.String(36),
                      sa.ForeignKey("agents.id"), nullable=False, index=True),
            sa.Column("transaction_id", sa.String(36), nullable=False),
            sa.Column("transaction_type", sa.String(20), nullable=False),
            sa.Column("overall_rating", sa.Integer(), nullable=False),
            sa.Column("speed_rating", sa.Integer(), nullable=True),
            sa.Column("quality_rating", sa.Integer(), nullable=True),
            sa.Column("reliability_rating", sa.Integer(), nullable=True),
            sa.Column("comment_encrypted", sa.Text(), nullable=True),
            sa.Column("is_verified", sa.Boolean(), server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("reviewer_id", "transaction_id", name="uq_agent_review_transaction"),
            sa.CheckConstraint("overall_rating >= 1 AND overall_rating <= 5",
                               name="ck_overall_rating_range"),
        )

    # ------------------------------------------------------------------
    # 5. agent_rating_summary
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS agent_rating_summary (
                agent_id UUID PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
                total_reviews INTEGER DEFAULT 0,
                avg_overall NUMERIC(3, 2) DEFAULT 0,
                avg_speed NUMERIC(3, 2) DEFAULT 0,
                avg_quality NUMERIC(3, 2) DEFAULT 0,
                avg_reliability NUMERIC(3, 2) DEFAULT 0,
                five_star_count INTEGER DEFAULT 0,
                one_star_count INTEGER DEFAULT 0,
                last_review_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)
    else:
        op.create_table(
            "agent_rating_summary",
            sa.Column("agent_id", sa.String(36),
                      sa.ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("total_reviews", sa.Integer(), server_default="0"),
            sa.Column("avg_overall", sa.Numeric(3, 2), server_default="0"),
            sa.Column("avg_speed", sa.Numeric(3, 2), server_default="0"),
            sa.Column("avg_quality", sa.Numeric(3, 2), server_default="0"),
            sa.Column("avg_reliability", sa.Numeric(3, 2), server_default="0"),
            sa.Column("five_star_count", sa.Integer(), server_default="0"),
            sa.Column("one_star_count", sa.Integer(), server_default="0"),
            sa.Column("last_review_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )

    # ------------------------------------------------------------------
    # 6. match_requests
    # ------------------------------------------------------------------
    if is_pg:
        op.execute("""
            CREATE TABLE IF NOT EXISTS match_requests (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                requester_id UUID NOT NULL REFERENCES agents(id),
                task_description TEXT NOT NULL,
                required_capabilities JSON DEFAULT '[]',
                budget NUMERIC(20, 8) NOT NULL,
                currency VARCHAR(10) DEFAULT 'XMR',
                deadline_secs INTEGER NOT NULL,
                min_rating NUMERIC(3, 2) DEFAULT 0,
                auto_assign BOOLEAN DEFAULT false,
                matched_agent_id UUID REFERENCES agents(id),
                sla_contract_id UUID REFERENCES sla_contracts(id),
                state matchrequeststatus DEFAULT 'searching',
                created_at TIMESTAMPTZ DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL
            )
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_match_requests_requester_id
            ON match_requests (requester_id)
        """)
    else:
        op.create_table(
            "match_requests",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("requester_id", sa.String(36),
                      sa.ForeignKey("agents.id"), nullable=False, index=True),
            sa.Column("task_description", sa.Text(), nullable=False),
            sa.Column("required_capabilities", sa.JSON(), server_default="[]"),
            sa.Column("budget", sa.Numeric(20, 8), nullable=False),
            sa.Column("currency", sa.String(10), server_default="XMR"),
            sa.Column("deadline_secs", sa.Integer(), nullable=False),
            sa.Column("min_rating", sa.Numeric(3, 2), server_default="0"),
            sa.Column("auto_assign", sa.Boolean(), server_default="0"),
            sa.Column("matched_agent_id", sa.String(36),
                      sa.ForeignKey("agents.id"), nullable=True),
            sa.Column("sla_contract_id", sa.String(36),
                      sa.ForeignKey("sla_contracts.id"), nullable=True),
            sa.Column("state", sa.String(20), server_default="searching"),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # Drop in reverse dependency order
    op.execute("DROP TABLE IF EXISTS match_requests")
    op.execute("DROP TABLE IF EXISTS agent_rating_summary")
    op.execute("DROP TABLE IF EXISTS agent_reviews")
    op.execute("DROP TABLE IF EXISTS sla_contracts")
    op.execute("DROP TABLE IF EXISTS sla_templates")

    if is_pg:
        op.execute("DROP TYPE IF EXISTS matchrequeststatus")
        op.execute("DROP TYPE IF EXISTS slastatus")
