# Sthrip Makefile - Real Monero Setup

.PHONY: help install-monero create-wallet start-rpc test balance

MONERO_DIR = $(HOME)/monero
WALLET_DIR = $(HOME)/sthrip-wallet
DAEMON_HOST = node.moneroworld.com:18089

help:
	@echo "🥷 Sthrip - Real Monero Setup"
	@echo "=================================="
	@echo ""
	@echo "Available commands:"
	@echo "  make install-monero  - Download and install Monero binaries"
	@echo "  make create-wallet   - Create new wallet (interactive)"
	@echo "  make start-rpc       - Start wallet RPC server"
	@echo "  make test            - Test connection"
	@echo "  make balance         - Check balance"
	@echo ""
	@echo "Quick start: make install-monero -> make create-wallet -> make start-rpc"

install-monero:
	@echo "📥 Downloading Monero..."
	@mkdir -p $(MONERO_DIR)
	@cd /tmp && curl -L -o monero-mac.tar.bz2 "https://downloads.getmonero.org/cli/monero-mac-armv8-v0.18.3.4.tar.bz2"
	@cd /tmp && tar -xjf monero-mac.tar.bz2
	@cp /tmp/monero-aarch64-apple-darwin*/monero-* $(MONERO_DIR)/
	@echo "✅ Monero installed to $(MONERO_DIR)"
	@echo "Add to your .zshrc: export PATH=\$$PATH:$(MONERO_DIR)"

create-wallet:
	@mkdir -p $(WALLET_DIR)
	@echo "🔐 Creating wallet..."
	@echo "Instructions:"
	@echo "  1. Select language (1 for English)"
	@echo "  2. Press Enter for no password (or enter password)"
	@echo "  3. Press Enter to confirm password"
	@echo ""
	@cd $(WALLET_DIR) && $(MONERO_DIR)/monero-wallet-cli \
		--generate-new-wallet sthrip \
		--daemon-address $(DAEMON_HOST)
	@echo ""
	@echo "✅ Wallet created in $(WALLET_DIR)"
	@echo "📝 SAVE YOUR SEED PHRASE SECURELY!"

start-rpc:
	@echo "🚀 Starting Monero Wallet RPC..."
	@echo "Connecting to $(DAEMON_HOST)"
	@echo ""
	@cd $(WALLET_DIR) && $(MONERO_DIR)/monero-wallet-rpc \
		--wallet-file sthrip \
		--password "" \
		--rpc-bind-port 18082 \
		--rpc-bind-ip 127.0.0.1 \
		--daemon-address $(DAEMON_HOST) \
		--confirm-external-bind \
		--trusted-daemon

test:
	@echo "🧪 Testing Sthrip connection..."
	@python3 -c "from sthrip import Sthrip; a = Sthrip.from_env(); print(f'✅ Connected! Balance: {a.balance} XMR')"

balance:
	@python3 -c "from sthrip import Sthrip; a = Sthrip.from_env(); info = a.get_info(); print(f'💰 Balance: {info.balance} XMR'); print(f'📍 Address: {info.address}')"

get-xmr:
	@echo "💡 How to get XMR:"
	@echo ""
	@echo "Option 1 - Buy on exchange:"
	@echo "  • Kraken, Binance, KuCoin"
	@echo "  • Withdraw to your address (show with: make balance)"
	@echo ""
	@echo "Option 2 - P2P (no KYC):"
	@echo "  • LocalMonero (localmonero.co)"
	@echo "  • HodlHodl"
	@echo ""
	@echo "Option 3 - Earn:"
	@echo "  • Sell data/services via Sthrip"
	@echo "  • Mining (not recommended for small amounts)"
	@echo ""
	@echo "⚠️  NEVER buy XMR from suspicious sources!"

faucet:
	@echo "🚰 Testnet Faucet (for testing only):"
	@echo "  https://community.xmr.to/faucet/testnet/"
	@echo ""
	@echo "To use testnet:"
	@echo "  make create-wallet TESTNET=1"
