# Sthrip Deployment Guide

Complete guide for deploying Sthrip in production.

## Quick Start

```bash
# Clone repository
git clone https://github.com/yourorg/sthrip.git
cd sthrip

# Start with Docker Compose
docker-compose up -d monerod monero-wallet-rpc

# Check logs
docker-compose logs -f monero-wallet-rpc
```

## Production Deployment

### 1. Infrastructure Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| Monero Node | 4 GB RAM, 100 GB SSD | 8 GB RAM, 500 GB NVMe |
| Wallet RPC | 1 GB RAM | 2 GB RAM |
| SDK Service | 512 MB RAM | 1 GB RAM |
| Network | 10 Mbps | 100 Mbps |

### 2. Server Setup

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# Install Docker Compose
sudo apt install docker-compose-plugin

# Create directories
mkdir -p /opt/sthrip/{data,wallets,logs}
chmod 700 /opt/sthrip/wallets
```

### 3. Environment Configuration

Create `.env` file:

```bash
# Monero Wallet
WALLET_PASSWORD=your_secure_password_here

# RPC Auth (optional but recommended)
RPC_USER=sthrip
RPC_PASS=your_rpc_password_here

# Network
MONERO_NETWORK=mainnet  # or stagenet for testing
```

### 4. Docker Compose Production

```yaml
version: '3.8'

services:
  monerod:
    image: ghcr.io/sethforprivacy/simple-monerod:latest
    restart: always
    ports:
      - "18080:18080"
      - "127.0.0.1:18081:18081"  # Restrict RPC to localhost
    volumes:
      - /opt/sthrip/data:/home/monero/.bitmonero
    command:
      - --rpc-restricted-bind-ip=0.0.0.0
      - --rpc-restricted-bind-port=18081
      - --confirm-external-bind
      - --no-igd
      - --enable-dns-blocklist
      - --prune-blockchain  # Saves disk space
      - --db-sync-mode=safe:sync:1000
    logging:
      driver: "json-file"
      options:
        max-size: "100m"
        max-file: "5"

  monero-wallet-rpc:
    image: ghcr.io/sethforprivacy/simple-monero-wallet-rpc:latest
    restart: always
    ports:
      - "127.0.0.1:18082:18082"  # Localhost only!
    volumes:
      - /opt/sthrip/wallets:/home/monero/wallets
    environment:
      - WALLET_FILE=/home/monero/wallets/sthrip
      - WALLET_PASSWORD=${WALLET_PASSWORD}
      - RPC_BIND_PORT=18082
      - RPC_BIND_IP=0.0.0.0
      - DAEMON_HOST=monerod
      - DAEMON_PORT=18081
      - CONFIRM_EXTERNAL_BIND=true
    depends_on:
      - monerod
    logging:
      driver: "json-file"
      options:
        max-size: "50m"
        max-file: "3"

  # Optional: Monitoring
  prometheus:
    image: prom/prometheus:latest
    restart: always
    ports:
      - "127.0.0.1:9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
    profiles: ["monitoring"]

  grafana:
    image: grafana/grafana:latest
    restart: always
    ports:
      - "127.0.0.1:3000:3000"
    volumes:
      - grafana_data:/var/lib/grafana
    profiles: ["monitoring"]

volumes:
  grafana_data:
```

### 5. Security Checklist

- [ ] Firewall: Only port 18080 (P2P) exposed externally
- [ ] Wallet RPC bound to localhost only (127.0.0.1:18082)
- [ ] Strong wallet password
- [ ] RPC authentication enabled
- [ ] Regular backups of wallet files
- [ ] Log rotation configured
- [ ] Non-root Docker containers
- [ ] Auto-updates with Watchtower

### 6. Backup Strategy

```bash
#!/bin/bash
# backup.sh - Run daily via cron

BACKUP_DIR="/backup/sthrip/$(date +%Y%m%d)"
mkdir -p $BACKUP_DIR

# Backup wallet files
docker cp sthrip-monero-wallet-rpc-1:/home/monero/wallets $BACKUP_DIR/

# Encrypt backup
gpg --symmetric --cipher-algo AES256 $BACKUP_DIR/wallets.tar.gz

# Upload to S3 (optional)
aws s3 cp $BACKUP_DIR/wallets.tar.gz.gpg s3://your-backup-bucket/

# Cleanup old backups
find /backup/sthrip -type d -mtime +7 -exec rm -rf {} \;
```

### 7. Monitoring

```bash
# Check node status
curl -X POST http://localhost:18081/json_rpc \
  -d '{"jsonrpc":"2.0","id":"0","method":"get_info"}'

# Check wallet status
curl -X POST http://localhost:18082/json_rpc \
  -d '{"jsonrpc":"2.0","id":"0","method":"get_balance"}'

# View logs
sudo docker-compose logs -f --tail 100
```

### 8. Troubleshooting

**Problem:** Wallet RPC connection refused
```bash
# Check if container is running
docker-compose ps

# Check logs
docker-compose logs monero-wallet-rpc

# Verify wallet exists
docker-compose exec monero-wallet-rpc ls -la /home/monero/wallets/
```

**Problem:** Node not syncing
```bash
# Check block height
curl -X POST http://localhost:18081/json_rpc \
  -d '{"jsonrpc":"2.0","id":"0","method":"get_info"}' | jq .result.height

# Compare with https://xmrchain.net/
```

**Problem:** Out of disk space
```bash
# Prune blockchain (keeps only recent blocks)
docker-compose exec monerod monerod prune-blockchain

# Or use pruned node from start
```

## Cloud Deployment

### AWS EC2

```bash
# 1. Launch t3.large instance with Ubuntu 22.04
# 2. Attach 500 GB EBS volume for blockchain
# 3. Open port 18080 in security group
# 4. Run setup above
```

### Hetzner / DigitalOcean

```bash
# Recommended: CPX31 (4 vCPU, 8 GB RAM, 160 GB NVMe)
# Add 400 GB volume for blockchain data

# Mount volume
sudo mkfs.ext4 /dev/sdb
sudo mount /dev/sdb /opt/sthrip/data
```

## Scaling

### Multiple Agents

```python
# Each agent gets its own wallet file
# agent_1_wallet, agent_2_wallet, etc.

# Or use accounts within single wallet
agent = Sthrip(
    rpc_host="localhost",
    rpc_port=18082,
    account_index=0  # Different for each agent
)
```

### Load Balancing

```yaml
# Use multiple wallet RPC instances behind nginx
nginx:
  image: nginx:alpine
  ports:
    - "18082:18082"
  volumes:
    - ./nginx.conf:/etc/nginx/nginx.conf
```

## Cost Estimation

| Component | Monthly Cost |
|-----------|-------------|
| VPS (4 vCPU, 8 GB, 500 GB) | $40-80 |
| Backup storage (S3) | $5-10 |
| Bandwidth | $5-20 |
| **Total** | **$50-110** |

## Support

- Issues: https://github.com/yourorg/sthrip/issues
- Docs: https://docs.sthrip.io
- Discord: https://discord.gg/sthrip
