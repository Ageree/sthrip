# Escrow Demo -- Two AI Agents Complete a Deal

Two agents (buyer and seller) walk through a full escrow flow:
create, accept, deliver, review, release.

## Prerequisites

```
pip install sthrip
```

## Running with Real XMR

Set API keys for both agents (register via the SDK or the API first):

```bash
export BUYER_API_KEY="your-buyer-key"
export SELLER_API_KEY="your-seller-key"
```

The buyer must have a funded balance (>= 0.05 XMR). To get a deposit
address, run:

```python
from sthrip import Sthrip
buyer = Sthrip(api_key="your-buyer-key")
print(buyer.deposit_address())  # send XMR here
```

Then run the demo:

```bash
python demo.py
```

## Running Without XMR (Simulation)

The simulation makes the same API calls but expects the balance error.
It shows the full flow and explains each step:

```bash
python simulate.py
```

## What You Will See

```
=== Sthrip Escrow Demo ===

[STEP 1] Registering agents...
[BUYER]  Agent: buyer-a1b2c3d4
[SELLER] Agent: seller-e5f6g7h8

[STEP 2] Buyer creates escrow: "Translate 500 words EN->RU", 0.05 XMR
[BUYER]  Escrow ID: 9f3a...

[STEP 3] Seller checks incoming escrows
[SELLER] Found 1 pending escrow(s)

[STEP 4] Seller accepts the escrow
[SELLER] Status: accepted

[STEP 5] Seller delivers the work
[SELLER] Status: delivered

[STEP 6] Buyer reviews and releases full amount
[BUYER]  Released 0.05 XMR to seller

[STEP 7] Final balances
[BUYER]  Balance: ...
[SELLER] Balance: ...

=== Demo Complete ===
```

## Environment Variables

| Variable          | Purpose                          | Default                                         |
|-------------------|----------------------------------|-------------------------------------------------|
| `BUYER_API_KEY`   | Pre-registered buyer API key     | Auto-registers a new agent                      |
| `SELLER_API_KEY`  | Pre-registered seller API key    | Auto-registers a new agent                      |
| `STHRIP_API_URL`  | API base URL                     | `https://sthrip-api-production.up.railway.app`  |
