#!/bin/bash
# Post-sync finalization script
# Run this after monerod reaches 100% sync
# Usage: ./scripts/post-sync-finalize.sh

set -e

echo "=== Checking monerod sync status ==="
LOGS=$(railway logs --service monerod --lines 3 2>&1)
echo "$LOGS"

# Check if sync is at 100%
if echo "$LOGS" | grep -q "SYNCHRONIZED OK"; then
    echo "✅ monerod is fully synchronized!"
elif echo "$LOGS" | grep -qP '\(99%|100%\)'; then
    echo "✅ monerod is at 99-100%"
else
    echo "❌ monerod is NOT fully synced yet. Wait and re-run."
    echo "$LOGS" | grep -oP '\d+%'
    exit 1
fi

echo ""
echo "=== Step 1: Switch monerod to safe DB mode ==="
railway variable set --service monerod --skip-deploys \
    "MONERO_EXTRA_ARGS=--db-sync-mode=safe:sync --out-peers 32 --max-concurrency 4"
echo "✅ monerod variables updated"

echo ""
echo "=== Step 2: Redeploy monerod with safe settings ==="
railway redeploy --service monerod --yes
echo "⏳ Waiting for monerod to start..."
sleep 60

echo ""
echo "=== Step 3: Check wallet-rpc ==="
WR_STATUS=$(railway service status --service monero-wallet-rpc 2>&1)
echo "$WR_STATUS"

echo ""
echo "=== Step 4: Verify API health ==="
HEALTH=$(curl -s https://sthrip-api-production.up.railway.app/health)
echo "$HEALTH" | python3 -m json.tool 2>/dev/null || echo "$HEALTH"

READY=$(curl -s https://sthrip-api-production.up.railway.app/ready)
echo ""
echo "Readiness: $READY"

echo ""
echo "=== Done ==="
echo "If wallet_rpc shows 'ok' in readiness → system is production-ready!"
echo "If wallet_rpc shows 'unavailable' → wallet-rpc may still be refreshing, wait 10-15 min"
