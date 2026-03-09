# Rollback Procedures

## Railway Deployment Rollback

### Quick rollback (< 1 min)
Railway keeps previous deployments. To rollback:
1. Open Railway dashboard → sthrip-api service
2. Go to **Deployments** tab
3. Click the previous successful deployment
4. Click **Redeploy**

### CLI rollback
```bash
railway service  # select sthrip-api
railway deployments  # list recent deployments
railway rollback  # rollback to previous
```

## Database Migration Rollback

### Check current migration
```bash
railway run alembic current
```

### Rollback one step
```bash
railway run alembic downgrade -1
```

### Rollback to specific revision
```bash
railway run alembic downgrade <revision_id>
```

### Emergency: skip migrations entirely
The API lifespan handler skips Alembic if tables already exist:
```python
# In api/main_v2.py lifespan()
conn.execute(text("SELECT 1 FROM agents LIMIT 0"))
# If this succeeds → skip Alembic
```

## Balance/Financial Rollback

### If a payment was processed incorrectly
1. Find the payment: `SELECT * FROM hub_routes WHERE payment_id = 'hp_...'`
2. Check fee collection: `SELECT * FROM fee_collections WHERE source_id = <route_id>`
3. Manual balance adjustment via admin SQL (requires DB access):
```sql
-- Credit back sender
UPDATE agent_balances SET available = available + <amount>
WHERE agent_id = '<sender_id>';
-- Deduct from recipient
UPDATE agent_balances SET available = available - <amount>
WHERE agent_id = '<recipient_id>';
-- Mark route as failed
UPDATE hub_routes SET status = 'failed' WHERE payment_id = 'hp_...';
```

## Wallet RPC Failure

### If wallet-rpc is unresponsive
1. Check service: Railway dashboard → monero-wallet-rpc
2. Restart: **Redeploy** the service
3. Wallet state persists in Railway volume

### If withdrawal was deducted but RPC failed
The API automatically rolls back the balance:
```python
# In api/routers/balance.py
except Exception as e:
    repo.credit(agent.id, amount)  # auto-rollback
```
Check audit log: `SELECT * FROM audit_log WHERE action = 'balance.withdraw' ORDER BY created_at DESC`

## Environment Variable Emergency

### If a secret is leaked
1. **Immediately** rotate on Railway dashboard:
   - `ADMIN_API_KEY`: `openssl rand -hex 32`
   - `MONERO_RPC_PASS`: change in both wallet-rpc and API service
2. Redeploy API service (picks up new vars)
3. Notify affected agents to rotate their API keys via `/v2/me/rotate-key`

### If CORS blocks legitimate clients
Add the domain to `CORS_ORIGINS` env var (comma-separated):
```
CORS_ORIGINS=https://app.sthrip.io,https://dashboard.sthrip.io
```
Redeploy — no code change needed.

## Monitoring

### Check if rollback is needed
- `/health` returns non-healthy status
- `/ready` returns 503
- `/metrics` shows spike in 5xx errors
- Sentry alerts for unhandled exceptions
- Railway deployment logs show crash loops

### Post-rollback verification
1. `curl https://<api-url>/health` → `{"status": "healthy"}`
2. `curl https://<api-url>/ready` → `{"status": "ready"}`
3. Register a test agent → verify 201
4. Check balance endpoint → verify 200
