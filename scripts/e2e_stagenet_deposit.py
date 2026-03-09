#!/usr/bin/env python3
"""
E2E Stagenet Deposit Test

Tests the full deposit flow on Railway:
1. Register agent via API
2. Get deposit subaddress
3. Send XMR from hub wallet to that subaddress (self-transfer)
4. Wait for DepositMonitor to credit balance
5. Verify balance updated

Usage:
    export STHRIP_API_URL=https://sthrip-api-production.up.railway.app
    export STHRIP_API_KEY=<admin-api-key>
    python scripts/e2e_stagenet_deposit.py
"""

import json
import os
import sys
import time
import requests

API_URL = os.getenv("STHRIP_API_URL", "https://sthrip-api-production.up.railway.app")
API_KEY = os.getenv("STHRIP_API_KEY")

DEPOSIT_AMOUNT_XMR = 0.001  # Small test amount
POLL_INTERVAL = 30  # seconds
MAX_WAIT = 1200  # 20 minutes max wait for confirmations


def api(method, path, **kwargs):
    url = f"{API_URL}{path}"
    headers = kwargs.pop("headers", {})
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    resp = getattr(requests, method)(url, headers=headers, **kwargs)
    print(f"  {method.upper()} {path} -> {resp.status_code}")
    if resp.status_code >= 400:
        print(f"  Error: {resp.text}")
    return resp


def main():
    if not API_KEY:
        print("ERROR: Set STHRIP_API_KEY env var")
        sys.exit(1)

    print(f"=== E2E Stagenet Deposit Test ===")
    print(f"API: {API_URL}")
    print()

    # Step 1: Check API health
    print("[1/6] Checking API health...")
    resp = api("get", "/ready")
    if resp.status_code != 200:
        print("FAIL: API not ready")
        sys.exit(1)
    print(f"  OK: {resp.json()}")
    print()

    # Step 2: Register test agent
    print("[2/6] Registering test agent...")
    agent_name = f"e2e-deposit-test-{int(time.time())}"
    resp = api("post", "/v2/agents/register", json={
        "name": agent_name,
        "description": "E2E deposit test agent",
        "wallet_address": "5" + "a" * 94,  # valid-format stagenet address
    })
    if resp.status_code != 201:
        print("FAIL: Could not register agent")
        sys.exit(1)
    agent_data = resp.json()
    agent_api_key = agent_data["api_key"]
    agent_id = agent_data["id"]
    print(f"  Agent: {agent_name} (id={agent_id})")
    print()

    # Step 3: Get deposit address
    print("[3/6] Getting deposit address...")
    resp = api("post", "/v2/balance/deposit", headers={"X-API-Key": agent_api_key})
    if resp.status_code != 200:
        print("FAIL: Could not get deposit address")
        sys.exit(1)
    deposit_data = resp.json()
    deposit_address = deposit_data["deposit_address"]
    print(f"  Deposit address: {deposit_address}")
    print(f"  Network: {deposit_data.get('network')}")
    print(f"  Min confirmations: {deposit_data.get('min_confirmations')}")
    print()

    # Step 4: Check initial balance
    print("[4/6] Checking initial balance...")
    resp = api("get", "/v2/balance", headers={"X-API-Key": agent_api_key})
    if resp.status_code != 200:
        print("FAIL: Could not get balance")
        sys.exit(1)
    initial_balance = resp.json()
    print(f"  Balance: {initial_balance}")
    print()

    # Step 5: Send XMR to deposit address
    print(f"[5/6] Sending {DEPOSIT_AMOUNT_XMR} XMR to deposit address...")
    print(f"  Address: {deposit_address}")
    print()
    print("  >>> MANUAL STEP <<<")
    print(f"  Send {DEPOSIT_AMOUNT_XMR} XMR to: {deposit_address}")
    print("  (Use a stagenet faucet or another wallet)")
    print()
    input("  Press Enter after sending XMR...")
    print()

    # Step 6: Poll for balance update
    print(f"[6/6] Waiting for deposit confirmation (polling every {POLL_INTERVAL}s, max {MAX_WAIT}s)...")
    start = time.time()
    while time.time() - start < MAX_WAIT:
        resp = api("get", "/v2/balance", headers={"X-API-Key": agent_api_key})
        if resp.status_code == 200:
            balance = resp.json()
            available = float(balance.get("available", 0))
            pending = float(balance.get("pending", 0))
            elapsed = int(time.time() - start)
            print(f"  [{elapsed}s] available={available}, pending={pending}")
            if available > float(initial_balance.get("available", 0)):
                print()
                print("=== SUCCESS! Deposit confirmed and balance credited ===")
                print(f"  Final balance: {balance}")
                return
        time.sleep(POLL_INTERVAL)

    print()
    print("TIMEOUT: Balance not updated within max wait time.")
    print("Check DepositMonitor logs for issues.")
    sys.exit(1)


if __name__ == "__main__":
    main()
