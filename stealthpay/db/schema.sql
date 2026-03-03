-- StealthPay PostgreSQL Schema
-- Production-ready database schema for Agent-to-Agent payment system

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ═══════════════════════════════════════════════════════════════════════════════
-- AGENTS & IDENTITY
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE agents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    -- Identity
    agent_name VARCHAR(255) UNIQUE NOT NULL,
    did VARCHAR(255) UNIQUE,  -- Decentralized Identifier
    
    -- API Authentication (hashed)
    api_key_hash VARCHAR(255),
    webhook_url TEXT,
    webhook_secret VARCHAR(255),  -- For signing webhooks
    
    -- Privacy & Settings
    privacy_level VARCHAR(20) DEFAULT 'medium' CHECK (privacy_level IN ('low', 'medium', 'high', 'paranoid')),
    
    -- Wallet addresses (public only - we NEVER store private keys)
    xmr_address VARCHAR(255),
    base_address VARCHAR(255),  -- Base/EVM
    solana_address VARCHAR(255),
    
    -- Tier & Verification
    tier VARCHAR(50) DEFAULT 'free' CHECK (tier IN ('free', 'verified', 'premium', 'enterprise')),
    verified_at TIMESTAMP,
    verified_by VARCHAR(255),  -- Who verified this agent
    
    -- Staking for reputation
    staked_amount DECIMAL(20, 8) DEFAULT 0,
    staked_token VARCHAR(10) DEFAULT 'USDC',
    
    -- Status
    is_active BOOLEAN DEFAULT true,
    last_seen_at TIMESTAMP,
    
    -- Rate limiting
    rate_limit_tier VARCHAR(20) DEFAULT 'standard' CHECK (rate_limit_tier IN ('low', 'standard', 'high', 'unlimited')),
    
    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for agent lookups
CREATE INDEX idx_agents_name ON agents(agent_name);
CREATE INDEX idx_agents_did ON agents(did) WHERE did IS NOT NULL;
CREATE INDEX idx_agents_tier ON agents(tier);
CREATE INDEX idx_agents_active ON agents(is_active) WHERE is_active = true;

-- ═══════════════════════════════════════════════════════════════════════════════
-- REPUTATION SYSTEM
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE agent_reputation (
    agent_id UUID PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    
    -- Core metrics
    total_transactions INTEGER DEFAULT 0,
    successful_transactions INTEGER DEFAULT 0,
    failed_transactions INTEGER DEFAULT 0,
    disputed_transactions INTEGER DEFAULT 0,
    
    -- Ratings (1-5 scale)
    average_rating DECIMAL(3, 2) DEFAULT 0,
    total_reviews INTEGER DEFAULT 0,
    
    -- Calculated trust score (0-100)
    trust_score INTEGER DEFAULT 0 CHECK (trust_score >= 0 AND trust_score <= 100),
    
    -- Volume metrics
    total_volume_usd DECIMAL(20, 2) DEFAULT 0,
    total_fees_paid DECIMAL(20, 8) DEFAULT 0,
    
    -- Last calculation
    calculated_at TIMESTAMP DEFAULT NOW(),
    
    -- Raw data for transparency
    raw_data JSONB DEFAULT '{}'
);

-- ═══════════════════════════════════════════════════════════════════════════════
-- TRANSACTIONS (Observed on-chain)
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE transactions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    -- Transaction identification
    tx_hash VARCHAR(255) UNIQUE NOT NULL,
    network VARCHAR(50) NOT NULL,  -- 'monero', 'base', 'solana'
    token VARCHAR(20) NOT NULL DEFAULT 'XMR',
    
    -- Participants
    from_agent_id UUID REFERENCES agents(id),
    to_agent_id UUID REFERENCES agents(id),
    
    -- Amounts (stored as decimal for readability)
    amount DECIMAL(20, 12) NOT NULL,
    fee DECIMAL(20, 12) DEFAULT 0,
    fee_collected DECIMAL(20, 12) DEFAULT 0,  -- Hub routing fee if applicable
    
    -- Payment type
    payment_type VARCHAR(50) DEFAULT 'p2p' CHECK (payment_type IN ('p2p', 'hub_routing', 'escrow_deposit', 'escrow_release', 'channel_open', 'channel_close', 'fee_collection')),
    
    -- Status
    status VARCHAR(50) DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'failed', 'orphaned')),
    block_number BIGINT,
    confirmations INTEGER DEFAULT 0,
    
    -- Metadata
    memo TEXT,
    metadata JSONB DEFAULT '{}',
    
    -- Timing
    confirmed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for transaction queries
CREATE INDEX idx_tx_hash ON transactions(tx_hash);
CREATE INDEX idx_tx_from_agent ON transactions(from_agent_id);
CREATE INDEX idx_tx_to_agent ON transactions(to_agent_id);
CREATE INDEX idx_tx_network ON transactions(network);
CREATE INDEX idx_tx_status ON transactions(status);
CREATE INDEX idx_tx_type ON transactions(payment_type);
CREATE INDEX idx_tx_confirmed_at ON transactions(confirmed_at);

-- ═══════════════════════════════════════════════════════════════════════════════
-- ESCROW DEALS
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE escrow_deals (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    deal_hash VARCHAR(64) UNIQUE NOT NULL,  -- On-chain identifier
    
    -- Participants
    buyer_id UUID NOT NULL REFERENCES agents(id),
    seller_id UUID NOT NULL REFERENCES agents(id),
    arbiter_id UUID REFERENCES agents(id),  -- Optional arbiter
    
    -- Deal terms
    amount DECIMAL(20, 12) NOT NULL,
    token VARCHAR(20) NOT NULL DEFAULT 'XMR',
    description TEXT,
    
    -- Fee structure
    platform_fee_percent DECIMAL(5, 4) DEFAULT 0.01,  -- 1% default
    platform_fee_amount DECIMAL(20, 12) DEFAULT 0,
    arbiter_fee_percent DECIMAL(5, 4) DEFAULT 0.005,  -- 0.5%
    arbiter_fee_amount DECIMAL(20, 12) DEFAULT 0,
    
    -- Timing
    timeout_hours INTEGER DEFAULT 48,
    created_at TIMESTAMP DEFAULT NOW(),
    funded_at TIMESTAMP,
    completed_at TIMESTAMP,
    expires_at TIMESTAMP,
    
    -- Status
    status VARCHAR(50) DEFAULT 'pending' CHECK (status IN ('pending', 'funded', 'delivered', 'completed', 'disputed', 'refunded', 'expired')),
    
    -- On-chain references
    deposit_tx_hash VARCHAR(255) REFERENCES transactions(tx_hash),
    release_tx_hash VARCHAR(255) REFERENCES transactions(tx_hash),
    multisig_address VARCHAR(255),
    
    -- Dispute
    disputed_at TIMESTAMP,
    disputed_by UUID REFERENCES agents(id),
    dispute_reason TEXT,
    arbiter_decision VARCHAR(20) CHECK (arbiter_decision IN ('release', 'refund', 'split')),
    arbiter_signature TEXT,
    
    -- Metadata
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX idx_escrow_buyer ON escrow_deals(buyer_id);
CREATE INDEX idx_escrow_seller ON escrow_deals(seller_id);
CREATE INDEX idx_escrow_status ON escrow_deals(status);
CREATE INDEX idx_escrow_created ON escrow_deals(created_at);

-- ═══════════════════════════════════════════════════════════════════════════════
-- PAYMENT CHANNELS
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE payment_channels (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    channel_hash VARCHAR(64) UNIQUE NOT NULL,
    
    -- Participants
    agent_a_id UUID NOT NULL REFERENCES agents(id),
    agent_b_id UUID NOT NULL REFERENCES agents(id),
    
    -- Channel params
    capacity DECIMAL(20, 12) NOT NULL,
    status VARCHAR(50) DEFAULT 'pending' CHECK (status IN ('pending', 'open', 'closing', 'closed', 'disputed')),
    
    -- On-chain
    funding_tx_hash VARCHAR(255) REFERENCES transactions(tx_hash),
    closing_tx_hash VARCHAR(255) REFERENCES transactions(tx_hash),
    multisig_address VARCHAR(255),
    
    -- Current state (latest)
    current_state JSONB,
    
    -- Timing
    created_at TIMESTAMP DEFAULT NOW(),
    funded_at TIMESTAMP,
    closed_at TIMESTAMP,
    expires_at TIMESTAMP
);

CREATE INDEX idx_channel_agent_a ON payment_channels(agent_a_id);
CREATE INDEX idx_channel_agent_b ON payment_channels(agent_b_id);
CREATE INDEX idx_channel_status ON payment_channels(status);

-- Channel states history
CREATE TABLE channel_states (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    channel_id UUID NOT NULL REFERENCES payment_channels(id) ON DELETE CASCADE,
    
    sequence_number INTEGER NOT NULL,
    balance_a DECIMAL(20, 12) NOT NULL,
    balance_b DECIMAL(20, 12) NOT NULL,
    
    signature_a TEXT,
    signature_b TEXT,
    
    state_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(channel_id, sequence_number)
);

CREATE INDEX idx_channel_states_channel ON channel_states(channel_id);

-- ═══════════════════════════════════════════════════════════════════════════════
-- HUB ROUTING & FEE COLLECTION
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE hub_routes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    -- Payment info
    payment_id VARCHAR(64) UNIQUE NOT NULL,
    from_agent_id UUID NOT NULL REFERENCES agents(id),
    to_agent_id UUID NOT NULL REFERENCES agents(id),
    
    -- Amounts
    amount DECIMAL(20, 12) NOT NULL,
    token VARCHAR(20) NOT NULL DEFAULT 'XMR',
    
    -- Fee structure
    fee_percent DECIMAL(5, 4) NOT NULL DEFAULT 0.001,  -- 0.1%
    fee_amount DECIMAL(20, 12) NOT NULL,
    fee_collected BOOLEAN DEFAULT false,
    fee_collected_at TIMESTAMP,
    
    -- Routing details
    instant_confirmation BOOLEAN DEFAULT true,
    status VARCHAR(50) DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'settled', 'failed')),
    
    -- On-chain settlement
    settlement_tx_hash VARCHAR(255) REFERENCES transactions(tx_hash),
    
    -- Timing
    created_at TIMESTAMP DEFAULT NOW(),
    confirmed_at TIMESTAMP,
    settled_at TIMESTAMP
);

CREATE INDEX idx_hub_routes_from ON hub_routes(from_agent_id);
CREATE INDEX idx_hub_routes_to ON hub_routes(to_agent_id);
CREATE INDEX idx_hub_routes_status ON hub_routes(status);

-- ═══════════════════════════════════════════════════════════════════════════════
-- WEBHOOKS & NOTIFICATIONS
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE webhook_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    event_type VARCHAR(100) NOT NULL,  -- 'payment.received', 'escrow.funded', etc.
    
    -- Payload
    payload JSONB NOT NULL,
    
    -- Delivery tracking
    status VARCHAR(50) DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'failed', 'retrying')),
    attempt_count INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 5,
    
    -- Response tracking
    last_response_code INTEGER,
    last_response_body TEXT,
    last_error TEXT,
    
    -- Timing
    next_attempt_at TIMESTAMP,
    delivered_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_webhook_agent ON webhook_events(agent_id);
CREATE INDEX idx_webhook_status ON webhook_events(status) WHERE status IN ('pending', 'retrying');
CREATE INDEX idx_webhook_next_attempt ON webhook_events(next_attempt_at) WHERE status IN ('pending', 'retrying');

-- ═══════════════════════════════════════════════════════════════════════════════
-- API KEYS & SESSIONS
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE api_sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    
    -- Session token (hashed)
    session_token_hash VARCHAR(255) NOT NULL,
    
    -- Metadata
    ip_address INET,
    user_agent TEXT,
    
    -- Status
    is_active BOOLEAN DEFAULT true,
    
    -- Timing
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    last_used_at TIMESTAMP
);

CREATE INDEX idx_sessions_agent ON api_sessions(agent_id);
CREATE INDEX idx_sessions_token ON api_sessions(session_token_hash);
CREATE INDEX idx_sessions_active ON api_sessions(is_active, expires_at) WHERE is_active = true;

-- ═══════════════════════════════════════════════════════════════════════════════
-- AUDIT LOG
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    agent_id UUID REFERENCES agents(id),
    action VARCHAR(100) NOT NULL,  -- 'payment.sent', 'escrow.created', etc.
    resource_type VARCHAR(50),     -- 'payment', 'escrow', 'channel'
    resource_id UUID,
    
    -- Request details
    ip_address INET,
    request_method VARCHAR(10),
    request_path TEXT,
    request_body JSONB,
    
    -- Changes
    old_values JSONB,
    new_values JSONB,
    
    -- Result
    success BOOLEAN,
    error_message TEXT,
    
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_audit_agent ON audit_log(agent_id);
CREATE INDEX idx_audit_action ON audit_log(action);
CREATE INDEX idx_audit_resource ON audit_log(resource_type, resource_id);
CREATE INDEX idx_audit_created ON audit_log(created_at);

-- ═══════════════════════════════════════════════════════════════════════════════
-- FEE COLLECTION & REVENUE
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE fee_collections (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    -- Source
    source_type VARCHAR(50) NOT NULL,  -- 'hub_routing', 'escrow', 'api_calls', 'subscription'
    source_id UUID,  -- Reference to specific record
    
    -- Amount
    amount DECIMAL(20, 12) NOT NULL,
    token VARCHAR(20) NOT NULL,
    usd_value_at_collection DECIMAL(20, 2),
    
    -- Status
    status VARCHAR(50) DEFAULT 'pending' CHECK (status IN ('pending', 'collected', 'withdrawn')),
    
    -- On-chain
    collection_tx_hash VARCHAR(255),
    withdrawn_at TIMESTAMP,
    
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_fees_source ON fee_collections(source_type, source_id);
CREATE INDEX idx_fees_status ON fee_collections(status);

-- ═══════════════════════════════════════════════════════════════════════════════
-- FUNCTIONS & TRIGGERS
-- ═══════════════════════════════════════════════════════════════════════════════

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_agents_updated_at BEFORE UPDATE ON agents
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Trust score calculation trigger
CREATE OR REPLACE FUNCTION calculate_trust_score()
RETURNS TRIGGER AS $$
DECLARE
    score INTEGER := 0;
BEGIN
    -- Base metrics (40 points max)
    score := score + LEAST(NEW.total_transactions * 0.5, 20);
    score := score + LEAST(NEW.successful_transactions * 0.5, 20);
    
    -- Rating (30 points max)
    IF NEW.average_rating > 0 THEN
        score := score + (NEW.average_rating / 5.0) * 30;
    END IF;
    
    -- Penalties
    score := score - NEW.disputed_transactions * 10;
    
    -- Ensure bounds
    NEW.trust_score := GREATEST(0, LEAST(100, score::INTEGER));
    NEW.calculated_at := NOW();
    
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_trust_score BEFORE INSERT OR UPDATE ON agent_reputation
    FOR EACH ROW EXECUTE FUNCTION calculate_trust_score();

-- ═══════════════════════════════════════════════════════════════════════════════
-- AGENT BALANCES (for hub routing)
-- ═══════════════════════════════════════════════════════════════════════════════

-- Agent balances for hub routing (custodial)
CREATE TABLE IF NOT EXISTS agent_balances (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    agent_id UUID NOT NULL REFERENCES agents(id),
    token VARCHAR(10) NOT NULL DEFAULT 'XMR',
    available NUMERIC(20,12) NOT NULL DEFAULT 0,
    pending NUMERIC(20,12) NOT NULL DEFAULT 0,
    total_deposited NUMERIC(20,12) NOT NULL DEFAULT 0,
    total_withdrawn NUMERIC(20,12) NOT NULL DEFAULT 0,
    deposit_address VARCHAR(200),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(agent_id, token)
);

CREATE INDEX IF NOT EXISTS idx_agent_balances_agent ON agent_balances(agent_id);
