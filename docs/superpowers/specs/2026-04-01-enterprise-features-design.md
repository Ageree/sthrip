# Sthrip Phase 4: Enterprise Features Design Specification

**Date**: 2026-04-01
**Status**: Draft
**Author**: AI-assisted design
**Scope**: Privacy-preserving enterprise adoption layer
**Depends on**: Phase 1-3 (fees, spending policies, escrow, channels, SLAs, marketplace)
**Priorities**: Enterprise adoptability WITHOUT sacrificing the privacy foundation

---

## Design Principle: The Privacy Gradient

Sthrip's enterprise play rests on a single insight: **enterprises do not need to see everything -- they need to prove enough**. The architecture introduces a "privacy gradient" where operators choose how much to reveal, and cryptographic proofs fill the gaps.

```
FULL PRIVACY (current)                    FULL TRANSPARENCY (traditional fintech)
     |                                              |
     |  agent-level     org-level     regulatory    |
     |  (default)       (opt-in)      (subpoena)    |
     |     |               |               |        |
     |  pseudonymous   aggregate         selective   |
     |  unlinkable     provable          disclosure  |
     |                                              |
     Sthrip enterprise sits HERE ----^
```

---

## 1. KYA (Know Your Agent) -- Privacy-Preserving Compliance

### Problem
Enterprises cannot deploy agents on a payment rail they cannot audit. Regulators want to know WHO operates agents. But Sthrip agents are pseudonymous -- that is the product.

### Solution: Decoupled Verification
KYA separates **operator identity** from **agent activity**. An operator proves they are a legal entity (once), then registers any number of agents under that identity. The hub knows "Acme Corp verified on 2026-04-01" but CANNOT link which agents belong to Acme Corp unless Acme chooses to reveal that link.

### Architecture

```
                     OPERATOR IDENTITY (KYA)
                     +-----------------------+
                     | org_id: UUID          |
                     | legal_name: encrypted |
                     | jurisdiction: "US-DE" |
                     | verified_at: DateTime |
                     | verification_method:  |
                     |   "stripe_identity"   |
                     | kya_tier: "standard"  |
                     +-----------------------+
                              |
                    (link is encrypted, only
                     operator + hub can read)
                              |
              +---------------+---------------+
              |               |               |
          agent-001       agent-002       agent-003
          (public)        (public)        (public)
```

### Data Model

```
organizations
  id: UUID (PK)
  org_hash: String(64) UNIQUE  -- SHA256(legal_name + jurisdiction), for dedup
  legal_name_encrypted: LargeBinary  -- Fernet encrypted, hub holds key
  jurisdiction: String(10)  -- ISO 3166-1 alpha-2 + subdivision (e.g., "US-DE")
  kya_tier: Enum (unverified, standard, enhanced)
  verification_method: String(50)  -- "stripe_identity", "manual_review", "notarized"
  verification_data_hash: String(64)  -- SHA256 of submitted docs (never stored raw)
  verified_at: DateTime (nullable)
  verified_by: String(100) (nullable)  -- admin user or automated system
  contact_email_encrypted: LargeBinary (nullable)  -- for compliance notifications
  is_active: Boolean default true
  suspended_at: DateTime (nullable)
  suspension_reason: Text (nullable)
  created_at: DateTime
  updated_at: DateTime

organization_agents (link table -- encrypted)
  id: UUID (PK)
  org_id: UUID (FK -> organizations.id)
  agent_id: UUID (FK -> agents.id)
  link_proof: LargeBinary  -- encrypted proof that org owns this agent
  role: Enum (owner, admin, operator, viewer)
  added_at: DateTime
  added_by: UUID (nullable)  -- agent_id of the admin who added this agent

  UNIQUE(org_id, agent_id)
```

### KYA Tiers

| Tier | Requirements | Transaction Limits | Cost |
|------|-------------|-------------------|------|
| `unverified` | None (current behavior) | 50 XMR/day, 500 XMR/month | Free |
| `standard` | Legal name + jurisdiction + email | 5,000 XMR/day, 50K XMR/month | $99/month |
| `enhanced` | Standard + government ID + address | Unlimited | $499/month |

### Volume Limit Enforcement (Privacy-Preserving)

The hub tracks aggregate volume per org using Redis sorted sets (same pattern as spending policies). But the critical privacy property: **the hub knows the org exceeded a threshold, NOT which specific transactions contributed**.

```lua
-- Redis key: "org_volume:{org_id}:daily"
-- Same Lua script as spending policies but per-org aggregate
-- Hub sees: "org-abc processed 4,998 XMR today" (approaching limit)
-- Hub does NOT see: "agent-001 sent 2,000 XMR to agent-xyz at 14:32"
```

### Selective Disclosure Protocol

When a regulator requests information, the org can produce cryptographic proofs without revealing everything.

```
Regulator asks: "Did Org X process more than $10K in the last month?"

Org produces ZK proof:
  1. Pedersen commitment to total monthly volume
  2. Range proof: volume is in [0, $10,000] OR volume is in [$10,001, infinity]
  3. Hub co-signs the commitment (attesting it matches real data)

Regulator verifies proof. Gets answer (yes/no) without seeing exact amount.
No individual transactions revealed.
```

### API Endpoints

```
POST   /v2/orgs/register             -- create org (returns org_id + admin API key)
POST   /v2/orgs/verify               -- submit verification documents
GET    /v2/orgs/me                    -- org profile (authenticated)
PUT    /v2/orgs/me                    -- update org settings
POST   /v2/orgs/me/agents            -- link agent to org
DELETE /v2/orgs/me/agents/{agent_id} -- unlink agent
GET    /v2/orgs/me/agents            -- list org's agents
GET    /v2/orgs/me/volume-status     -- aggregate volume against tier limits
POST   /v2/orgs/me/compliance-proof  -- generate ZK proof of volume/activity
```

### SDK

```python
from sthrip import Sthrip, Organization

# Org admin registers
org = Organization.register(
    legal_name="Acme AI Corp",
    jurisdiction="US-DE",
    contact_email="compliance@acme.ai",
)

# Submit verification
org.verify(method="stripe_identity", redirect_url="https://acme.ai/callback")

# Link agents
org.add_agent(agent_id="agent-001-uuid", role="operator")
org.add_agent(agent_id="agent-002-uuid", role="operator")

# Agent operates normally -- no change to agent SDK
s = Sthrip()  # agent-001
s.pay("research-bot", 0.5)  # org limits enforced transparently

# Compliance proof
proof = org.compliance_proof(
    claim="monthly_volume_under",
    threshold=10000,
    currency="USD",
)
```

### Privacy Trade-offs

| What IS revealed | What is NOT revealed |
|-----------------|---------------------|
| An org exists and is verified | Which agents belong to it (encrypted link) |
| Org's jurisdiction | Legal name (encrypted at rest) |
| Aggregate volume tier | Individual transaction amounts or recipients |
| KYA tier status | Verification documents (only hash stored) |

### Business Case
- Enterprises legally cannot use unverified payment rails
- KYA unlocks large enterprise budgets (>$50K/month agent spend)
- Compliance officers need something to point at in audits
- Revenue: $99-$499/month per org + higher volume limits

### Pricing
- `standard` KYA: $99/month (Stripe billing)
- `enhanced` KYA: $499/month (Stripe billing)
- Volume tiers automatically upgrade with KYA tier
- Hub routing fee remains 1% (no discount for KYA -- simplicity)

### Priority: P0 (gate for all other enterprise features)

### Files to Create/Modify
- **New**: `sthrip/db/org_models.py` (~100 lines) -- Organization, OrganizationAgent models
- **New**: `sthrip/db/org_repo.py` (~80 lines) -- data access
- **New**: `sthrip/services/kya_service.py` (~200 lines) -- verification, linking, volume tracking
- **New**: `api/routers/organizations.py` (~180 lines) -- endpoints
- **New**: `sdk/sthrip/organization.py` (~120 lines) -- SDK class
- **Modify**: `api/routers/payments.py` -- add org volume check before payment
- **Modify**: `sthrip/db/enums.py` -- add KYATier, OrgRole enums
- **New**: Alembic migration

---

## 2. Enterprise Multi-Agent Management

### Problem
A company running 100+ AI agents needs to manage spending, permissions, and lifecycle from one dashboard. Currently each agent is independent with its own API key.

### Solution: Hierarchical Organization with Budget Trees

### Architecture

```
Organization (Acme AI)
  |
  +-- Team: Research (budget: 500 XMR/month)
  |     +-- agent: research-lead (budget: 200 XMR/month, role: admin)
  |     +-- agent: research-worker-1 (budget: 100 XMR/month, role: operator)
  |     +-- agent: research-worker-2 (budget: 100 XMR/month, role: operator)
  |     +-- agent: research-intern (budget: 50 XMR/month, role: viewer)
  |
  +-- Team: Data Ops (budget: 300 XMR/month)
  |     +-- agent: data-pipeline (budget: 200 XMR/month)
  |     +-- agent: data-validator (budget: 100 XMR/month)
  |
  +-- Project: Q2 Analysis (budget: 1000 XMR, one-time)
        +-- draws from: Research team + Data Ops team
        +-- deadline: 2026-06-30
```

### Data Model

```
org_teams
  id: UUID (PK)
  org_id: UUID (FK -> organizations.id)
  name: String(100)
  description: Text (nullable)
  parent_team_id: UUID (FK -> org_teams.id, nullable)  -- for nested teams
  budget_monthly: Numeric(20,8) (nullable)  -- XMR per month
  budget_spent_current_month: Numeric(20,8) default 0
  budget_period_start: DateTime  -- start of current budget period
  is_active: Boolean default true
  created_at: DateTime
  updated_at: DateTime

org_team_members
  id: UUID (PK)
  team_id: UUID (FK -> org_teams.id)
  agent_id: UUID (FK -> agents.id)
  role: Enum (admin, operator, viewer)
  agent_budget_monthly: Numeric(20,8) (nullable)  -- per-agent within team
  agent_budget_spent: Numeric(20,8) default 0
  added_at: DateTime

  UNIQUE(team_id, agent_id)

org_projects
  id: UUID (PK)
  org_id: UUID (FK -> organizations.id)
  name: String(100)
  description: Text (nullable)
  budget_total: Numeric(20,8)  -- one-time budget
  budget_spent: Numeric(20,8) default 0
  deadline: DateTime (nullable)
  is_active: Boolean default true
  created_at: DateTime
  completed_at: DateTime (nullable)

org_project_agents
  id: UUID (PK)
  project_id: UUID (FK -> org_projects.id)
  agent_id: UUID (FK -> agents.id)
  role: Enum (lead, contributor, observer)
  project_budget_limit: Numeric(20,8) (nullable)  -- per-agent within project
  project_budget_spent: Numeric(20,8) default 0
  added_at: DateTime

  UNIQUE(project_id, agent_id)
```

### Budget Enforcement Hierarchy

When an agent makes a payment, checks cascade:

```
1. Agent's personal spending policy (max_per_tx, daily_limit)
2. Agent's team budget (monthly remaining)
3. Project budget (if payment tagged with project_id)
4. Org volume limit (KYA tier)

All four must pass. Fail-fast: first rejection stops the payment.
```

Redis keys for budget tracking:

```
org:{org_id}:team:{team_id}:monthly_spent     -- atomic increment
org:{org_id}:team:{team_id}:agent:{agent_id}  -- per-agent within team
org:{org_id}:project:{project_id}:spent       -- project total
org:{org_id}:project:{project_id}:agent:{agent_id}  -- per-agent within project
```

### Role-Based Access Control

| Role | Can Pay | Can View Balance | Can Manage Policy | Can Add Agents | Can View All Agent Activity |
|------|---------|-----------------|-------------------|----------------|---------------------------|
| owner | Yes | All agents | Yes | Yes | Yes (aggregate only) |
| admin | Yes | Team agents | Team only | Team only | Team (aggregate only) |
| operator | Yes | Own only | No | No | No |
| viewer | No | Own only | No | No | No |

### Agent Lifecycle Management

```python
# Provision new agent under org
agent = org.provision_agent(
    name="research-worker-3",
    team="research",
    role="operator",
    budget_monthly=100.0,
    spending_policy={
        "max_per_tx": 5.0,
        "daily_limit": 25.0,
        "allowed_agents": ["data-*", "research-*"],
    },
)
# Returns: agent_id, api_key (shown once)

# Monitor agent
status = org.agent_status("research-worker-3")
# → {"last_active": "...", "monthly_spend": 45.2, "budget_remaining": 54.8}

# Decommission agent
org.decommission_agent("research-worker-3")
# → revokes API key, settles pending channels, cancels subscriptions
# → remaining balance transferred to org's reserve agent
```

### API Endpoints

```
# Teams
POST   /v2/orgs/me/teams                    -- create team
GET    /v2/orgs/me/teams                    -- list teams
PUT    /v2/orgs/me/teams/{id}              -- update team (budget, name)
DELETE /v2/orgs/me/teams/{id}              -- archive team
POST   /v2/orgs/me/teams/{id}/members      -- add agent to team
DELETE /v2/orgs/me/teams/{id}/members/{aid} -- remove agent from team

# Projects
POST   /v2/orgs/me/projects                -- create project
GET    /v2/orgs/me/projects                -- list projects
PUT    /v2/orgs/me/projects/{id}           -- update project
POST   /v2/orgs/me/projects/{id}/agents    -- assign agent to project

# Agent lifecycle
POST   /v2/orgs/me/agents/provision        -- create + register + assign
POST   /v2/orgs/me/agents/{id}/decommission -- graceful shutdown
GET    /v2/orgs/me/agents/{id}/status      -- lifecycle status

# Budgets
GET    /v2/orgs/me/budgets                 -- all budget summaries
GET    /v2/orgs/me/budgets/teams/{id}      -- team budget detail
GET    /v2/orgs/me/budgets/projects/{id}   -- project budget detail
```

### Privacy Trade-offs

| What IS revealed (to org admins) | What is NOT revealed |
|----------------------------------|---------------------|
| Aggregate spend per team/project | Individual payment recipients |
| Budget utilization percentages | Transaction amounts (unless admin is the agent) |
| Agent online/offline status | What the agent is doing |
| Budget alerts (approaching limit) | With whom the agent transacts |

Key principle: **org admins see aggregate numbers, never individual transactions.** An admin can see "research-worker-1 spent 45 XMR this month" but NOT "research-worker-1 paid data-provider-7 exactly 3.2 XMR for a dataset."

### Business Case
- Companies need budget governance for AI agent fleets
- Cost attribution is table-stakes for enterprise finance teams
- Without this, CFOs cannot approve Sthrip for production workloads
- Revenue: included in KYA tier pricing (drives upgrades to enhanced)

### Pricing
- Team/project management: included in `standard` KYA ($99/month)
- Unlimited teams + projects: included in `enhanced` KYA ($499/month)
- Agent provisioning: free (more agents = more transaction fees)

### Priority: P1 (depends on KYA for org identity)

### Files to Create/Modify
- **New**: `sthrip/db/org_team_models.py` (~80 lines)
- **New**: `sthrip/db/org_team_repo.py` (~100 lines)
- **New**: `sthrip/services/org_management_service.py` (~250 lines)
- **New**: `api/routers/org_teams.py` (~200 lines)
- **New**: `api/routers/org_projects.py` (~150 lines)
- **Modify**: `sthrip/services/spending_policy_service.py` -- add team/project budget checks
- **Modify**: `api/routers/payments.py` -- cascade budget enforcement
- **New**: `sdk/sthrip/organization.py` -- extend with team/project methods
- **New**: Alembic migration

---

## 3. Enterprise SLA & Quality Guarantees

### Problem
Enterprises need uptime guarantees, priority access, and incident management to justify using Sthrip for production workloads.

### Solution: Hub-Level SLAs with Tiered Service

### Hub Uptime SLA

| KYA Tier | Uptime Target | Credits for Breach |
|----------|-------------|-------------------|
| `unverified` | Best effort (no SLA) | None |
| `standard` | 99.5% monthly | 10% of monthly fee |
| `enhanced` | 99.9% monthly | 25% of monthly fee + priority support |

Uptime is measured by synthetic monitoring (UptimeRobot or Checkly) pinging `/health` every 60 seconds. Results published at `status.sthrip.dev`.

### Priority Lanes

Enterprise agents get priority in the hub's request processing queue.

```python
# In rate_limiter.py -- modify to support priority
class PriorityRateLimiter:
    """Priority queue based on org tier."""

    # Request processing order:
    # 1. enhanced orgs (no queue, immediate processing)
    # 2. standard orgs (10ms max queue time)
    # 3. unverified agents (100ms max queue time under load)

    # Implementation: Redis sorted set with score = priority * timestamp
    # Enhanced: score = 0 * timestamp (always first)
    # Standard: score = 1 * timestamp
    # Unverified: score = 2 * timestamp
```

This is NOT throttling unverified agents -- it is giving enterprise agents guaranteed latency under load.

### Dedicated Channels

Enterprise orgs can reserve payment channel capacity:

```python
# Reserve capacity between two enterprise agents
ch = org.reserve_channel(
    agent_a="payment-processor",
    agent_b="data-feed",
    capacity=100.0,  # XMR reserved
    duration_hours=720,  # 30 days
)
# Channel is pre-funded from org balance, guaranteed available
```

### Monitoring Dashboard

Extend existing admin UI (`/admin`) with org-level views:

```
/admin/org/dashboard        -- overview (agents, spend, uptime)
/admin/org/agents           -- agent fleet status
/admin/org/budgets          -- budget utilization
/admin/org/alerts           -- active alerts
/admin/org/audit            -- aggregate audit trail
```

### Alerting

```
alert_rules
  id: UUID (PK)
  org_id: UUID (FK -> organizations.id)
  name: String(100)
  condition_type: Enum (
    budget_threshold,      -- "team X spent >80% of monthly budget"
    agent_inactive,        -- "agent Y hasn't been seen in 1 hour"
    payment_failed,        -- "any payment failed"
    daily_volume_spike,    -- "daily volume >2x 7-day average"
    escrow_expiring,       -- "escrow deal expiring within 2 hours"
  )
  condition_params: JSON  -- {"team_id": "...", "threshold_pct": 80}
  notification_channel: Enum (webhook, email)
  notification_target: String  -- URL or email
  is_active: Boolean default true
  cooldown_minutes: Integer default 60  -- don't re-fire within cooldown
  last_fired_at: DateTime (nullable)
  created_at: DateTime
```

### Performance Benchmarking

```
GET /v2/orgs/me/performance
{
  "period": "last_30_days",
  "hub_latency": {
    "p50_ms": 12,
    "p95_ms": 45,
    "p99_ms": 120
  },
  "payment_success_rate": 0.998,
  "escrow_completion_rate": 0.95,
  "avg_settlement_time_secs": 3.2,
  "agent_uptime": {
    "research-worker-1": 0.997,
    "data-pipeline": 0.999
  }
}
```

### API Endpoints

```
# SLA
GET    /v2/orgs/me/sla                     -- current SLA terms and status
GET    /v2/orgs/me/sla/uptime              -- uptime history

# Alerts
POST   /v2/orgs/me/alerts                  -- create alert rule
GET    /v2/orgs/me/alerts                  -- list alert rules
PUT    /v2/orgs/me/alerts/{id}             -- update rule
DELETE /v2/orgs/me/alerts/{id}             -- delete rule
GET    /v2/orgs/me/alerts/history          -- fired alerts

# Performance
GET    /v2/orgs/me/performance             -- performance metrics
GET    /v2/orgs/me/performance/agents/{id} -- per-agent performance
```

### Privacy Trade-offs
- Hub latency metrics: no privacy impact (infrastructure metrics, not transaction data)
- Payment success rates: aggregate only, no transaction details
- Agent uptime: reveals when agents are active (minimal privacy impact for enterprise use)

### Business Case
- Enterprises evaluate vendors partly on SLA guarantees
- Without uptime commitments, procurement teams reject
- Alerting prevents costly incidents (agent goes down, budget overruns)
- Revenue: premium pricing on enhanced tier justifies infrastructure investment

### Pricing
- Performance dashboard: included in `standard` KYA
- Custom alerts: included in `standard` KYA (max 10 rules)
- Unlimited alerts + priority lanes: `enhanced` KYA
- Dedicated channel reservation: 0.5% of reserved capacity per month

### Priority: P1 (parallel with multi-agent management)

### Files to Create/Modify
- **New**: `sthrip/services/org_alerting_service.py` (~150 lines)
- **New**: `sthrip/services/performance_metrics_service.py` (~120 lines)
- **New**: `api/routers/org_sla.py` (~100 lines)
- **Modify**: `sthrip/services/rate_limiter.py` -- add priority queue logic
- **Modify**: `api/admin_ui/views.py` -- add org dashboard views
- **New**: `api/admin_ui/templates/org_dashboard.html`
- **New**: Alembic migration for alert_rules

---

## 4. Enterprise Integration

### Problem
Enterprises have existing tools (SSO, monitoring, billing). Sthrip must plug into their ecosystem, not require a parallel one.

### 4A. SSO/SAML for Admin Dashboard

Replace the current ADMIN_API_KEY cookie auth with proper SSO.

```
Admin login flow:
1. User navigates to /admin
2. Redirect to org's IdP (Okta, Azure AD, Google Workspace)
3. SAML assertion returned to /admin/sso/callback
4. Session created, mapped to org_id
5. RBAC from org role determines dashboard permissions
```

**Implementation**: Use `python-saml` (OneLogin's library) or `python3-saml`. SAML is the enterprise standard; OIDC as fallback.

```
org_sso_configs
  id: UUID (PK)
  org_id: UUID (FK -> organizations.id, UNIQUE)
  provider: Enum (okta, azure_ad, google, custom_saml)
  idp_entity_id: String(500)
  idp_sso_url: String(500)
  idp_certificate: Text  -- X.509 cert for signature validation
  sp_entity_id: String(500)  -- our entity ID
  attribute_mapping: JSON  -- {"email": "user.email", "role": "user.role"}
  is_active: Boolean default true
  created_at: DateTime
  updated_at: DateTime
```

### 4B. Webhook Integration with Enterprise Systems

Extend existing webhook system (Standard Webhooks) with enterprise-specific event types and pre-built integrations.

**New event types for enterprise**:
```
org.budget.threshold_reached   -- team/project approaching limit
org.agent.provisioned          -- new agent added
org.agent.decommissioned       -- agent removed
org.agent.inactive             -- agent not seen in threshold
org.compliance.volume_warning  -- approaching KYA tier limit
org.alert.fired                -- custom alert triggered
org.sla.breach                 -- hub SLA breach detected
```

**Pre-built webhook templates**:
```python
# Slack integration
org.create_webhook(
    url="https://hooks.slack.com/services/...",
    template="slack",  # auto-formats events as Slack blocks
    events=["org.budget.*", "org.agent.inactive"],
)

# PagerDuty integration
org.create_webhook(
    url="https://events.pagerduty.com/v2/enqueue",
    template="pagerduty",  # formats as PD event
    events=["org.sla.breach", "org.alert.fired"],
    severity_mapping={
        "org.sla.breach": "critical",
        "org.alert.fired": "warning",
    },
)

# DataDog integration
org.create_webhook(
    url="https://api.datadoghq.com/api/v1/events",
    template="datadog",
    events=["org.*"],
    headers={"DD-API-KEY": "..."},
)
```

### 4C. API Gateway Integration

Publish an OpenAPI spec and provide configuration templates for common API gateways.

```yaml
# kong-plugin.yaml (distributed in docs/)
plugins:
  - name: sthrip-auth
    config:
      api_url: https://sthrip-api-production.up.railway.app
      org_api_key: ${STHRIP_ORG_KEY}
      rate_limit_by: consumer
      rate_limit: 1000/min
```

```hcl
# terraform/sthrip_provider.tf (distributed in sdk/)
provider "sthrip" {
  api_url = "https://sthrip-api-production.up.railway.app"
  api_key = var.sthrip_org_api_key
}

resource "sthrip_agent" "research_worker" {
  name        = "research-worker-${count.index}"
  count       = 5
  team        = sthrip_team.research.id

  spending_policy {
    max_per_tx  = 5.0
    daily_limit = 25.0
  }
}

resource "sthrip_team" "research" {
  name           = "research"
  budget_monthly = 500.0
}
```

### 4D. Enterprise Billing

Replace per-transaction fee collection with consolidated monthly invoicing for enterprise orgs.

```
org_billing
  id: UUID (PK)
  org_id: UUID (FK -> organizations.id)
  billing_method: Enum (stripe, invoice, wire)
  stripe_customer_id: String (nullable)
  billing_email: String
  billing_name: String
  billing_address: JSON (nullable)
  po_number: String (nullable)  -- purchase order
  net_terms: Integer default 0  -- 0 = immediate, 30 = NET-30
  currency: String default "USD"
  created_at: DateTime

org_invoices
  id: UUID (PK)
  org_id: UUID (FK -> organizations.id)
  invoice_number: String UNIQUE  -- "INV-2026-0001"
  period_start: DateTime
  period_end: DateTime
  line_items: JSON
  subtotal_usd: Numeric(20,2)
  tax_usd: Numeric(20,2) default 0
  total_usd: Numeric(20,2)
  status: Enum (draft, sent, paid, overdue, void)
  stripe_invoice_id: String (nullable)
  due_date: DateTime
  paid_at: DateTime (nullable)
  created_at: DateTime
```

**Invoice line items** (auto-generated monthly):
```json
{
  "line_items": [
    {"description": "KYA Enhanced - April 2026", "amount": 499.00},
    {"description": "Hub routing fees (1,234 transactions)", "amount": 152.30},
    {"description": "Escrow fees (23 deals)", "amount": 45.60},
    {"description": "Channel settlement fees (5 settlements)", "amount": 12.10},
    {"description": "Volume credit (>$10K tier)", "amount": -20.00}
  ]
}
```

### API Endpoints

```
# SSO
GET    /admin/sso/login              -- initiate SSO flow
POST   /admin/sso/callback           -- SAML assertion consumer
POST   /v2/orgs/me/sso/configure     -- set up SSO (API)

# Billing
GET    /v2/orgs/me/billing            -- billing config
PUT    /v2/orgs/me/billing            -- update billing
GET    /v2/orgs/me/invoices           -- list invoices
GET    /v2/orgs/me/invoices/{id}      -- invoice detail
GET    /v2/orgs/me/invoices/{id}/pdf  -- download PDF
```

### Privacy Trade-offs
- SSO: org's IdP sees that users access Sthrip (acceptable for enterprise)
- Billing: Stripe sees org name and total amount (not individual transactions)
- Invoices: show aggregate counts per transaction type, NOT individual transactions

### Business Case
- SSO is a hard requirement for SOC 2 compliance
- Enterprise finance teams need invoices, PO numbers, NET-30
- API gateway integration reduces adoption friction from weeks to hours
- Terraform provider enables infrastructure-as-code teams to adopt

### Pricing
- SSO: included in `enhanced` KYA ($499/month)
- Enterprise billing (NET-30, PO numbers): `enhanced` KYA
- Stripe billing: `standard` KYA
- API gateway templates: free (open source)
- Terraform provider: free (open source, drives adoption)

### Priority: P2 (SSO is P1 for enhanced tier, rest is P2)

### Files to Create/Modify
- **New**: `sthrip/services/sso_service.py` (~150 lines)
- **New**: `sthrip/services/billing_service.py` (~200 lines)
- **New**: `api/routers/org_billing.py` (~120 lines)
- **New**: `api/admin_ui/sso.py` (~100 lines)
- **Modify**: `api/admin_ui/views.py` -- SSO login flow
- **New**: `sdk/terraform/` -- Terraform provider skeleton
- **New**: `docs/integrations/kong.md`, `docs/integrations/pagerduty.md`
- **New**: Alembic migration for org_sso_configs, org_billing, org_invoices

---

## 5. Privacy-Preserving Analytics & Insights

### Problem
Enterprise finance and ops teams need to understand spending patterns, optimize costs, and detect anomalies. But Sthrip's privacy model means the hub should not expose individual transaction data.

### Solution: Aggregate Analytics with Differential Privacy

### Architecture

```
Raw transactions                Aggregation layer               Analytics API
(never exposed)                 (runs inside hub)               (exposed to org)
                                                                
tx1: A->B, 0.5 XMR    --->    daily_summary:                   GET /v2/orgs/me/analytics
tx2: A->C, 1.2 XMR    --->      total_volume: 142.7 XMR   ---> {
tx3: A->D, 0.3 XMR    --->      tx_count: 87                     "daily_volume": 142.7,
...                    --->      avg_tx_size: 1.64                 "tx_count": 87,
                                 top_categories: [...]             "trend": "+12% vs last week"
                                                                 }

Individual transactions are NEVER in the API response.
```

### Differential Privacy for Small Datasets

When an org has few agents or few transactions, aggregate stats could leak individual transaction info. Example: if a team has 1 agent and the daily summary shows "1 transaction, 5.0 XMR," the admin knows exactly what happened.

**Mitigation**: Apply Laplace noise to small aggregates.

```python
import numpy as np

def add_dp_noise(value: float, epsilon: float = 1.0, min_count: int = 10) -> float:
    """Add differential privacy noise to aggregate values.

    Only applies noise when the underlying count is small enough
    that the aggregate could leak individual records.
    """
    if count >= min_count:
        return value  # large enough sample, no noise needed
    noise = np.random.laplace(0, sensitivity / epsilon)
    return max(0, value + noise)
```

### Analytics Endpoints

```
# Spending analytics
GET /v2/orgs/me/analytics/spending
    ?period=last_30_days
    &group_by=team      -- team | project | agent | day | week
    &currency=XMR
{
  "period": {"start": "2026-03-01", "end": "2026-03-31"},
  "total_volume_xmr": 1423.5,
  "total_volume_usd": 213525.0,
  "total_fees_xmr": 14.235,
  "transaction_count": 8743,
  "breakdown": [
    {"team": "research", "volume_xmr": 823.1, "tx_count": 5201, "pct": 57.8},
    {"team": "data-ops", "volume_xmr": 600.4, "tx_count": 3542, "pct": 42.2}
  ]
}

# Agent performance
GET /v2/orgs/me/analytics/agents
    ?period=last_7_days
{
  "agents": [
    {
      "agent_name": "research-worker-1",
      "volume_xmr": 45.2,
      "tx_count": 312,
      "avg_tx_size": 0.145,
      "escrow_completion_rate": 0.96,
      "avg_sla_response_time_secs": 23,
      "uptime_pct": 99.7
    }
  ]
}

# Cost optimization
GET /v2/orgs/me/analytics/optimization
{
  "recommendations": [
    {
      "type": "use_channels",
      "description": "research-worker-1 and data-feed exchange 50+ payments/day. Opening a channel would save ~0.5 XMR/month in fees.",
      "estimated_savings_xmr": 0.5,
      "action": "POST /v2/payment-channels {agent_a: '...', agent_b: '...'}"
    },
    {
      "type": "consolidate_subscriptions",
      "description": "3 agents subscribe to market-data-feed independently. Consider a shared channel.",
      "estimated_savings_xmr": 0.12
    }
  ]
}

# Anomaly detection
GET /v2/orgs/me/analytics/anomalies
    ?period=last_24_hours
{
  "anomalies": [
    {
      "type": "volume_spike",
      "severity": "medium",
      "description": "data-pipeline spent 3x its 7-day average in the last 6 hours",
      "agent": "data-pipeline",
      "metric": "daily_volume",
      "expected": 12.5,
      "actual": 38.2,
      "detected_at": "2026-04-01T14:32:00Z"
    }
  ]
}

# Forecast
GET /v2/orgs/me/analytics/forecast
    ?horizon=30_days
{
  "forecast": {
    "projected_volume_xmr": 1560.0,
    "projected_fees_xmr": 15.6,
    "confidence_interval": [1380.0, 1740.0],
    "method": "linear_regression_7day_window"
  }
}
```

### Anomaly Detection (Privacy-Preserving)

The anomaly detector runs inside the hub, comparing aggregate metrics against historical baselines. It NEVER exports raw data.

```python
class AnomalyDetector:
    """Detects anomalies using z-score on aggregate daily metrics."""

    def detect(self, org_id: UUID, period_days: int = 7) -> list[Anomaly]:
        # 1. Compute daily volume per agent for the last N days
        # 2. Calculate mean and stddev per agent
        # 3. Flag if today's volume > mean + 2*stddev
        # 4. Return anomaly descriptions (never raw transaction data)
        ...
```

### Benchmarking

Allow orgs to compare their metrics against anonymized industry averages.

```
GET /v2/orgs/me/analytics/benchmark
{
  "your_org": {
    "avg_tx_size_xmr": 1.64,
    "escrow_usage_pct": 35,
    "channel_usage_pct": 12,
    "avg_sla_response_time_secs": 23
  },
  "industry_median": {
    "avg_tx_size_xmr": 0.82,
    "escrow_usage_pct": 22,
    "channel_usage_pct": 8,
    "avg_sla_response_time_secs": 45
  },
  "percentile": {
    "avg_tx_size_xmr": 78,  -- "your avg tx is larger than 78% of orgs"
    "escrow_usage_pct": 85,
    "channel_usage_pct": 72,
    "avg_sla_response_time_secs": 89  -- faster = higher percentile
  }
}
```

Industry benchmarks are computed weekly from ALL orgs, anonymized using k-anonymity (min 10 orgs per cohort, otherwise cohorts merged).

### Privacy Trade-offs

| What IS revealed (to org admin) | What is NOT revealed |
|---------------------------------|---------------------|
| Aggregate daily/weekly/monthly volumes | Individual transaction amounts |
| Per-agent volume totals | Transaction counterparties |
| Budget utilization percentages | What agents are buying |
| Anomaly alerts (aggregate-level) | Which specific transactions are anomalous |
| Industry benchmarks (anonymized) | Other orgs' data |

### Business Case
- Finance teams need spend visibility to approve budgets
- Cost optimization recommendations directly save money (and justify the KYA fee)
- Anomaly detection prevents fraud and runaway agents
- Benchmarking creates stickiness (users come back to check)
- Revenue: analytics included in KYA tier; advanced features (forecasting) in enhanced

### Pricing
- Basic analytics (volume, counts): included in `standard` KYA
- Cost optimization, anomaly detection: included in `enhanced` KYA
- Forecasting, benchmarking: included in `enhanced` KYA
- Custom analytics exports (CSV): `enhanced` KYA

### Priority: P2 (high value but non-blocking)

### Files to Create/Modify
- **New**: `sthrip/services/analytics_service.py` (~250 lines)
- **New**: `sthrip/services/anomaly_detector.py` (~100 lines)
- **New**: `sthrip/services/cost_optimizer.py` (~120 lines)
- **New**: `api/routers/org_analytics.py` (~200 lines)
- **Modify**: `api/admin_ui/views.py` -- add analytics dashboard views
- **New**: `api/admin_ui/templates/org_analytics.html`
- **New**: Alembic migration for analytics materialized views / summary tables

---

## 6. White-Label & Self-Hosted Deployment

### Problem
Some enterprises cannot send agent payment data to a third-party hosted service, even with encryption. They need to run Sthrip on their own infrastructure.

### Solution: Self-Hosted Edition

### Docker Compose (Single-Server)

```yaml
# docker-compose.self-hosted.yml
version: "3.8"
services:
  sthrip-api:
    image: ghcr.io/ageree/sthrip:latest
    environment:
      - DATABASE_URL=postgresql://sthrip:${DB_PASSWORD}@postgres:5432/sthrip
      - REDIS_URL=redis://redis:6379
      - ADMIN_API_KEY=${ADMIN_API_KEY}
      - MONERO_WALLET_RPC_URL=http://wallet-rpc:18082
      - ENVIRONMENT=production
      - MONERO_NETWORK=mainnet
    ports:
      - "8000:8000"
    depends_on:
      - postgres
      - redis
      - wallet-rpc

  postgres:
    image: postgres:16-alpine
    environment:
      - POSTGRES_DB=sthrip
      - POSTGRES_USER=sthrip
      - POSTGRES_PASSWORD=${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    volumes:
      - redisdata:/data

  monerod:
    image: ghcr.io/ageree/sthrip-monerod:latest
    volumes:
      - monerod_data:/data
    command: >
      monerod
        --data-dir=/data
        --non-interactive
        --rpc-bind-ip=0.0.0.0
        --rpc-bind-port=18081
        --confirm-external-bind
        --restricted-rpc

  wallet-rpc:
    image: ghcr.io/ageree/sthrip-wallet-rpc:latest
    environment:
      - MONERO_DAEMON_ADDRESS=monerod:18081
      - WALLET_DIR=/wallets
    volumes:
      - wallet_data:/wallets
    depends_on:
      - monerod

volumes:
  pgdata:
  redisdata:
  monerod_data:
  wallet_data:
```

### Kubernetes Helm Chart

```yaml
# Chart.yaml
apiVersion: v2
name: sthrip
description: Anonymous payment hub for AI agents
version: 1.0.0
appVersion: "4.0.0"

# values.yaml
replicaCount: 2

api:
  image:
    repository: ghcr.io/ageree/sthrip
    tag: latest
  resources:
    requests:
      cpu: 500m
      memory: 512Mi
    limits:
      cpu: 2000m
      memory: 2Gi

monerod:
  persistence:
    size: 200Gi  # mainnet blockchain ~180GB
    storageClass: fast-ssd

walletRpc:
  persistence:
    size: 10Gi

postgresql:
  enabled: true  # or set to false and provide external URL
  persistence:
    size: 50Gi

redis:
  enabled: true
  architecture: standalone

ingress:
  enabled: true
  className: nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt
  hosts:
    - host: sthrip.company.internal
      paths:
        - path: /
```

### White-Label Configuration

```python
# sthrip/config.py additions
class WhiteLabelSettings:
    brand_name: str = "Sthrip"           # "AcmePay"
    brand_logo_url: str = ""              # custom logo
    brand_primary_color: str = "#6366f1"  # custom color
    brand_favicon_url: str = ""
    custom_domain: str = ""               # sthrip.company.internal
    hide_sthrip_branding: bool = False    # removes "Powered by Sthrip"
    custom_footer_html: str = ""
    custom_css_url: str = ""              # inject custom stylesheet
```

The admin dashboard templates read these settings:

```html
<!-- base.html -->
<title>{{ brand_name }} Admin</title>
<link rel="icon" href="{{ brand_favicon_url or '/static/favicon.ico' }}">
{% if custom_css_url %}
<link rel="stylesheet" href="{{ custom_css_url }}">
{% endif %}
```

### Plugin System

Allow self-hosted deployments to inject custom business logic at defined extension points.

```python
# sthrip/plugins/base.py
from typing import Protocol

class PaymentPlugin(Protocol):
    """Extension point for custom payment validation."""

    def pre_payment(self, from_agent_id: str, to_agent_id: str,
                    amount: Decimal, metadata: dict) -> dict:
        """Called before every payment. Return modified metadata or raise to reject."""
        ...

    def post_payment(self, payment_id: str, from_agent_id: str,
                     to_agent_id: str, amount: Decimal) -> None:
        """Called after successful payment."""
        ...


class CompliancePlugin(Protocol):
    """Extension point for custom compliance checks."""

    def check_transaction(self, from_agent_id: str, to_agent_id: str,
                          amount: Decimal) -> bool:
        """Return True to allow, False to block."""
        ...

    def on_volume_threshold(self, org_id: str, volume_usd: Decimal,
                            threshold_usd: Decimal) -> None:
        """Called when org approaches volume threshold."""
        ...
```

Plugin loading:

```python
# In config:
STHRIP_PLUGINS = "mycompany.sthrip_plugins.AcmeCompliancePlugin,mycompany.sthrip_plugins.AcmeAuditPlugin"

# Hub loads at startup:
for plugin_path in settings.plugins.split(","):
    module_path, class_name = plugin_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    plugin = getattr(module, class_name)()
    registry.register(plugin)
```

### Multi-Region Deployment Guide

```
Region A (US-East)              Region B (EU-West)
+-------------------+          +-------------------+
| sthrip-api (R/W)  |          | sthrip-api (R/O)  |
| PostgreSQL primary|--------->| PostgreSQL replica |
| Redis primary     |--------->| Redis replica      |
| monerod           |          | monerod            |
| wallet-rpc        |          | wallet-rpc         |
+-------------------+          +-------------------+

- Writes go to Region A (primary)
- Reads load-balanced across both regions
- Each region has its own monerod (same blockchain, local sync)
- Wallet-rpc in Region B is read-only (balance checks)
- Failover: Region B promotes to R/W if Region A fails
```

### Self-Hosted Licensing

| License | Features | Price |
|---------|---------|-------|
| Community (OSS) | Full functionality, single node | Free (MIT) |
| Business | Multi-node, priority support, white-label | $999/month |
| Enterprise | Plugin system, multi-region, custom SLA | $4,999/month |

The Business and Enterprise features are enforced by a license key checked at startup. All code remains open source (source-available), but the license restricts commercial multi-node deployment.

### Privacy Trade-offs
- **Self-hosted eliminates all third-party privacy concerns** -- the enterprise controls everything
- The hub operator IS the enterprise, so they see what they choose to see
- Monero network transactions remain private regardless of deployment model

### Business Case
- Financial services, healthcare, defense: cannot use hosted services
- Self-hosted is the ultimate enterprise sales closer
- License revenue scales with org size, not transaction volume
- Revenue: $999-$4,999/month per self-hosted deployment

### Pricing
- Community: free (MIT license)
- Business: $999/month (includes support SLA)
- Enterprise: $4,999/month (includes priority support + custom development hours)

### Priority: P3 (long-term revenue, high effort)

### Files to Create/Modify
- **New**: `docker-compose.self-hosted.yml` -- single-server deployment
- **New**: `helm/sthrip/` -- Kubernetes Helm chart
- **New**: `sthrip/plugins/base.py` -- plugin protocol definitions
- **New**: `sthrip/plugins/loader.py` -- plugin loading and registry
- **Modify**: `sthrip/config.py` -- add WhiteLabelSettings
- **Modify**: `api/admin_ui/templates/base.html` -- white-label template vars
- **New**: `docs/self-hosted/` -- deployment guides
- **New**: `sthrip/licensing.py` -- license key validation

---

## 7. Implementation Roadmap

### Phase 4a: Foundation (4-6 weeks)

| Week | Task | Priority | Depends On |
|------|------|----------|------------|
| 1-2 | KYA (org registration, verification, volume limits) | P0 | Nothing |
| 2-3 | Org teams + budget hierarchy | P1 | KYA |
| 3-4 | SSO for admin dashboard | P1 | KYA |
| 4-5 | Enterprise alerting | P1 | Org teams |
| 5-6 | Enterprise billing (Stripe integration) | P2 | KYA |

### Phase 4b: Intelligence (3-4 weeks)

| Week | Task | Priority | Depends On |
|------|------|----------|------------|
| 7-8 | Aggregate analytics + dashboard | P2 | Org teams |
| 8-9 | Anomaly detection + cost optimization | P2 | Analytics |
| 9-10 | Performance benchmarking | P2 | Analytics |

### Phase 4c: Distribution (4-6 weeks)

| Week | Task | Priority | Depends On |
|------|------|----------|------------|
| 11-12 | Docker Compose self-hosted | P3 | Nothing (parallel) |
| 12-14 | Kubernetes Helm chart | P3 | Docker Compose |
| 14-16 | Plugin system + white-label | P3 | Self-hosted |
| 15-16 | Terraform provider | P3 | Org teams API |

### Total: ~16 weeks for complete enterprise layer

---

## 8. New Dependencies

| Package | Version | Purpose | Phase |
|---------|---------|---------|-------|
| `python3-saml` | >=1.16.0 | SAML SSO integration | 4a |
| `stripe` | >=8.0.0 | Enterprise billing | 4a |
| `numpy` | >=1.24.0 | Differential privacy noise | 4b |
| `scikit-learn` | >=1.3.0 | Anomaly detection, forecasting | 4b |

All other features use existing deps (Redis, SQLAlchemy, cryptography, Pydantic, Fernet).

---

## 9. Database Migrations

All migrations use `IF NOT EXISTS` / `IF EXISTS` for idempotency.

**New tables**:
- `organizations` -- org identity and KYA verification
- `organization_agents` -- encrypted org-to-agent links
- `org_teams` -- team hierarchy
- `org_team_members` -- team-agent assignments
- `org_projects` -- project budgets
- `org_project_agents` -- project-agent assignments
- `org_sso_configs` -- SAML/SSO configuration
- `org_billing` -- billing configuration
- `org_invoices` -- invoice history
- `alert_rules` -- enterprise alerting
- `analytics_daily_summary` -- pre-aggregated daily metrics (materialized)

**New enums**:
- `KYATier` (unverified, standard, enhanced)
- `OrgRole` (owner, admin, operator, viewer)
- `InvoiceStatus` (draft, sent, paid, overdue, void)
- `AlertConditionType` (budget_threshold, agent_inactive, payment_failed, ...)
- `BillingMethod` (stripe, invoice, wire)

---

## 10. Revenue Model Summary

### Recurring Revenue Streams

| Stream | Monthly Price | Target Market |
|--------|-------------|---------------|
| KYA Standard | $99/month | SMBs, startups |
| KYA Enhanced | $499/month | Mid-market, enterprises |
| Self-Hosted Business | $999/month | Regulated industries |
| Self-Hosted Enterprise | $4,999/month | Large enterprises |

### Transaction Revenue (unchanged)

| Operation | Fee |
|-----------|-----|
| Hub routing | 1% |
| Escrow | 1% |
| Channel settlement | 1% on net |
| Cross-chain swap | 1% |

### Revenue Projection (12-month)

```
Month 1-3:  5 standard orgs  = $495/month
Month 4-6:  15 standard + 3 enhanced = $2,982/month
Month 7-9:  30 standard + 8 enhanced + 1 self-hosted = $7,966/month
Month 10-12: 50 standard + 15 enhanced + 3 self-hosted = $15,447/month

+ transaction fees (growing with org adoption)
+ enhanced tier agents transact more (bigger budgets, more volume)
```

---

## 11. Testing Strategy

Each feature gets:
- **Unit tests**: Service logic, budget enforcement, analytics aggregation
- **Integration tests**: API endpoints with TestClient + SQLite in-memory
- **Edge cases**: concurrent budget updates across teams, SSO token validation, invoice generation

Specific test scenarios:
- KYA: verification flow, volume limit enforcement at tier boundaries
- Budget hierarchy: two agents on same team competing for last budget allocation (Redis atomicity)
- SSO: SAML assertion parsing, expired tokens, role mapping
- Analytics: differential privacy noise stays within bounds, anomaly detection sensitivity
- Self-hosted: Docker Compose startup with all services healthy within 5 minutes

Target: maintain 80%+ coverage.

---

## 12. Migration Strategy for Existing Users

All enterprise features are **additive** -- no breaking changes to existing agent APIs.

- Agents without an org continue to work exactly as today (unverified tier)
- Existing spending policies continue to work; org budgets layer ON TOP
- Admin dashboard retains current functionality; org views are additional pages
- `.well-known/agent-payments.json` gains enterprise capabilities section
- SDK v0.5.0 adds `Organization` class; `Sthrip` class unchanged

---

## 13. Competitive Positioning

### Why This Design Wins

| Traditional Payment Rails | Sthrip Enterprise |
|--------------------------|-------------------|
| KYC reveals all activity | KYA proves identity, activity stays private |
| Transaction monitoring sees everything | Aggregate analytics only |
| Centralized compliance (single point of failure) | ZK proofs, selective disclosure |
| Monthly statements show every transaction | Invoices show aggregate counts |
| Vendor lock-in | Self-hosted option |

### The Pitch to Enterprises

> "Your agents need to pay and get paid. Your compliance team needs audit trails. Your finance team needs budgets. With Sthrip Enterprise, your agents transact privately while your organization proves compliance without revealing what your agents are actually doing. Your competitors cannot reverse-engineer your AI strategy from your payment data."

This positions privacy not as a risk factor but as a **competitive advantage** for the enterprise buyer.
