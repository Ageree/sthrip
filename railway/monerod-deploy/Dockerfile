FROM ubuntu:22.04

ARG MONERO_VERSION=0.18.4.5
ARG MONERO_SHA256=423b49f3658e29f70a1d971667dec924c7ee7a107cfc93440456e28500b471a6

RUN apt-get update \
    && apt-get install -y --no-install-recommends wget bzip2 ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/*

# Download pinned Monero CLI binaries with checksum verification
RUN wget -q "https://downloads.getmonero.org/cli/monero-linux-x64-v${MONERO_VERSION}.tar.bz2" \
        -O /tmp/monero.tar.bz2 \
    && echo "${MONERO_SHA256}  /tmp/monero.tar.bz2" | sha256sum -c - \
    && tar -xjf /tmp/monero.tar.bz2 -C /opt/ \
    && mv /opt/monero-x86_64-linux-gnu-* /opt/monero \
    && rm /tmp/monero.tar.bz2

# Non-root user
RUN useradd -r -m -s /bin/false monero

EXPOSE 18081 18089

COPY <<'EOF' /opt/start.sh
#!/bin/sh
set -e

DATA_DIR="${MONEROD_DATA_DIR:-/data/monerod}"

# Fix permissions on Railway-mounted volume
mkdir -p "$DATA_DIR"
chown -R monero:monero /data

exec gosu monero /opt/monero/monerod \
    --non-interactive \
    --data-dir "$DATA_DIR" \
    --p2p-bind-ip 0.0.0.0 \
    --p2p-bind-port 38080 \
    --rpc-bind-ip 0.0.0.0 \
    --rpc-bind-port 18081 \
    --confirm-external-bind \
    ${MONEROD_RPC_MODE:---restricted-rpc} \
    --no-igd \
    --enable-dns-blocklist \
    --prune-blockchain \
    --sync-pruned-blocks \
    ${MONERO_EXTRA_ARGS} \
    ${MONEROD_NETWORK_FLAG}
EOF

ENTRYPOINT ["sh", "/opt/start.sh"]
