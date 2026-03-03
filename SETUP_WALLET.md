# Real Monero Wallet Setup

Step-by-step guide to create a real Monero wallet for StealthPay.

## Prerequisites

Monero binaries already installed at `~/monero/`

## Step 1: Create Wallet

Run this command in your terminal:

```bash
cd ~/stealthpay-wallet
~/monero/monero-wallet-cli \
  --generate-new-wallet stealthpay \
  --daemon-address node.moneroworld.com:18089
```

### Interactive steps:

1. **Select language**: Type `1` (English) → Press Enter
2. **Set password**: Press Enter (empty = no password) or type password
3. **Confirm password**: Press Enter again

### ⚠️ IMPORTANT: Save Your Seed Phrase!

After creation, you'll see:
```
Generated new wallet: 44...
View key: ...
Seed: abcd efgh ijkl mnop qrst uvwx yzab cdef efgh ijkl mnop qrst uvwx yzab cdef
```

**WRITE DOWN THE SEED PHRASE AND STORE IT SECURELY!**

This is your only backup if you lose the wallet file.

## Step 2: Exit Wallet CLI

Type `exit` or press `Ctrl+C` to quit.

## Step 3: Start RPC Server

In a NEW terminal window/tab:

```bash
make start-rpc
```

Or manually:
```bash
cd ~/stealthpay-wallet
~/monero/monero-wallet-rpc \
  --wallet-file stealthpay \
  --password "" \
  --rpc-bind-port 18082 \
  --rpc-bind-ip 127.0.0.1 \
  --daemon-address node.moneroworld.com:18089 \
  --confirm-external-bind \
  --trusted-daemon
```

You should see:
```
2024-XX-XX ... Monero 'Fluorine Fermi' (v0.18.3.4-release)
2024-XX-XX ... Binding on 127.0.0.1:18082
2024-XX-XX ... Starting wallet RPC server
```

**Keep this terminal running!**

## Step 4: Test Connection

In another terminal:

```bash
cd stealthpay
python3 -c "from stealthpay import StealthPay; a = StealthPay.from_env(); print(f'✅ Connected! Address: {a.address}')"
```

## Step 5: Get XMR

### Option 1: Buy on Exchange (Recommended)

1. Register on Kraken, Binance, or KuCoin
2. Complete KYC verification
3. Buy XMR with card/bank transfer
4. Withdraw to your StealthPay address:

```bash
make balance
```

Copy the address shown and paste into exchange withdrawal form.

### Option 2: P2P (No KYC)

- [LocalMonero](https://localmonero.co) - Buy with cash, bank transfer, PayPal
- [HodlHodl](https://hodlhodl.com) - P2P exchange

### Option 3: Testnet (Free, for testing)

For testing without real money:

```bash
# Create testnet wallet
~/monero/monero-wallet-cli \
  --testnet \
  --generate-new-wallet stealthpay-testnet \
  --daemon-address node.moneroworld.com:28089

# Get free testnet XMR from faucet:
# https://community.xmr.to/faucet/testnet/
```

## Verification

Once you have XMR:

```bash
# Check balance
make balance

# Or with Python
python3 -c "
from stealthpay import StealthPay
agent = StealthPay.from_env()
info = agent.get_info()
print(f'Balance: {info.balance} XMR')
print(f'Address: {info.address}')
"
```

## Troubleshooting

### "Connection refused"
- Make sure `monero-wallet-rpc` is running
- Check port 18082 is not blocked

### "Wallet file not found"
- Check `~/stealthpay-wallet/` exists
- Verify `stealthpay.keys` file exists

### "Daemon not synced"
- First connection may take 5-10 minutes to sync
- Check status with `make balance`

## Security Tips

1. **Backup**: Save seed phrase in 2+ secure locations
2. **Password**: Use strong password for production
3. **Firewall**: RPC binds to localhost only (127.0.0.1)
4. **Updates**: Keep Monero binaries updated
5. **Privacy**: Don't reuse addresses - StealthPay handles this

## Next Steps

Once wallet is ready:

```bash
# Run demo with real XMR
cd stealthpay
python3 -c "
from stealthpay import StealthPay
agent = StealthPay.from_env()

# Create stealth address
stealth = agent.create_stealth_address('first-payment')
print(f'Receive XMR at: {stealth.address}')

# Check balance
print(f'Balance: {agent.balance} XMR')
"
```

Ready to use! 🚀
