#!/bin/bash
# Sthrip Setup Script
# Usage: ./scripts/setup.sh [dev|prod]

set -e

MODE=${1:-dev}
echo "Setting up Sthrip in $MODE mode..."

# Create necessary directories
echo "Creating directories..."
mkdir -p data/bitcoin
mkdir -p data/monero
mkdir -p data/mpc_keys
mkdir -p logs
mkdir -p contracts

# Check dependencies
echo "Checking dependencies..."
command -v docker >/dev/null 2>&1 || { echo "Docker required but not installed."; exit 1; }
command -v docker-compose >/dev/null 2>&1 || { echo "Docker Compose required but not installed."; exit 1; }

# Generate MPC keys if not exist
if [ ! -f "data/mpc_keys/key_shares.json" ]; then
    echo "Generating MPC key shares..."
    python3 scripts/generate_mpc_keys.py --nodes 5 --threshold 3 --output data/mpc_keys/
fi

# Setup environment
echo "Setting up environment..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "Created .env file. Please edit it with your configuration."
fi

# Build images
echo "Building Docker images..."
docker-compose -f docker-compose.full.yml build

# Start infrastructure
echo "Starting infrastructure..."
docker-compose -f docker-compose.full.yml up -d bitcoin monero monero-wallet ethereum redis vault

# Wait for services
echo "Waiting for services to be ready..."
sleep 30

# Generate blocks for Bitcoin
echo "Generating initial Bitcoin blocks..."
docker-compose -f docker-compose.full.yml exec -T bitcoin bitcoin-cli -regtest -rpcuser=bitcoin -rpcpassword=bitcoin generatetoaddress 101 $(docker-compose -f docker-compose.full.yml exec -T bitcoin bitcoin-cli -regtest -rpcuser=bitcoin -rpcpassword=bitcoin getnewaddress)

# Deploy contracts if in dev mode
if [ "$MODE" = "dev" ]; then
    echo "Deploying smart contracts..."
    docker-compose -f docker-compose.full.yml run --rm ethereum npx hardhat run /contracts/deploy.js --network localhost
fi

echo ""
echo "=============================================="
echo "Setup complete!"
echo ""
echo "Services:"
echo "  Bitcoin RPC:    http://localhost:18443"
echo "  Monero RPC:     http://localhost:38081"
echo "  Monero Wallet:  http://localhost:38082"
echo "  Ethereum:       http://localhost:8545"
echo "  API:            http://localhost:8000"
echo "  Prometheus:     http://localhost:9090"
echo "  Grafana:        http://localhost:3000"
echo ""
echo "To start MPC nodes:"
echo "  docker-compose -f docker-compose.full.yml up -d mpc-node-1 mpc-node-2 mpc-node-3 mpc-node-4 mpc-node-5"
echo ""
echo "=============================================="
