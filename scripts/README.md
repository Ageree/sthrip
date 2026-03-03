# StealthPay Scripts

## Key Ceremony

The key ceremony script performs secure distributed key generation for the MPC network.

### Prerequisites

1. **TSS Server Running**
   ```bash
   cd ../tss-service
   make run
   ```

2. **HSM Setup** (choose one)
   
   **Option A: Hashicorp Vault**
   ```bash
   export VAULT_URL="https://vault.example.com"
   export VAULT_TOKEN="hvs.xxx"
   ```
   
   **Option B: AWS KMS**
   ```bash
   export AWS_REGION="us-east-1"
   aws configure  # or use IAM role
   ```

### Running the Ceremony

Each party runs the script independently:

```bash
# Party 1
python key_ceremony.py --party-id 1 --threshold 3 --total 5

# Party 2 (on different machine)
python key_ceremony.py --party-id 2 --threshold 3 --total 5

# And so on...
```

### Development Mode (No HSM)

```bash
python key_ceremony.py --party-id 1 --threshold 3 --total 5 --no-hsm
```

### Security Checklist

- [ ] All parties in separate secure locations
- [ ] No key material transmitted over network
- [ ] HSM properly configured
- [ ] Backups created and secured
- [ ] Group public key verified across all parties

## Contract Deployment

See `../contracts/scripts/deploy.js`
