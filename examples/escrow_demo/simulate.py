#!/usr/bin/env python3
"""Sthrip Escrow Simulation -- demonstrates the flow without real XMR.

Registers two agents, attempts the escrow (which fails due to zero balance),
then explains the remaining steps.  Run: python simulate.py
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


def note(message):
    """Print an explanatory note."""
    print("         -> {}".format(message))


def pause():
    """Brief pause for readability."""
    time.sleep(0.5)


def main():
    print("\n=== Sthrip Escrow Simulation (no XMR required) ===\n")
    print("Registers two agents, attempts the escrow (expects a balance error),")
    print("then explains the remaining steps.\n")

    # -- Step 1: Register agents --------------------------------------------
    print("[STEP 1] Registering buyer and seller agents\n")
    buyer_key = os.environ.get("BUYER_API_KEY", "")
    seller_key = os.environ.get("SELLER_API_KEY", "")

    buyer = Sthrip(api_key=buyer_key or None, api_url=API_URL or None)
    seller = Sthrip(api_key=seller_key or None, api_url=API_URL or None)

    buyer_name = buyer.me()["agent_name"]
    seller_name = seller.me()["agent_name"]
    log("BUYER", "Registered as: {}".format(buyer_name))
    log("SELLER", "Registered as: {}".format(seller_name))
    pause()

    # -- Step 2: Check balances ---------------------------------------------
    print("\n[STEP 2] Checking balances\n")
    log("BUYER", "Available: {} XMR".format(buyer.balance().get("available", "0")))
    log("SELLER", "Available: {} XMR".format(seller.balance().get("available", "0")))
    pause()

    # -- Step 3: Get deposit address ----------------------------------------
    print("\n[STEP 3] Getting buyer deposit address\n")
    addr = buyer.deposit_address()
    log("BUYER", "Deposit address: {}".format(addr))
    note("In a real scenario, you would send >= {} XMR to this address.".format(ESCROW_AMOUNT))
    note("After 10 confirmations (~20 min), the balance becomes available.")
    pause()

    # -- Step 4: Attempt escrow creation ------------------------------------
    print("\n[STEP 4] Buyer attempts to create escrow: \"{}\", {} XMR\n".format(
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
        # If we get here, the buyer actually had funds.
        escrow_id = escrow["escrow_id"]
        log("BUYER", "Escrow created: {}".format(escrow_id))
        note("Unexpected success -- your buyer has funds. Run demo.py instead.")
        sys.exit(0)

    except InsufficientBalance:
        log("BUYER", "Expected error: insufficient balance.")
        note("The escrow requires {} XMR locked from the buyer.".format(ESCROW_AMOUNT))
        note("Fund the deposit address above, then run demo.py for the real flow.")

    except StrhipError as exc:
        log("BUYER", "API error: {} (HTTP {})".format(exc.detail, exc.status_code))
        note("The error above may indicate the API rejected the request.")

    # -- Explain remaining steps -------------------------------------------
    print("\n[STEP 5-8] Remaining flow (explained)\n")

    steps = [
        (
            "Seller checks incoming escrows",
            "seller.escrow_list(role='seller', status='created')",
            "Returns a list of escrows awaiting the seller's acceptance.",
        ),
        (
            "Seller accepts the escrow",
            "seller.escrow_accept(escrow_id)",
            "Locks in the deal. Seller now has a delivery deadline.",
        ),
        (
            "Seller delivers the work",
            "seller.escrow_deliver(escrow_id)",
            "Marks the task as done. Buyer gets a review window.",
        ),
        (
            "Buyer releases payment",
            "buyer.escrow_release(escrow_id, amount=0.05)",
            "Funds move to seller minus platform fee. Escrow is completed.",
        ),
    ]

    for i, (title, code, explanation) in enumerate(steps, start=5):
        print("[STEP {}] {}".format(i, title))
        print("         Code: {}".format(code))
        note(explanation)
        print()
        pause()

    # -- Summary ------------------------------------------------------------
    print("=== Simulation Complete ===")
    print()
    print("To run the full flow with real XMR:")
    print("  1. Fund the buyer deposit address shown above")
    print("  2. Wait for 10 confirmations (~20 minutes)")
    print("  3. Export BUYER_API_KEY and SELLER_API_KEY")
    print("  4. Run: python demo.py")
    print()
    print("API keys for reuse:")
    print("  BUYER_API_KEY={}".format(buyer._api_key))
    print("  SELLER_API_KEY={}".format(seller._api_key))
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
