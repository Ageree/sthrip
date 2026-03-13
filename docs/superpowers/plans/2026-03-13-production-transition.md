# Production Transition Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transition Sthrip from stagenet to full mainnet production: merge code, configure secrets, update Railway env vars, switch Monero to mainnet, set up monitoring and backups.

**Architecture:** Railway-hosted FastAPI + PostgreSQL + Redis + Monero wallet RPC. Switch ENVIRONMENT from stagenet to production, MONERO_NETWORK from stagenet to mainnet, generate all required production secrets, restrict CORS, increase confirmations.

**Tech Stack:** Railway CLI/MCP, Python/FastAPI, Monero (mainnet), PostgreSQL, Redis

---

## Phase 1: Merge Security-Hardening Code

### Task 1: Commit uncommitted changes on feat/security-hardening

**Files:** 50 modified files (see git status)

- [ ] **Step 1:** Stage all modified files
- [ ] **Step 2:** Commit with descriptive message
- [ ] **Step 3:** Merge feat/security-hardening into main
- [ ] **Step 4:** Verify tests pass on main (1322 passed)

### Task 2: Push main to trigger Railway build

- [ ] **Step 1:** Push main to origin
- [ ] **Step 2:** Verify Railway build starts

---

## Phase 2: Generate Production Secrets

### Task 3: Generate all required secrets

- [ ] **Step 1:** Generate API_KEY_HMAC_SECRET (64 hex chars): `openssl rand -hex 32`
- [ ] **Step 2:** Generate WEBHOOK_ENCRYPTION_KEY (Fernet key): `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- [ ] **Step 3:** Document generated secrets securely (DO NOT commit to git)

---

## Phase 3: Update Railway Environment Variables

### Task 4: Set production env vars on Railway

Variables to SET:
- `ENVIRONMENT=production`
- `MONERO_NETWORK=mainnet`
- `MONERO_MIN_CONFIRMATIONS=10`
- `API_KEY_HMAC_SECRET=<generated 64-char hex>`
- `WEBHOOK_ENCRYPTION_KEY=<generated Fernet key>`
- `CORS_ORIGINS=` (empty string = reject all CORS, or specific domains)
- `LOG_FORMAT=json`
- `LOG_LEVEL=INFO`

Variables to VERIFY (already set correctly):
- `DATABASE_URL` (Railway auto-injected)
- `REDIS_URL` (Railway auto-injected)
- `ADMIN_API_KEY` (already 64+ chars)
- `HUB_MODE=onchain`
- `MONERO_RPC_HOST=monero-wallet-rpc.railway.internal`
- `MONERO_RPC_PORT=18082`

---

## Phase 4: Switch Monero to Mainnet

### Task 5: Reconfigure monerod for mainnet

The current monerod runs with `--stagenet`. For mainnet:
- Remove `--stagenet` flag from monerod startup
- Add `--prune-blockchain` for faster sync
- This requires redeployment of the monerod service
- **Sync time:** Pruned mainnet ~24-72 hours

### Task 6: Reconfigure monero-wallet-rpc for mainnet

- Remove `--stagenet` from wallet-rpc startup
- Create new mainnet wallet
- Update RPC credentials if needed

**NOTE:** Mainnet sync is a multi-day process. The API will start in production mode but wallet operations will return errors until sync completes. This is expected — the health endpoint will reflect wallet unavailability.

---

## Phase 5: Deploy and Verify

### Task 7: Deploy and verify health

- [ ] **Step 1:** Trigger Railway redeploy after env var changes
- [ ] **Step 2:** Check `/ready` endpoint returns 200
- [ ] **Step 3:** Check `/health` endpoint shows component status
- [ ] **Step 4:** Verify logs show `ENVIRONMENT=production`, `MONERO_NETWORK=mainnet`
- [ ] **Step 5:** Test agent registration flow
- [ ] **Step 6:** Test admin dashboard login

---

## Phase 6: Monitoring and Backups

### Task 8: Configure monitoring

- [ ] **Step 1:** Set `SENTRY_DSN` if Sentry account available
- [ ] **Step 2:** Set `ALERT_WEBHOOK_URL` for Telegram/Discord alerts
- [ ] **Step 3:** Verify Betterstack logging works (BETTERSTACK_SOURCE_TOKEN already set)

### Task 9: Database backup strategy

- [ ] **Step 1:** Enable Railway PostgreSQL automatic backups (Railway Pro plan)
- [ ] **Step 2:** Document manual backup procedure: `pg_dump` via Railway CLI
