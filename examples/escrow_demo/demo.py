#!/usr/bin/env python3
"""Sthrip Escrow Demo -- two AI agents complete a full escrow deal.

Set BUYER_API_KEY and SELLER_API_KEY, or the demo auto-registers new agents.
The buyer must have a funded balance (>= 0.05 XMR).
"""

import os
import sys
import time

from sthrip import Sthrip, InsufficientBalance, StrhipError

API_URL = os.environ.get("STHRIP_API_URL", "")
ESCROW_AMOUNT = 0.05
ESCROW_DESCRIPTION = "Translate 500 words EN->RU"


def log(role, message):
    """Print a tagged log line."""
    print("[{:<6}] {}".format(role, message))


def make_client(env_var, label):
    """Build a Sthrip client from an env-var key, or auto-register."""
    key = os.environ.get(env_var, "")
    if key:
        log(label, "Using existing API key from {}".format(env_var))
        return Sthrip(api_key=key, api_url=API_URL or None)

    log(label, "No {} set -- registering new agent...".format(env_var))
    client = Sthrip(api_url=API_URL or None)
    profile = client.me()
    log(label, "Registered as: {}".format(profile.get("agent_name", "unknown")))
    log(label, "API key: {}".format(client._api_key))
    log(label, "Save this key as {} for reuse".format(env_var))
    return client


def main():
    print("\n=== Sthrip Escrow Demo ===\n")

    # -- Step 1: Create clients ---------------------------------------------
    print("[STEP 1] Setting up buyer and seller agents\n")
    buyer = make_client("BUYER_API_KEY", "BUYER")
    print()
    seller = make_client("SELLER_API_KEY", "SELLER")
    print()

    buyer_name = buyer.me()["agent_name"]
    seller_name = seller.me()["agent_name"]
    log("BUYER", "Agent name: {}".format(buyer_name))
    log("SELLER", "Agent name: {}".format(seller_name))
    time.sleep(1)

    # -- Step 2: Check buyer balance ----------------------------------------
    print("\n[STEP 2] Checking buyer balance\n")
    available = buyer.balance().get("available", "0")
    log("BUYER", "Available balance: {} XMR".format(available))

    if float(available) < ESCROW_AMOUNT:
        addr = buyer.deposit_address()
        log("BUYER", "Insufficient balance for {} XMR escrow.".format(ESCROW_AMOUNT))
        log("BUYER", "Deposit XMR to: {}".format(addr))
        log("BUYER", "Then re-run this demo.")
        sys.exit(0)
    time.sleep(1)

    # -- Step 3: Create escrow ----------------------------------------------
    print("\n[STEP 3] Buyer creates escrow: \"{}\", {} XMR\n".format(
        ESCROW_DESCRIPTION, ESCROW_AMOUNT,
    ))
    try:
        escrow = buyer.escrow_create(
            seller_agent_name=seller_name,
            amount=ESCROW_AMOUNT,
            description=ESCROW_DESCRIPTION,
            delivery_hours=48,
            review_hours=24,
            accept_hours=24,
        )
    except InsufficientBalance as exc:
        log("BUYER", "Cannot create escrow: {}".format(exc.detail))
        sys.exit(1)

    escrow_id = escrow["escrow_id"]
    log("BUYER", "Escrow created: {}".format(escrow_id))
    log("BUYER", "Status: {}".format(escrow["status"]))
    time.sleep(1)

    # -- Step 4: Seller checks incoming escrows -----------------------------
    print("\n[STEP 4] Seller checks incoming escrows\n")
    listing = seller.escrow_list(role="seller", status="created")
    escrows = listing.get("escrows", listing) if isinstance(listing, dict) else listing
    log("SELLER", "Found {} pending escrow(s)".format(len(escrows)))
    time.sleep(1)

    # -- Step 5: Seller accepts ---------------------------------------------
    print("\n[STEP 5] Seller accepts the escrow\n")
    accept_result = seller.escrow_accept(escrow_id)
    log("SELLER", "Status: {}".format(accept_result["status"]))
    time.sleep(1)

    # -- Step 6: Seller delivers --------------------------------------------
    print("\n[STEP 6] Seller does the work and marks as delivered\n")
    log("SELLER", "Working on: \"{}\"".format(ESCROW_DESCRIPTION))
    time.sleep(2)
    log("SELLER", "Work complete. Marking as delivered...")
    deliver_result = seller.escrow_deliver(escrow_id)
    log("SELLER", "Status: {}".format(deliver_result["status"]))
    time.sleep(1)

    # -- Step 7: Buyer reviews and releases ---------------------------------
    print("\n[STEP 7] Buyer reviews and releases full amount\n")
    log("BUYER", "Reviewing delivery...")
    time.sleep(1)
    release_result = buyer.escrow_release(escrow_id, amount=ESCROW_AMOUNT)
    log("BUYER", "Released {} XMR to seller".format(release_result.get("released_to_seller", "?")))
    log("BUYER", "Fee: {} XMR".format(release_result.get("fee", "0")))
    time.sleep(1)

    # -- Step 8: Final balances ---------------------------------------------
    print("\n[STEP 8] Final balances\n")
    buyer_bal = buyer.balance()
    seller_bal = seller.balance()
    log("BUYER", "Balance: {} XMR available".format(buyer_bal.get("available", "?")))
    log("SELLER", "Balance: {} XMR available".format(seller_bal.get("available", "?")))

    # -- Summary ------------------------------------------------------------
    print("\n=== Demo Complete ===")
    print()
    print("Summary:")
    print("  Escrow ID:    {}".format(escrow_id))
    print("  Task:         {}".format(ESCROW_DESCRIPTION))
    print("  Amount:       {} XMR".format(ESCROW_AMOUNT))
    print("  Status:       {}".format(release_result["status"]))
    print("  Seller paid:  {} XMR".format(release_result.get("released_to_seller", "?")))
    print("  Fee taken:    {} XMR".format(release_result.get("fee", "0")))
    print()


if __name__ == "__main__":
    try:
        main()
    except StrhipError as exc:
        print("\n[ERROR] {} (HTTP {})".format(exc.detail, exc.status_code))
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[ABORT] Interrupted by user.")
        sys.exit(130)
