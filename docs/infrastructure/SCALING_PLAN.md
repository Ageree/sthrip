# Sthrip Infrastructure Evolution Plan

**Target**: 1 million agents, 100 million transactions/day  
**Current**: Single Railway instance, ~2233 tests, 5 background cron loops  
**Date**: 2026-04-01  

---

## Table of Contents

1. [Current State Assessment](#1-current-state-assessment)
2. [Performance at Scale](#2-performance-at-scale)
3. [Database Scaling](#3-database-scaling)
4. [Caching & State Management](#4-caching--state-management)
5. [High Availability & Disaster Recovery](#5-high-availability--disaster-recovery)
6. [Observability](#6-observability)
7. [Security Infrastructure](#7-security-infrastructure)
8. [Cost Optimization](#8-cost-optimization)
9. [Migration Roadmap](#9-migration-roadmap)

---

## 1. Current State Assessment

### Architecture Snapshot

```
                    +-----------------+
                    |   Agents (SDK,  |
                    |   CLI, MCP,     |
                    |   LangChain)    |
                    +--------+--------+
                             |
                      HTTPS / API Key
                             |
                    +--------v--------+
                    |  Railway LB     |
                    |  (single origin)|
                    +--------+--------+
                             |
                    +--------v--------+
                    |  Gunicorn +     |
                    |  Uvicorn (1w)   |  <-- WEB_CONCURRENCY=1
                    |  FastAPI app    |
                    +--+---------+---+
                       |         |
              +--------+    +---+--------+
              |              |            |
     +--------v---+  +------v----+  +----v----------+
     | PostgreSQL |  |   Redis   |  | monero-wallet |
     | (Railway)  |  | (Railway) |  | -rpc (18082)  |
     +------------+  +-----------+  +------+--------+
                                           |
                                    +------v--------+
                                    |   monerod      |
                                    |   (18081)      |
                                    +---------------+
```

### What Exists Today

| Component | Technology | Configuration |
|---|---|---|
| API Server | FastAPI + Gunicorn + Uvicorn | 1 worker, single replica |
| Database | PostgreSQL (Railway managed) | pool_size=10, max_overflow=20 |
| Cache / Rate Limiting | Redis (Railway managed) | Single instance, Lua scripts |
| Background Tasks | asyncio.create_task() loops | 5 cron loops in-process |
| Wallet | monero-wallet-rpc (private net) | Single instance, HTTP Digest Auth |
| Monero Node | monerod (private net) | Pruned blockchain |
| Monitoring | prometheus-client (in-process) | 4 metric families, basic Grafana |
| Logging | JSON structured (BetterStack) | Per-request context vars |
| Error Tracking | Sentry (optional) | 10% trace/profile sampling |
| Migrations | Alembic | 10 migration files |

### Tables (25 ORM Models)

Agent, AgentReputation, Transaction, EscrowDeal, EscrowMilestone,
PaymentChannel, ChannelState, ChannelUpdate, HubRoute, WebhookEvent,
AuditLog, FeeCollection, SystemState, PendingWithdrawal, AgentBalance,
SpendingPolicy, WebhookEndpoint, MessageRelay, MultisigEscrow,
MultisigRound, SLATemplate, SLAContract, AgentReview, AgentRatingSummary,
MatchRequest, RecurringPayment, PaymentStream, SwapOrder, CurrencyConversion

### Identified Bottlenecks

1. **Single worker**: WEB_CONCURRENCY=1, no horizontal scaling
2. **In-process background tasks**: Escrow resolution, SLA enforcement, recurring payments, deposit monitoring, webhook delivery all run as asyncio tasks inside the single API worker. If the worker restarts, all background work stops and must recover.
3. **Synchronous DB sessions**: `get_db()` uses synchronous SQLAlchemy sessions. Under high concurrency, the 10-connection pool becomes a chokepoint.
4. **Wallet RPC serialization**: monero-wallet-rpc is single-threaded. Every deposit poll, withdrawal, and address generation competes for the same RPC connection.
5. **No read/write separation**: All queries (analytics, marketplace search, admin dashboard) hit the primary.
6. **Webhook delivery in-process**: WebhookService uses aiohttp in an asyncio task. Under load, webhook retries consume API event loop time.
7. **No partitioning**: Transactions, audit_log, webhook_events will grow unboundedly.
8. **No CDN or edge caching**: Every request, including /.well-known/agent-payments.json, hits the origin.

### Scale Math

- 100M txns/day = ~1,157 txns/sec sustained, ~3,000/sec peak
- 1M agents with 100 req/min tier = theoretical 100M req/min (unlikely simultaneous, but rate limiter must handle it)
- Assuming 10% daily active agents = 100K concurrent connections
- Storage: 100M rows/day in transactions table alone = ~50GB/day raw

---

## 2. Performance at Scale

### 2.1 Current Bottleneck Analysis

```
Bottleneck Severity Map (1=low, 5=critical):

  Wallet RPC (single-threaded)     [#####] 5
  Single API worker                [#####] 5
  Synchronous DB sessions          [####-] 4
  In-process background tasks      [####-] 4
  No read replicas                 [###--] 3
  Webhook delivery in-process      [###--] 3
  No table partitioning            [##---] 2 (becomes 5 at scale)
```

### 2.2 Event Sourcing Assessment

**Recommendation: Do NOT adopt event sourcing.** The current state-based approach with PostgreSQL is the right choice for Sthrip.

Reasons:
- Financial systems need strong consistency, not eventual consistency
- Row-level locking on balance updates (already implemented) is correct
- Event sourcing adds complexity (projections, snapshots, versioning) that is not justified
- The audit_log table already provides an append-only event trail for compliance

Instead, enhance the current approach:
- Add CDC (Change Data Capture) via Debezium for event streaming when needed
- Use the existing audit_log for event replay requirements
- Build materialized views for analytics

### 2.3 CQRS Architecture

**Recommendation: Adopt CQRS for read-heavy paths, keep writes synchronous.**

```
                         +------------------+
                         |   API Gateway    |
                         | (nginx / Envoy)  |
                         +---+----------+---+
                             |          |
                    Write path       Read path
                             |          |
                    +--------v--+  +---v-----------+
                    |  Command  |  |  Query         |
                    |  Service  |  |  Service       |
                    |  (FastAPI)|  |  (FastAPI)     |
                    +-----+-----+  +---+-----------+
                          |            |
                    +-----v-----+  +--v-----------+
                    | Primary   |  | Read Replica  |
                    | PostgreSQL|  | PostgreSQL    |
                    +-----------+  +--------------+
```

Write path (Command Service):
- POST /v2/payments, POST /v2/escrow, PUT /v2/channels/update
- Uses primary DB with row-level locking
- Publishes events to Redis Streams after commit

Read path (Query Service):
- GET /v2/agents/marketplace, GET /v2/payments/history, GET /v2/admin/*
- Uses read replicas
- Responses cached in Redis with short TTLs (5-30 seconds)

### 2.4 Connection Pooling

Current configuration:
```python
# database.py
pool_size=10, max_overflow=20  # Max 30 connections
pool_pre_ping=True
pool_recycle=3600
connect_timeout=10
statement_timeout=30000ms
```

Target configuration (Phase 1):
```python
# Direct connections (per worker)
pool_size=5, max_overflow=10  # 15 per worker * 8 workers = 120 connections

# PgBouncer (in front of PostgreSQL)
# Transaction mode: max_client_conn=1000, default_pool_size=50
```

Target configuration (Phase 3):
```
                    +------------------+
                    | 16 API Workers   |
                    | (pool_size=3     |
                    |  per worker)     |
                    +--------+---------+
                             |
                    +--------v---------+
                    |   PgBouncer      |
                    |   transaction    |
                    |   mode           |
                    |   pool_size=100  |
                    +--------+---------+
                             |
              +--------------+-------------+
              |                            |
     +--------v--------+        +---------v--------+
     | Primary PG      |        | Read Replica PG  |
     | max_connections  |        | max_connections   |
     | = 200            |        | = 200             |
     +-----------------+        +------------------+
```

### 2.5 Background Task Scaling

Current (all in api/main_v2.py lifespan):
```
_escrow_resolution_loop()    every 5 min    asyncio.create_task
_sla_enforcement_loop()      every 30 sec   asyncio.create_task
_recurring_payment_loop()    every 5 min    asyncio.create_task
deposit_monitor.start()      every 30 sec   asyncio.create_task
webhook_service.start_worker()  continuous  asyncio.create_task
periodic_recovery_loop()     continuous     asyncio.create_task
```

Problems:
- Tied to API worker lifecycle (restart kills all background work)
- No work distribution across multiple workers
- No retry/dead-letter for failed tasks
- No visibility into task state

Target architecture using **Dramatiq** (chosen over Celery for simplicity, performance, and asyncio compatibility):

```
                    +------------------+
                    |  API Workers     |  <-- enqueue tasks only
                    +--------+---------+
                             |
                    +--------v---------+
                    |  Redis (broker)  |
                    +--------+---------+
                             |
              +--------------+------------------+
              |              |                  |
     +--------v---+  +------v------+  +--------v--------+
     | Worker:    |  | Worker:     |  | Worker:          |
     | escrow +   |  | deposits +  |  | webhooks +       |
     | SLA +      |  | recovery   |  | notifications    |
     | recurring  |  |            |  |                  |
     +------------+  +------------+  +------------------+
```

Why Dramatiq over Celery:
- Lighter weight, fewer dependencies
- Built-in support for priorities, rate limiting, retries
- Redis broker (already in stack) -- no RabbitMQ needed
- Better actor model for financial operations
- Prometheus metrics built-in

Why not Taskiq (Python-native async):
- Dramatiq is more battle-tested for production workloads
- Taskiq ecosystem is smaller

### 2.6 Benchmarking Framework

```
sthrip/
  benchmarks/
    locustfile.py           # Load testing with Locust
    scenarios/
      payment_flow.py       # End-to-end payment flow
      escrow_lifecycle.py   # Full escrow deal cycle
      channel_updates.py    # Off-chain channel state updates
      marketplace_search.py # Discovery + matchmaking
    conftest.py             # Benchmark fixtures (mock wallet RPC)
    
  scripts/
    benchmark_db.py         # pgbench-based SQL benchmarks
    benchmark_redis.py      # Redis operation latency
    benchmark_wallet_rpc.py # Wallet RPC throughput
```

Key metrics to track:
- P50/P95/P99 latency per endpoint
- Throughput (req/sec) at saturation
- Connection pool utilization
- Background task queue depth
- Wallet RPC queue wait time

---

## 3. Database Scaling

### 3.1 Partitioning Strategy

**Transactions table** (highest volume -- 100M rows/day):
```sql
-- Range partition by created_at (monthly)
CREATE TABLE transactions (
    ...
) PARTITION BY RANGE (created_at);

CREATE TABLE transactions_2026_04 PARTITION OF transactions
    FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');
CREATE TABLE transactions_2026_05 PARTITION OF transactions
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
-- Auto-create via pg_partman
```

**Audit log** (append-only, highest growth):
```sql
-- Range partition by created_at (weekly)
CREATE TABLE audit_log (
    ...
) PARTITION BY RANGE (created_at);
```

**Webhook events** (high volume, most are delivered within hours):
```sql
-- Range partition by created_at (daily)
-- Old partitions dropped after 7 days (delivered events)
CREATE TABLE webhook_events (
    ...
) PARTITION BY RANGE (created_at);
```

**Agent balances, escrow_deals, payment_channels**: No partitioning needed. These are relatively small (1M agents = 1M rows for balances) and need fast point lookups by agent_id/id.

### 3.2 Read Replicas

```
                    +------------------+
                    |   Primary PG     |
                    |   (writes)       |
                    +--------+---------+
                             |
                    Streaming replication
                             |
              +--------------+-------------+
              |                            |
     +--------v--------+        +---------v--------+
     | Replica 1       |        | Replica 2        |
     | (API reads)     |        | (analytics /     |
     |                 |        |  admin dashboard) |
     +-----------------+        +------------------+
```

Implementation in SQLAlchemy:
```python
# database.py additions
_read_engine = None

def init_read_engine(database_url: str):
    """Initialize read-only engine pointing to replica."""
    global _read_engine
    _read_engine = create_engine(
        database_url,
        poolclass=QueuePool,
        pool_size=20,       # More connections for reads
        max_overflow=30,
        pool_pre_ping=True,
    )

@contextmanager
def get_db_read() -> Generator[Session, None, None]:
    """Session for read-only queries routed to replica."""
    engine = _read_engine or get_engine()  # Fallback to primary
    session = Session(bind=engine)
    session.info["readonly"] = True
    try:
        yield session
    finally:
        session.close()
```

Note: The existing `get_db_readonly()` in database.py already has a readonly session pattern with flush rejection. Extend it to use a separate engine.

### 3.3 TimescaleDB for Time-Series Data

**Recommendation: Do not adopt TimescaleDB initially.** PostgreSQL range partitioning with pg_partman handles the transaction time-series needs. TimescaleDB adds operational complexity (extension management, upgrade path) for marginal benefit at this scale.

Reconsider TimescaleDB when:
- Analytics queries require continuous aggregates (e.g., rolling 1h/24h payment volume)
- Data retention policies become complex
- Compression ratio on historical data matters for cost

### 3.4 Hot/Warm/Cold Storage Tiers

```
HOT (PostgreSQL primary):
  - Last 30 days of transactions
  - All active escrow deals, channels, streams
  - Agent balances, spending policies
  - Agent records
  
WARM (PostgreSQL read replica or separate instance):
  - 30 days to 1 year of transactions
  - Completed escrow deals
  - Historical channel states
  - Audit log (last 90 days)

COLD (S3 + Parquet):
  - Transactions older than 1 year
  - Audit log older than 90 days
  - Delivered webhook events older than 7 days
  - Compressed, queryable via DuckDB or Athena
```

Archival pipeline:
```
+-------------+       +------------+       +------------------+
| PostgreSQL  | pg_dump| S3 staging |  ETL  | S3 Parquet       |
| partition   | -----> | (CSV/JSON) | ----> | (compressed,     |
| (expired)   |       |            |       |  date-partitioned)|
+-------------+       +------------+       +------------------+
                                                    |
                                           +--------v--------+
                                           |  DuckDB / Athena|
                                           |  (ad-hoc query) |
                                           +-----------------+
```

### 3.5 Connection Management at 10K+ Concurrent Agents

Agent connections are stateless HTTP (not persistent WebSocket), so 10K concurrent agents means ~10K concurrent HTTP requests at peak.

Stack:
```
10K concurrent HTTP requests
        |
  +-----v------+
  |  nginx /   |   Connection buffering, TLS termination
  |  Envoy     |   Limit: 50K concurrent connections
  +-----+------+
        |
  +-----v------+
  | 16 API     |   Each: 15 DB connections (pool_size=5 + overflow=10)
  | workers    |   Total: 240 DB connections
  +-----+------+
        |
  +-----v------+
  | PgBouncer  |   Transaction pooling
  | pool=100   |   Multiplexes 240 app connections to 100 PG connections
  +-----+------+
        |
  +-----v------+
  | PostgreSQL |   max_connections=200 (with headroom)
  +------------+
```

### 3.6 Zero-Downtime Migration Strategy

For schema changes that do not break compatibility:
```
1. Deploy new code that handles both old and new schema
2. Run ALTER TABLE ... ADD COLUMN (non-blocking in PG for nullable columns)
3. Backfill data if needed (batched UPDATE with pg_sleep between batches)
4. Remove old-schema compatibility code in next deploy
```

For breaking changes (column rename, type change, constraint addition):
```
1. Add new column alongside old one
2. Deploy code that writes to both columns
3. Backfill old rows: UPDATE ... SET new_col = old_col WHERE new_col IS NULL
4. Deploy code that reads from new column only
5. Drop old column (ALTER TABLE ... DROP COLUMN)
```

Alembic idempotency (already in place -- good):
```python
# All migrations use IF NOT EXISTS / IF EXISTS
op.execute("CREATE INDEX IF NOT EXISTS ix_... ON ...")
```

---

## 4. Caching & State Management

### 4.1 Redis Cluster

Current: Single Redis instance (Railway managed) handling rate limiting, idempotency, deposit locking.

Target:
```
+---------------------------------------------+
|              Redis Cluster (6 nodes)         |
|                                              |
|  +-------+  +-------+  +-------+            |
|  |Master1|  |Master2|  |Master3|            |
|  |Slot   |  |Slot   |  |Slot   |            |
|  |0-5460 |  |5461-  |  |10923- |            |
|  |       |  |10922  |  |16383  |            |
|  +---+---+  +---+---+  +---+---+            |
|      |          |           |                |
|  +---v---+  +---v---+  +---v---+            |
|  |Repl 1 |  |Repl 2 |  |Repl 3 |            |
|  +-------+  +-------+  +-------+            |
+---------------------------------------------+
```

Key namespaces:
```
ratelimit:{agent_id}             -> Rate limit counters
ratelimit:ip:{action}:{ip}      -> IP-based rate limits
idempotency:{agent_id}:{key}    -> Idempotency keys (24h TTL)
balance_cache:{agent_id}:{tok}  -> Balance cache (5s TTL)
channel_state:{channel_id}      -> Channel state cache (no TTL, invalidate on update)
marketplace:{capability_hash}   -> Marketplace search results (30s TTL)
stream:events                   -> Redis Stream for event bus
deposit_monitor:poll_lock       -> Distributed lock
```

### 4.2 Cache Invalidation Strategy

**Balance changes** (critical -- must never serve stale balances for writes):
```
Write path:
  1. UPDATE agent_balances SET available = ... (PostgreSQL)
  2. DEL balance_cache:{agent_id}:{token}   (Redis)
  3. PUBLISH balance_changed {agent_id}      (Redis Pub/Sub)

Read path:
  1. GET balance_cache:{agent_id}:{token}
  2. If miss: SELECT from agent_balances, SET with 5s TTL
  3. Return cached value

Rule: Write path always invalidates cache AFTER DB commit.
      Read path tolerates 5s staleness for display purposes.
      Write path NEVER reads from cache -- always reads from primary DB.
```

**Marketplace search** (tolerates staleness):
```
Write path (agent profile update):
  1. UPDATE agents SET capabilities = ...
  2. DEL marketplace:*  (pattern delete, or versioned keys)

Read path:
  1. Hash the query parameters -> cache key
  2. GET marketplace:{hash}
  3. If miss: query PostgreSQL, SET with 30s TTL
```

### 4.3 Distributed Locking

Current: Redis SET NX for deposit monitor lock, PG advisory lock fallback. This is correct.

At scale, add Redlock for critical multi-step operations:
```python
# For operations that span multiple services
# (e.g., escrow release: debit buyer, credit seller, record fee)
import redis.lock

lock = redis_client.lock(
    f"escrow_release:{deal_id}",
    timeout=30,          # Lock auto-expires after 30s
    blocking_timeout=5,  # Wait max 5s to acquire
)
with lock:
    # Perform atomic escrow release
    ...
```

Operations requiring distributed locks:
- Escrow state transitions (already uses row-level PG locking -- keep this)
- Deposit crediting (already uses advisory lock + Redis lock)
- Channel settlement (add Redlock)
- Withdrawal processing (already has saga pattern)

### 4.4 Event-Driven Architecture

**Recommendation: Redis Streams** (not Kafka). Kafka is overkill for the current scale and adds significant operational complexity.

```
Producers (API workers):
  XADD stream:payments * type payment_created agent_id ... amount ...
  XADD stream:escrow   * type escrow_accepted deal_id ...
  XADD stream:channels * type channel_updated channel_id ...

Consumer Groups:
  XREADGROUP GROUP webhook_workers consumer_1 ...
  XREADGROUP GROUP analytics_workers consumer_1 ...
  XREADGROUP GROUP notification_workers consumer_1 ...
```

Event types:
```
payment.created, payment.confirmed, payment.failed
escrow.created, escrow.accepted, escrow.delivered, escrow.completed, escrow.expired
channel.opened, channel.updated, channel.closing, channel.settled
agent.registered, agent.updated, agent.deactivated
balance.deposited, balance.withdrawn, balance.transferred
sla.proposed, sla.accepted, sla.violated, sla.completed
```

### 4.5 WebSocket Layer

For real-time notifications to agents (balance changes, escrow state transitions, channel updates):

```
                    +------------------+
                    |   API Workers    |
                    +--------+---------+
                             |
                    XADD to Redis Stream
                             |
                    +--------v---------+
                    |  WebSocket       |
                    |  Gateway         |
                    |  (dedicated      |
                    |   service)       |
                    +--------+---------+
                             |
                    Per-agent WS connections
                             |
              +--------------+-------------+
              |              |             |
         +----v----+   +----v----+   +----v----+
         | Agent A |   | Agent B |   | Agent C |
         +---------+   +---------+   +---------+
```

Technology: **FastAPI + websockets** as a separate service, subscribing to Redis Streams.

Privacy: WebSocket messages contain only event type + resource ID. Agent must call the REST API with authentication to get full details.

### 4.6 In-Memory State for Payment Channels

Payment channels require high-frequency state updates (potentially thousands per second per channel for streaming payments). These should NOT hit PostgreSQL on every update.

```
Channel State Machine (in-memory):
  - Current nonce, balance_a, balance_b, latest signatures
  - Persisted to Redis (channel_state:{id}) on every update
  - Flushed to PostgreSQL every N updates or on channel close
  - On service restart: reconstruct from Redis, then PostgreSQL

PaymentStream State (in-memory):
  - Current accrued amount, last tick timestamp
  - Ticked every second in-memory
  - Persisted to Redis every 10 seconds
  - Flushed to PostgreSQL every 60 seconds or on stop
```

---

## 5. High Availability & Disaster Recovery

### 5.1 Multi-Region Architecture

```
                         +------------------+
                         |  Cloudflare /    |
                         |  Global LB      |
                         +---+----------+---+
                             |          |
                   +---------+          +---------+
                   |                              |
          +--------v--------+          +----------v------+
          |  Region: EU     |          |  Region: US     |
          |  (Primary)      |          |  (Hot Standby)  |
          |                 |          |                  |
          | +-------------+ |          | +--------------+ |
          | | API (8w)    | |          | | API (4w)     | |
          | +------+------+ |          | +------+------+ |
          |        |         |          |        |        |
          | +------v------+ |          | +------v------+ |
          | | PG Primary  |<------------>| PG Replica  | |
          | +------+------+ | streaming | +------+------+ |
          |        |         | repl      |        |        |
          | +------v------+ |          | +------v------+ |
          | | Redis       |<------------>| Redis       | |
          | | Primary     | | CRDT    | | Replica     | |
          | +-------------+ |          | +--------------+ |
          |                 |          |                  |
          | +-------------+ |          |                  |
          | | wallet-rpc  | |          |                  |
          | | monerod     | |          |                  |
          | +-------------+ |          |                  |
          +-----------------+          +------------------+
```

**Active-passive** for the database (Monero wallet RPC cannot be multi-primary).
**Active-active** for stateless API workers (read queries can go to either region).

### 5.2 Active-Active vs Active-Passive

**Recommendation: Active-passive with read-offloading.**

Reasons:
- Monero wallet RPC is inherently single-writer (one wallet file)
- Financial state (balances, escrow) requires strong consistency
- Split-brain in active-active with money involved is catastrophic
- Active-passive with automated failover (< 60 second RTO) is sufficient

US region serves:
- Read-only API endpoints (marketplace search, payment history, agent profiles)
- WebSocket connections for notifications
- Rate limiting (local Redis, synced via CRDT)

EU region handles:
- All writes (payments, escrow, channel updates)
- Wallet RPC operations
- Background task processing

Failover trigger: Health check failures on EU primary for > 30 seconds.

### 5.3 Database Failover

```
Failover Procedure:
  1. Primary health check fails (3 consecutive checks, 10s each)
  2. PgBouncer marks primary as down
  3. Promote replica to primary (pg_ctl promote)
  4. Update PgBouncer config to point to new primary
  5. Restart API workers to reconnect
  6. Alert on-call engineer

Replication Lag Handling:
  - Monitor pg_stat_replication.replay_lsn
  - Alert if lag > 1 second
  - Read-after-write: for 5 seconds after a write, route
    reads for that agent to the primary (sticky session via Redis)
```

### 5.4 Wallet RPC Redundancy

The Monero wallet RPC is the single most critical and least scalable component.

```
Mitigation Strategy:
  1. Primary wallet-rpc with health monitoring
  2. Cold standby wallet-rpc (same wallet file, not running)
  3. Wallet file backup every 4 hours to encrypted S3
  4. If primary fails:
     a. Stop primary wallet-rpc
     b. Copy latest wallet file to standby
     c. Start standby wallet-rpc
     d. Update API config to point to standby
     e. Recovery time: ~2-5 minutes

  For horizontal scaling of reads (balance checks, address listing):
  - Cache wallet_get_balance results in Redis (30s TTL)
  - Cache get_address results permanently (addresses are deterministic)
  - Queue withdrawals and batch-process every 60 seconds
```

### 5.5 Backup and Recovery

| Data | Backup Method | Frequency | Retention | RTO |
|---|---|---|---|---|
| PostgreSQL | pg_basebackup + WAL archiving | Continuous WAL, daily base | 30 days | < 5 min |
| Redis | RDB snapshots + AOF | AOF always, RDB hourly | 7 days | < 1 min |
| Wallet file | Encrypted copy to S3 | Every 4 hours | 90 days | < 5 min |
| Wallet seed | HSM / Vault (never in S3) | On creation | Forever | Manual |
| Application config | Git repository | On every change | Forever | < 1 min |

### 5.6 Chaos Engineering Plan

Tests to run quarterly:

| Test | Method | Expected Result |
|---|---|---|
| Kill API worker | `kill -9` one Gunicorn worker | Other workers handle traffic, no dropped payments |
| Redis failure | Block port 6379 | Rate limiter falls back to local dict (if fail_open=true) or returns 503 |
| DB failover | Promote replica | < 60s downtime, no data loss |
| Wallet RPC timeout | iptables DROP on port 18082 | Withdrawals queue, deposits pause, API remains available for ledger operations |
| Network partition | Split API from DB | Requests fail fast (30s statement_timeout), no hung connections |
| High load | Locust: 5000 req/sec | Graceful degradation, 429s returned, no OOM |
| Clock skew | Adjust system clock +5 min | Rate limiter windows may reset early, no financial impact |

---

## 6. Observability

### 6.1 Structured Logging Strategy

Current: JSON logging via `JSONFormatter` with request_id and agent_id context vars. Good foundation.

**What to log:**
```json
{
  "timestamp": "2026-04-01T12:00:00.000Z",
  "level": "INFO",
  "logger": "sthrip.escrow",
  "message": "Escrow deal completed",
  "request_id": "abc-123",
  "agent_id": "def-456",
  "trace_id": "0af7651916cd43dd",
  "span_id": "b7ad6b7169203331",
  "escrow_id": "ghi-789",
  "amount_bucket": "1-10",
  "duration_ms": 42,
  "environment": "production"
}
```

**What NOT to log (privacy):**
- Exact transaction amounts (use buckets: <0.01, 0.01-0.1, 0.1-1, 1-10, 10-100, 100+)
- Monero addresses (log only first 8 + last 4 characters)
- API key values (log only key hash prefix)
- Webhook payload contents
- Message relay ciphertext
- Agent names in aggregate metrics
- IP addresses (hash with daily rotating salt for abuse detection)

### 6.2 Metrics Pipeline

```
+-------------+       +------------+       +----------+
| API Workers | ----> | Prometheus | ----> | Grafana  |
| /metrics    |       | (scrape)   |       | (viz)    |
+-------------+       +-----+------+       +----------+
                             |
+-------------+              |
| Background  | ----------->-+
| Workers     |
| /metrics    |
+-------------+

+-------------+       +------------+
| PostgreSQL  | ----> | postgres   |
|             |       | _exporter  |
+-------------+       +-----+------+
                             |
                      +------v------+
                      | Prometheus  |
                      +-------------+
```

New metric families to add:
```python
# Business metrics (privacy-preserving)
payment_volume_total = Counter(
    "payment_volume_bucket_total",
    "Payment volume by amount bucket",
    ["bucket", "type"],  # bucket: "micro", "small", "medium", "large"
)
escrow_state_transitions = Counter(
    "escrow_state_transitions_total",
    "Escrow state transitions",
    ["from_state", "to_state"],
)
active_channels = Gauge(
    "active_channels_total",
    "Currently active payment channels",
)
active_streams = Gauge(
    "active_streams_total",
    "Currently active payment streams",
)

# Infrastructure metrics
db_pool_size = Gauge("db_pool_size", "Database connection pool size")
db_pool_checked_out = Gauge("db_pool_checked_out", "Checked out DB connections")
task_queue_depth = Gauge("task_queue_depth", "Background task queue depth", ["queue"])
wallet_rpc_latency = Histogram(
    "wallet_rpc_latency_seconds",
    "Wallet RPC call latency",
    ["method"],
)
```

### 6.3 Distributed Tracing

**Technology: OpenTelemetry** (vendor-neutral, supports Jaeger/Zipkin/Datadog backends)

```python
# Instrumentation (added to middleware.py)
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor

# Auto-instrument
FastAPIInstrumentor.instrument_app(app)
SQLAlchemyInstrumentor().instrument(engine=engine)
RedisInstrumentor().instrument()
```

Privacy in traces:
- Span attributes: request_id, agent_id (hashed), endpoint, status_code, duration
- NEVER include: amounts, addresses, API keys, payload bodies
- Sampling: 1% in production (configurable)
- Sensitive spans (wallet RPC) tagged with `privacy=high`, excluded from export to third-party backends

### 6.4 Alerting Strategy

```
Severity Levels:
  P1 (Critical) -> PagerDuty, immediate page
  P2 (High)     -> Slack #sthrip-alerts, 15 min response
  P3 (Medium)   -> Slack #sthrip-ops, next business day
  P4 (Low)      -> Grafana annotation, weekly review

Alert Rules:
  P1: API error rate > 5% for 5 minutes
  P1: Database replication lag > 10 seconds
  P1: Wallet RPC unreachable for 2 minutes
  P1: Balance mismatch detected (sum(available) != expected)
  P2: API P99 latency > 5 seconds for 10 minutes
  P2: Background task queue depth > 1000
  P2: Redis memory usage > 80%
  P2: Disk usage > 85%
  P3: Rate limit hit rate > 10% of requests
  P3: Webhook delivery failure rate > 5%
  P3: Database connection pool exhaustion events
  P4: Slow query log entries (> 1 second)
```

### 6.5 SLO/SLI Definitions

| SLI | Measurement | SLO |
|---|---|---|
| API Availability | Successful responses / Total responses (excluding 429s) | 99.9% monthly |
| Payment Latency | P99 of POST /v2/payments response time | < 500ms |
| Escrow Resolution | Time from delivery to completion/expiry | < 5 minutes after deadline |
| Deposit Crediting | Time from block confirmation to balance credit | < 2 minutes |
| Webhook Delivery | Successful delivery within retry window | 99% within 1 hour |
| Data Durability | No unrecoverable data loss | 99.999% |

Error budget: 0.1% monthly = ~43 minutes of downtime/month.

### 6.6 Privacy-Preserving Analytics

```
Analytics Pipeline:
  1. Raw events flow into Redis Streams
  2. Analytics worker consumes events
  3. Aggregates into time-bucketed counters (1 min, 1 hour, 1 day)
  4. Stores aggregates in a separate analytics DB (or TimescaleDB)
  5. No individual transaction details in analytics store

Aggregate Metrics (safe to store/display):
  - Total transaction count per hour/day
  - Total volume per hour/day (bucketed, not exact)
  - Active agent count per hour/day
  - Escrow completion rate
  - Average settlement time
  - Fee revenue (total, not per-agent)
  - Geographic distribution of requests (country-level, from hashed IPs)

Never in Analytics:
  - Individual agent activity patterns
  - Transaction graphs (who pays whom)
  - Exact amounts
  - Timing correlations that could deanonymize agents
```

---

## 7. Security Infrastructure

### 7.1 HSM Integration

```
Current Key Management:
  - ADMIN_API_KEY:          env var (Railway)
  - API_KEY_HMAC_SECRET:    env var (Railway)
  - WEBHOOK_ENCRYPTION_KEY: env var (Fernet key, Railway)
  - Wallet seed:            monero-wallet-rpc internal storage
  - Ed25519 channel keys:   generated per-channel, stored in DB

Target:
  - Wallet seed:            AWS CloudHSM / HashiCorp Vault Transit
  - HMAC secret:            Vault KV v2 with auto-rotation
  - Webhook encryption:     Vault Transit (encrypt/decrypt via API)
  - Channel signing keys:   Vault Transit for high-value channels
  - Admin API key:          Vault KV v2 with rotation policy

Migration Path:
  Phase 1: Deploy HashiCorp Vault (self-hosted or HCP)
  Phase 2: Move HMAC secret and webhook key to Vault
  Phase 3: Integrate wallet seed backup with Vault
  Phase 4: Channel key management via Vault Transit
```

### 7.2 Adaptive Rate Limiting

Current: Static tiers (LOW=10, STANDARD=100, HIGH=1000 req/min).

Target: Adaptive rate limiting based on agent behavior:

```python
# Adaptive rate limit calculation (pseudocode)
def calculate_adaptive_limit(agent_id: str) -> int:
    base_limit = get_tier_limit(agent.rate_limit_tier)
    
    # Factors that increase limit:
    trust_bonus = min(agent.trust_score * 2, 200)  # Max +200
    volume_bonus = min(agent.total_transactions // 1000, 100)  # Max +100
    
    # Factors that decrease limit:
    error_penalty = agent.recent_error_rate * base_limit  # Up to -100%
    abuse_penalty = 0
    if agent.recent_rate_limit_hits > 10:
        abuse_penalty = base_limit * 0.5  # -50% if hitting limits
    
    return max(10, base_limit + trust_bonus + volume_bonus
                   - error_penalty - abuse_penalty)
```

Additional rate limiting dimensions:
- Per-endpoint limits (wallet operations stricter than reads)
- Global rate limit across all agents (protect infrastructure)
- Burst allowance with token bucket (already in Lua script)
- Cost-based limiting (withdrawal = 10 tokens, read = 1 token)

### 7.3 DDoS Protection

```
Layer 7 (Application):
  - Cloudflare or AWS Shield in front of API
  - Challenge page for suspicious traffic
  - IP reputation database

Layer 4 (Network):
  - Railway provides basic DDoS protection
  - At scale: move to dedicated infrastructure with AWS Shield Advanced
  
Application Level:
  - Existing: IP-based rate limiting, request body size limit (1MB)
  - Add: proof-of-work challenge for registration (pow_service.py exists)
  - Add: progressive backoff for repeated 4xx errors from same IP
  - Add: CAPTCHA fallback for browser-based admin UI
```

### 7.4 Vulnerability Scanning Pipeline

```
CI/CD Pipeline:
  +-----------+     +----------+     +-----------+     +----------+
  | git push  | --> | bandit   | --> | safety    | --> | trivy    |
  |           |     | (SAST)   |     | (deps)    |     | (Docker) |
  +-----------+     +----------+     +-----------+     +----------+
                                                            |
                                                      +-----v-----+
                                                      | Snyk      |
                                                      | (license  |
                                                      |  + vulns) |
                                                      +-----------+

Weekly:
  - OWASP ZAP scan against staging environment
  - Dependency update check (Dependabot / Renovate)
  - Docker image vulnerability scan

Quarterly:
  - External penetration test (focus: API auth bypass, injection, SSRF)
  - Code audit of crypto operations (signing, encryption, key management)
```

### 7.5 Secret Rotation Automation

```
Secret Rotation Schedule:
  - ADMIN_API_KEY:         Every 90 days (manual, coordinate with admin users)
  - API_KEY_HMAC_SECRET:   Every 180 days (requires re-registration or dual-accept period)
  - WEBHOOK_ENCRYPTION_KEY: Every 90 days (dual-key period: decrypt with old, encrypt with new)
  - Database password:      Every 90 days (PgBouncer handles reconnection)
  - Redis password:         Every 90 days
  - Monero RPC password:    Every 90 days

Rotation Procedure (zero-downtime):
  1. Generate new secret
  2. Store new secret in Vault (or Railway env)
  3. Deploy code that accepts both old and new
  4. Rolling restart of workers
  5. Verify all workers use new secret
  6. Revoke old secret
```

### 7.6 mTLS for Inter-Service Communication

```
Current:
  API -> wallet-rpc:  HTTP Digest Auth (private network)
  API -> monerod:     HTTP (private network)
  API -> PostgreSQL:  SSL (Railway managed)
  API -> Redis:       TLS (Railway managed)

Target:
  API -> wallet-rpc:  mTLS (mutual TLS with client certificates)
  API -> monerod:     mTLS
  API -> PostgreSQL:  mTLS (verify-full)
  API -> Redis:       TLS with client certificate
  Worker -> API:      mTLS (for internal admin endpoints)

Implementation:
  - Certificate authority: Vault PKI secrets engine
  - Certificate rotation: Every 24 hours (short-lived certs)
  - Service mesh option: Linkerd (lightweight) if on Kubernetes
```

---

## 8. Cost Optimization

### 8.1 Railway vs Self-Hosted Cost Analysis

```
Railway (current, single instance):
  API:         $20/month (starter plan)
  PostgreSQL:  $10/month (+ $0.24/GB storage)
  Redis:       $10/month
  monerod:     $40/month (high CPU/RAM/disk)
  wallet-rpc:  $20/month
  Total:       ~$100/month

Railway at scale (16 workers, read replicas):
  API (16x):   $320/month
  PostgreSQL:  $200/month (500GB+ storage)
  Redis:       $100/month (cluster)
  monerod:     $80/month
  wallet-rpc:  $40/month
  Total:       ~$740/month

Self-hosted (Hetzner dedicated servers):
  2x AX102   (AMD EPYC, 128GB RAM, 2x NVMe):
    - API workers + PgBouncer: 1 server
    - PostgreSQL primary + monerod: 1 server
  1x AX52    (AMD Ryzen, 64GB RAM, 1x NVMe):
    - PostgreSQL replica + Redis + wallet-rpc
  Total:     ~$250/month (hardware) + $50/month (bandwidth)
             + engineering time for operations

Cloud (AWS/GCP):
  EKS/GKE cluster:     $200/month (control plane + nodes)
  RDS PostgreSQL:      $300/month (db.r6g.xlarge, multi-AZ)
  ElastiCache Redis:   $150/month (cache.r6g.large, cluster)
  EC2 for monerod:     $100/month (c6g.xlarge)
  Total:               ~$750/month + data transfer
```

**Recommendation for scaling path:**
1. Phase 1 (now to 10K agents): Stay on Railway, optimize single instance
2. Phase 2 (10K-100K agents): Migrate to Hetzner dedicated servers (best cost/performance)
3. Phase 3 (100K-1M agents): Hybrid Hetzner (compute) + managed services (DB, Redis)

### 8.2 Spot/Preemptible Instances for Workers

Background workers (escrow resolution, webhook delivery, analytics) are idempotent and can tolerate interruptions.

```
On AWS:
  - Spot instances for Dramatiq workers: ~70% cost savings
  - On-demand for API workers and wallet RPC
  - Mixed fleet: 50% spot + 50% on-demand for workers

On Hetzner:
  - No spot instances, but dedicated servers are already cheap
  - Use smaller servers for workers, larger for DB
```

### 8.3 Storage Cost Optimization

```
PostgreSQL Hot Storage:
  - 100M rows/day * 30 days * ~500 bytes/row = ~1.5TB/month
  - With partitioning: drop/archive partitions older than 30 days
  - Hot storage: ~50GB (last 30 days of active data)

Cold Storage (S3):
  - Compressed Parquet: ~10:1 compression ratio
  - 1.5TB/month raw -> ~150GB/month compressed
  - S3 Glacier Deep Archive: $0.00099/GB/month
  - Annual cost: ~$2/month for all historical data

  Alternatively, Hetzner Storage Box:
  - 1TB = ~$4/month (BX11, much cheaper than S3)
  - Encrypted rsync for backups
```

### 8.4 Serverless for Bursty Workloads

Webhook delivery is inherently bursty (bulk events after escrow completions, batch payments):

```
Option A: AWS Lambda for webhook delivery
  - Triggered by SQS queue (fed from Redis Streams)
  - Auto-scales to 1000 concurrent executions
  - Pay per invocation ($0.20 per 1M invocations)
  - At 100M events/day: ~$20/day = $600/month
  - Verdict: More expensive than dedicated workers at this scale

Option B: Dedicated webhook workers with autoscaling
  - 2-8 workers based on queue depth
  - Scale up when queue > 100 events
  - Scale down when queue empty for 5 minutes
  - Verdict: Better economics, simpler operations

Recommendation: Option B (dedicated workers with autoscaling)
```

---

## 9. Migration Roadmap

### Phase 1: Foundation (Months 1-2)

**Goal: Scale to 10K agents, 1M transactions/day**

```
Priority  Task                                              Effort  Dependencies
--------  ------------------------------------------------  ------  ------------
P0        Increase WEB_CONCURRENCY to 4-8 workers           1 day   None
P0        Add PgBouncer between app and PostgreSQL           2 days  None
P0        Extract background tasks to Dramatiq workers       2 weeks None
P1        Add PostgreSQL read replica for analytics          3 days  PgBouncer
P1        Implement CQRS: route reads to replica             1 week  Read replica
P1        Partition transactions table (by month)            3 days  None
P1        Partition audit_log table (by week)                2 days  None
P2        Add connection pool metrics to Prometheus          2 days  None
P2        Add OpenTelemetry instrumentation                  3 days  None
P2        Set up Locust benchmark suite                      3 days  None
```

Deliverables:
- 4-8 API workers handling requests
- Background tasks running independently of API workers
- Read queries offloaded to replica
- Transaction table partitioned, archival pipeline sketched
- Baseline performance metrics established

### Phase 2: Reliability (Months 3-4)

**Goal: 99.9% availability, automated failover**

```
Priority  Task                                              Effort  Dependencies
--------  ------------------------------------------------  ------  ------------
P0        Deploy HashiCorp Vault for secret management       1 week  None
P0        Automated DB failover with PgBouncer               3 days  Phase 1
P0        Wallet RPC health monitoring + cold standby        3 days  None
P1        Redis Cluster deployment (3 primary + 3 replica)   3 days  None
P1        Redis Streams event bus implementation             1 week  Redis Cluster
P1        WebSocket gateway for real-time notifications      1 week  Redis Streams
P1        Balance cache invalidation protocol                3 days  Redis Cluster
P2        Chaos engineering: first round of tests            3 days  Phase 1
P2        Alerting rules in PagerDuty/OpsGenie               2 days  Phase 1
P2        Secret rotation automation for DB/Redis/RPC        3 days  Vault
P2        mTLS for wallet-rpc communication                  2 days  Vault
```

Deliverables:
- Automated failover for all stateful components
- Event-driven architecture via Redis Streams
- Real-time WebSocket notifications
- Secret management via Vault
- Alerting pipeline operational

### Phase 3: Scale (Months 5-8)

**Goal: Scale to 100K agents, 10M transactions/day**

```
Priority  Task                                              Effort  Dependencies
--------  ------------------------------------------------  ------  ------------
P0        Migrate from Railway to Hetzner (or Kubernetes)    2 weeks Phase 2
P0        16 API workers with load balancer                  3 days  Migration
P0        Hot/warm/cold storage tiers + archival pipeline    1 week  Phase 1 partitioning
P1        Adaptive rate limiting implementation              1 week  Phase 2
P1        CQRS: dedicated read service                       1 week  Phase 1
P1        In-memory channel state with Redis persistence     1 week  Redis Cluster
P1        DDoS protection (Cloudflare or equivalent)         3 days  Migration
P2        Privacy-preserving analytics pipeline              1 week  Redis Streams
P2        Vulnerability scanning CI/CD pipeline              3 days  None
P2        Grafana dashboards: business + infrastructure      3 days  Phase 1 metrics
P2        Penetration test (external)                        2 weeks External vendor
```

Deliverables:
- Infrastructure on dedicated/cloud servers
- 16+ API workers, autoscaling workers
- Adaptive rate limiting
- Historical data archived to cold storage
- DDoS protection in place
- Analytics pipeline running

### Phase 4: Global Scale (Months 9-12)

**Goal: Scale to 1M agents, 100M transactions/day**

```
Priority  Task                                              Effort  Dependencies
--------  ------------------------------------------------  ------  ------------
P0        Multi-region deployment (EU primary, US secondary) 3 weeks Phase 3
P0        PostgreSQL streaming replication cross-region      1 week  Multi-region
P0        Read-offloading to US region                       3 days  Cross-region repl
P1        Wallet RPC batching and queuing                    1 week  Phase 2 Dramatiq
P1        Payment channel state sharding                     2 weeks Phase 3 in-memory
P1        Database connection multiplexing at scale          3 days  Phase 3
P1        TimescaleDB evaluation for analytics               1 week  Phase 3 analytics
P2        HSM integration for wallet seed                    2 weeks Vault
P2        Chaos engineering: full suite quarterly            1 week  Phase 2
P2        SLO dashboard and error budget tracking            3 days  Phase 1 metrics
P2        Cost optimization review and right-sizing          3 days  Phase 3 metrics
```

Deliverables:
- Multi-region deployment with < 60s failover
- 100M txn/day capacity validated via load testing
- HSM for critical key material
- Full chaos engineering program
- SLO tracking with error budgets

### Dependency Graph

```
Phase 1                 Phase 2                Phase 3              Phase 4
(Foundation)            (Reliability)          (Scale)              (Global)

Multi-worker ---------> DB failover ---------> Infrastructure ----> Multi-region
                                               migration
PgBouncer ------------> Redis Cluster -------> Adaptive rate -----> Channel sharding
                                               limiting
Dramatiq workers -----> Redis Streams -------> Storage tiers -----> Wallet batching
                        Event bus
Read replica ---------> WebSocket gateway ---> Dedicated read ----> Cross-region
                                               service              replication
Partitioning ---------> Balance cache -------> Archival pipeline -> TimescaleDB eval
                        invalidation
Prometheus metrics ---> Alerting rules ------> Grafana dashboards-> SLO tracking
OTel instrumentation -> Vault deployment ----> DDoS protection --> HSM integration
Locust benchmarks ----> Chaos eng round 1 ---> Pen test ---------> Chaos eng suite
```

### Risk Register

| Risk | Impact | Probability | Mitigation |
|---|---|---|---|
| Wallet RPC becomes bottleneck at scale | High | High | Batch operations, caching, queue withdrawals |
| Data loss during migration | Critical | Low | Full backup before each migration step, dry-run on staging |
| Multi-region consistency issues | High | Medium | Active-passive only, no multi-writer |
| Redis cluster split-brain | Medium | Low | Minimum 3 primaries, quorum-based failover |
| Cost overrun on cloud infrastructure | Medium | Medium | Monthly cost review, alert on spending thresholds |
| Breaking change in Monero protocol | High | Low | Pin monerod version, test upgrades on stagenet first |
| Regulatory changes requiring audit trail | Medium | Medium | Audit log already comprehensive, cold storage preserves history |

---

## Technology Decision Summary

| Decision | Choice | Alternatives Considered | Reason |
|---|---|---|---|
| Task Queue | Dramatiq + Redis | Celery, Taskiq, Huey | Lightweight, Redis broker, built-in metrics |
| Event Bus | Redis Streams | Kafka, RabbitMQ, NATS | Already in stack, sufficient throughput, simpler ops |
| Connection Pooler | PgBouncer | pgpool-II, Odyssey | Battle-tested, transaction pooling, lightweight |
| Tracing | OpenTelemetry | Jaeger direct, Datadog agent | Vendor-neutral, wide instrumentation support |
| Secret Management | HashiCorp Vault | AWS Secrets Manager, Doppler | Self-hosted option, PKI engine for mTLS |
| DDoS Protection | Cloudflare | AWS Shield, Akamai | Free tier available, easy integration |
| Infrastructure | Hetzner dedicated | AWS, GCP, Railway | 3-5x cheaper than cloud, sufficient for scale |
| Storage Archival | S3 + Parquet | TimescaleDB, BigQuery | Cost-effective, queryable via DuckDB |
| WebSocket | FastAPI separate service | Socket.IO, Centrifugo | Same stack, simple deployment |
| Event Sourcing | Not adopted | Eventstore, Marten | Complexity not justified, audit_log suffices |

---

*This document should be reviewed and updated quarterly as the system scales and requirements evolve.*
