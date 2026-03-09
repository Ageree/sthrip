FROM ubuntu:22.04

ARG MONERO_VERSION=0.18.4.5
ARG MONERO_SHA256=423b49f3658e29f70a1d971667dec924c7ee7a107cfc93440456e28500b471a6

RUN apt-get update \
    && apt-get install -y --no-install-recommends wget bzip2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Download pinned Monero CLI binaries with checksum verification
RUN wget -q "https://downloads.getmonero.org/cli/monero-linux-x64-v${MONERO_VERSION}.tar.bz2" \
        -O /tmp/monero.tar.bz2 \
    && echo "${MONERO_SHA256}  /tmp/monero.tar.bz2" | sha256sum -c - \
    && tar -xjf /tmp/monero.tar.bz2 -C /opt/ \
    && mv /opt/monero-x86_64-linux-gnu-* /opt/monero \
    && rm /tmp/monero.tar.bz2

# Non-root user
RUN useradd -r -s /bin/false monero \
    && apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 18082

COPY <<'EOF' /opt/start.sh
#!/bin/sh
set -e

# Allow pausing wallet-rpc while monerod syncs
if [ "${WALLET_RPC_PAUSED:-}" = "true" ]; then
    echo "=== wallet-rpc PAUSED (WALLET_RPC_PAUSED=true) ==="
    exec sleep infinity
fi

WALLET_DIR=/data/wallets
WALLET_NAME="${MONERO_WALLET_NAME:-sthrip}"
WALLET_PASS="${MONERO_WALLET_PASS:-}"
DAEMON="${MONERO_DAEMON_ADDRESS:-node.monerodevs.org:38089}"

# Fix permissions on Railway-mounted volume (mounted as root)
mkdir -p "$WALLET_DIR"
mkdir -p /home/monero/.shared-ringdb
chown -R monero:monero /data /home/monero

# Auto-create wallet on first run
if [ ! -f "$WALLET_DIR/$WALLET_NAME.keys" ]; then
    echo "=== Creating new wallet: $WALLET_NAME ==="
    yes "" | gosu monero /opt/monero/monero-wallet-cli \
        --generate-new-wallet "$WALLET_DIR/$WALLET_NAME" \
        --password "$WALLET_PASS" \
        --daemon-address "$DAEMON" \
        --mnemonic-language English \
        ${MONERO_EXTRA_ARGS} \
        --log-file /dev/null \
        --command version
    echo "=== Wallet created ==="
fi

exec gosu monero /opt/monero/monero-wallet-rpc \
    --rpc-bind-port 18082 \
    --rpc-bind-ip 0.0.0.0 \
    --confirm-external-bind \
    --wallet-file "$WALLET_DIR/$WALLET_NAME" \
    --password "$WALLET_PASS" \
    --daemon-address "$DAEMON" \
    --rpc-login "${MONERO_RPC_USER:-sthrip}:${MONERO_RPC_PASS}" \
    --disable-rpc-ban \
    ${MONERO_EXTRA_ARGS}
EOF

ENTRYPOINT ["sh", "/opt/start.sh"]
