#!/bin/bash
# Check monerod sync status on Railway
# Usage: ./scripts/check-monerod-sync.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Monerod Sync Status ==="
echo "Time: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# Get last 5 Synced lines from monerod logs
LOGS=$(cd "$PROJECT_DIR" && railway logs --service monerod -n 200 2>/dev/null | grep -i "Synced\|Failure\|stopped\|error" | tail -5)

if [ -z "$LOGS" ]; then
    echo "No sync data found in recent logs. Node may be restarting."
    exit 1
fi

echo "$LOGS"
echo ""

# Extract latest height and target
LATEST=$(echo "$LOGS" | grep "Synced" | tail -1)
if [ -n "$LATEST" ]; then
    HEIGHT=$(echo "$LATEST" | grep -oP 'Synced \K[0-9]+')
    TARGET=$(echo "$LATEST" | grep -oP 'Synced [0-9]+/\K[0-9]+')
    SPEED=$(echo "$LATEST" | grep -oP '[0-9]+\.[0-9]+ blocks/sec')
    QUEUE=$(echo "$LATEST" | grep -oP '[0-9]+\.[0-9]+ MB queued')

    if [ -n "$HEIGHT" ] && [ -n "$TARGET" ]; then
        LEFT=$((TARGET - HEIGHT))
        PCT=$(echo "scale=2; $HEIGHT * 100 / $TARGET" | bc)
        echo "--- Summary ---"
        echo "Height:   $HEIGHT / $TARGET ($PCT%)"
        echo "Left:     $LEFT blocks"
        echo "Speed:    ${SPEED:-unknown}"
        echo "Queue:    ${QUEUE:-0 MB}"

        if [ -n "$SPEED" ]; then
            BPS=$(echo "$SPEED" | grep -oP '[0-9]+\.[0-9]+')
            if [ "$(echo "$BPS > 0" | bc)" -eq 1 ]; then
                SECS=$(echo "scale=0; $LEFT / $BPS" | bc)
                DAYS=$((SECS / 86400))
                HOURS=$(( (SECS % 86400) / 3600 ))
                echo "ETA:      ${DAYS}d ${HOURS}h"
            fi
        fi
    fi
fi
