#!/bin/sh
# Sthrip Tor sidecar entrypoint.
#
# 1. Start `tor` in the background so we can dump the .onion address
#    once the v3 hidden-service descriptor is generated. This is purely
#    a one-shot operator convenience — once captured, the operator pastes
#    the address into the API service's STHRIP_ONION_ENDPOINT env var.
# 2. Wait up to 60 s for /var/lib/tor/sthrip-hsv3/hostname to appear.
#    On a fresh volume the first boot generates the keys + hostname; on
#    subsequent boots the file is already there from the persistent vol.
# 3. Hand control back to tor (foreground via `wait`) so the container
#    process supervisor (Railway / tini) tracks the right PID.
#
# Notes:
# - Do NOT log the .onion at info-level on subsequent runs; the address is
#   not a secret per se but we keep stdout clean.
# - This script does not write the env var anywhere; that's the operator's
#   responsibility per the runbook.

set -e

HOSTNAME_FILE="/var/lib/tor/sthrip-hsv3/hostname"

echo "[sthrip-tor] starting tor..."
tor -f /etc/tor/torrc &
TOR_PID=$!

# Poll for the v3 hostname file.
i=0
while [ $i -lt 60 ]; do
    if [ -f "$HOSTNAME_FILE" ]; then
        ADDR=$(cat "$HOSTNAME_FILE")
        echo "[sthrip-tor] ONION_ADDRESS=${ADDR}"
        echo "[sthrip-tor] copy this into STHRIP_ONION_ENDPOINT on the API service"
        break
    fi
    i=$((i + 1))
    sleep 1
done

if [ ! -f "$HOSTNAME_FILE" ]; then
    echo "[sthrip-tor] WARNING: hostname not generated within 60s; check tor logs above"
fi

# Foreground tor. tini will forward SIGTERM here.
wait $TOR_PID
