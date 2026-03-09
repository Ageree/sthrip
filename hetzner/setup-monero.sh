#!/bin/bash
# Sthrip Monero Node Setup for Hetzner
# Run this on your Hetzner CX11 VPS (Ubuntu 22.04)

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Configuration
MONERO_VERSION="v0.18.3.1"
MONERO_DIR="/opt/monero"
WALLET_DIR="/opt/monero/wallets"
DATA_DIR="/opt/monero/data"
RPC_USER="${MONERO_RPC_USER:-sthrip}"
RPC_PASS="${MONERO_RPC_PASS:-$(openssl rand -base64 32)}"
WALLET_NAME="${MONERO_WALLET_NAME:-sthrip}"
WALLET_PASS="${MONERO_WALLET_PASS:-$(openssl rand -base64 32)}"

echo "=== Sthrip Monero Node Setup ==="
echo "This will install Monero daemon and wallet RPC"
echo ""

# Update system
log "Updating system..."
apt-get update
apt-get upgrade -y

# Install dependencies
log "Installing dependencies..."
apt-get install -y \
    wget \
    curl \
    unzip \
    jq \
    ufw \
    fail2ban \
    htop \
    tmux \
    logrotate

# Create directories
log "Creating directories..."
mkdir -p $MONERO_DIR $WALLET_DIR $DATA_DIR

# Download Monero
log "Downloading Monero $MONERO_VERSION..."
cd /tmp
wget -q --show-progress "https://downloads.getmonero.org/cli/linux64" -O monero-linux-x64.tar.bz2
tar -xjf monero-linux-x64.tar.bz2
mv monero-x86_64-linux-gnu-* monero

# Install binaries
log "Installing Monero binaries..."
cp monero/monerod monero/monero-wallet-rpc monero/monero-wallet-cli /usr/local/bin/
chmod +x /usr/local/bin/monerod /usr/local/bin/monero-wallet-rpc /usr/local/bin/monero-wallet-cli

# Clean up
rm -rf monero monero-linux-x64.tar.bz2

# Create monero user
log "Creating monero user..."
id -u monero &>/dev/null || useradd -r -s /bin/false monero
chown -R monero:monero $MONERO_DIR

# Configure Firewall
log "Configuring firewall..."
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    # SSH
ufw allow 18080/tcp # Monero P2P
ufw allow 18081/tcp # Monero restricted RPC (optional)
ufw allow from 127.0.0.1 to any port 18082  # Wallet RPC (localhost only!)
ufw --force enable

# Create monerod config
log "Creating monerod configuration..."
cat > /etc/monerod.conf <<EOF
# Monero Daemon Configuration
data-dir=$DATA_DIR
log-file=/var/log/monero/monerod.log
max-log-file-size=104850000
log-level=0

# Network
p2p-bind-ip=0.0.0.0
p2p-bind-port=18080
no-igd=1

# RPC (restricted, for health checks only)
rpc-bind-ip=127.0.0.1
rpc-bind-port=18081
confirm-external-bind=0
restricted-rpc=1
no-zmq=1

# Performance
db-sync-mode=safe:sync
out-peers=32
in-peers=32

# Bandwidth limits (adjust as needed)
limit-rate-up=2048
limit-rate-down=8192
EOF

# Create systemd service for monerod
log "Creating monerod service..."
cat > /etc/systemd/system/monerod.service <<EOF
[Unit]
Description=Monero Full Node
After=network.target

[Service]
Type=simple
User=monero
Group=monero
WorkingDirectory=$MONERO_DIR
ExecStart=/usr/local/bin/monerod --config-file /etc/monerod.conf --non-interactive
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=monerod

[Install]
WantedBy=multi-user.target
EOF

# Setup log directory
mkdir -p /var/log/monero
chown monero:monero /var/log/monero

# Create wallet RPC config
log "Creating wallet RPC configuration..."
cat > /etc/monero-wallet-rpc.conf <<EOF
# Monero Wallet RPC Configuration
wallet-dir=$WALLET_DIR
log-file=/var/log/monero/wallet-rpc.log
log-level=0

# RPC Settings
rpc-bind-port=18082
rpc-bind-ip=0.0.0.0
confirm-external-bind=1
rpc-login=$RPC_USER:$RPC_PASS

# Security
disable-rpc-login=0
trusted-daemon=1
EOF

# Create systemd service for wallet RPC
log "Creating wallet RPC service..."
cat > /etc/systemd/system/monero-wallet-rpc.service <<EOF
[Unit]
Description=Monero Wallet RPC
After=network.target monerod.service
Wants=monerod.service

[Service]
Type=simple
User=monero
Group=monero
WorkingDirectory=$MONERO_DIR
ExecStart=/usr/local/bin/monero-wallet-rpc --config-file /etc/monero-wallet-rpc.conf --daemon-address 127.0.0.1:18081
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=monero-wallet-rpc

[Install]
WantedBy=multi-user.target
EOF

# Create wallet creation script
log "Creating wallet setup script..."
cat > /opt/monero/create-wallet.sh <<'EOF'
#!/bin/bash
# Create new wallet for Sthrip

WALLET_NAME="${1:-sthrip}"
WALLET_DIR="/opt/monero/wallets"
WALLET_FILE="$WALLET_DIR/$WALLET_NAME"

echo "Creating wallet: $WALLET_NAME"
echo "This will generate a new wallet and show you the seed phrase."
echo "IMPORTANT: Save the seed phrase securely!"
echo ""

# Generate random password if not set
if [ -z "$WALLET_PASS" ]; then
    WALLET_PASS=$(openssl rand -base64 32)
    echo "Generated wallet password: $WALLET_PASS"
    echo "SAVE THIS PASSWORD!"
    echo ""
fi

# Create wallet
cd $WALLET_DIR
/usr/local/bin/monero-wallet-cli --generate-new-wallet "$WALLET_FILE" \
    --password "$WALLET_PASS" \
    --daemon-address 127.0.0.1:18081 \
    --command "exit"

echo ""
echo "Wallet created: $WALLET_FILE"
echo "Password: $WALLET_PASS"
echo ""
echo "To view seed phrase, run:"
echo "  monero-wallet-cli --wallet-file $WALLET_FILE --password $WALLET_PASS"
echo "Then type: seed"
EOF

chmod +x /opt/monero/create-wallet.sh

# Create status check script
cat > /usr/local/bin/monero-status <<'EOF'
#!/bin/bash
echo "=== Monero Node Status ==="
echo ""
echo "Daemon Status:"
systemctl is-active monerod || echo "Not running"
echo ""
echo "Wallet RPC Status:"
systemctl is-active monero-wallet-rpc || echo "Not running"
echo ""
echo "Blockchain Height:"
curl -s -X POST http://127.0.0.1:18081/json_rpc \
    -d '{"jsonrpc":"2.0","id":"0","method":"get_block_count"}' \
    -H 'Content-Type: application/json' 2>/dev/null | jq -r '.result.count' || echo "N/A"
echo ""
echo "Network Connections:"
curl -s -X POST http://127.0.0.1:18081/json_rpc \
    -d '{"jsonrpc":"2.0","id":"0","method":"get_connections"}' \
    -H 'Content-Type: application/json' 2>/dev/null | jq '.result.connections | length' || echo "N/A"
echo ""
echo "Disk Usage:"
df -h /opt/monero
echo ""
echo "Memory Usage:"
free -h
echo ""
echo "Recent Logs:"
journalctl -u monerod --no-pager -n 5
EOF

chmod +x /usr/local/bin/monero-status

# Configure logrotate
log "Setting up log rotation..."
cat > /etc/logrotate.d/monero <<EOF
/var/log/monero/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 monero monero
    sharedscripts
    postrotate
        systemctl reload monerod 2>/dev/null || true
    endscript
}
EOF

# Setup fail2ban for SSH protection
log "Configuring fail2ban..."
cat > /etc/fail2ban/jail.local <<EOF
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 3

[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 3
EOF

systemctl restart fail2ban

# Start services
log "Starting Monero services..."
systemctl daemon-reload
systemctl enable monerod
systemctl enable monero-wallet-rpc
systemctl start monerod

echo ""
echo "=== Setup Complete! ==="
echo ""
echo "IMPORTANT: SAVE THESE CREDENTIALS"
echo "=========================================="
echo "RPC Username: $RPC_USER"
echo "RPC Password: $RPC_PASS"
echo ""
echo "Wallet Name: $WALLET_NAME"
echo "Wallet Password: $WALLET_PASS"
echo "=========================================="
echo ""
echo "Services Status:"
echo "  - Daemon (monerod): $(systemctl is-active monerod)"
echo "  - Wallet RPC: $(systemctl is-active monero-wallet-rpc)"
echo ""
echo "Useful Commands:"
echo "  monero-status          - Check node status"
echo "  sudo systemctl status monerod     - View daemon logs"
echo "  sudo systemctl status monero-wallet-rpc  - View RPC logs"
echo "  /opt/monero/create-wallet.sh      - Create new wallet"
echo ""
echo "Wait for blockchain sync (~24-48 hours for full sync)"
echo "Check progress: monero-status"
echo ""
echo "For Railway connection, use these credentials:"
echo "  Host: $(curl -s ifconfig.me)"
echo "  Port: 18082"
echo "  User: $RPC_USER"
echo "  Pass: $RPC_PASS"
