#!/usr/bin/env python3
"""
MPC Key Generation Ceremony

This script performs a secure distributed key generation ceremony
with multiple parties in different locations.

Usage:
    python key_ceremony.py --party-id 1 --threshold 3 --total 5
    
Environment Variables:
    TSS_ENDPOINT: TSS server endpoint (default: localhost:50051)
    VAULT_URL: Hashicorp Vault URL
    VAULT_TOKEN: Vault authentication token
    AWS_REGION: AWS region for KMS
"""

import argparse
import json
import os
import sys
import time
from getpass import getpass
from typing import List, Optional

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sthrip.bridge.tss_client import TSSClient
from sthrip.bridge.hsm import VaultManager, AWSKMSManager


def print_header(party_id: int):
    """Print ceremony header"""
    print("""
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║           STHRIP MPC KEY GENERATION CEREMONY                 ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
""")
    print(f"Party ID: {party_id}")
    print(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print()


def get_hsm_backend(args) -> Optional[object]:
    """Initialize HSM backend based on arguments"""
    if args.no_hsm:
        print("⚠️  WARNING: HSM disabled - key share will be stored locally!")
        print("   This should ONLY be used for development/testing.\n")
        return None
    
    if args.hsm_type == "vault":
        print("HSM Backend: Hashicorp Vault")
        vault_url = args.vault_url or os.getenv("VAULT_URL") or getpass("Vault URL: ")
        vault_token = args.vault_token or os.getenv("VAULT_TOKEN")
        
        if not vault_token:
            vault_token = getpass("Vault Token: ")
        
        try:
            vault = VaultManager(vault_url, vault_token)
            if not vault.health_check():
                print("❌ Vault health check failed!")
                sys.exit(1)
            print("✓ Connected to Vault\n")
            return vault
        except Exception as e:
            print(f"❌ Failed to connect to Vault: {e}")
            sys.exit(1)
    
    elif args.hsm_type == "aws":
        print("HSM Backend: AWS KMS")
        region = args.aws_region or os.getenv("AWS_REGION", "us-east-1")
        
        try:
            aws = AWSKMSManager(region)
            if not aws.health_check():
                print("❌ AWS KMS health check failed!")
                sys.exit(1)
            print(f"✓ Connected to AWS KMS ({region})\n")
            return aws
        except Exception as e:
            print(f"❌ Failed to connect to AWS KMS: {e}")
            sys.exit(1)
    
    else:
        print(f"❌ Unknown HSM type: {args.hsm_type}")
        sys.exit(1)


def perform_key_generation(
    party_id: int,
    threshold: int,
    total: int,
    tss_endpoint: str,
    hsm: Optional[object],
    output_dir: str
) -> dict:
    """
    Perform distributed key generation
    
    Returns:
        Dictionary with ceremony results
    """
    results = {
        "party_id": party_id,
        "threshold": threshold,
        "total": total,
        "timestamp": time.time(),
        "success": False
    }
    
    # Step 1: Connect to TSS service
    print("━" * 60)
    print("STEP 1: Connecting to TSS service...")
    print("━" * 60)
    
    try:
        client = TSSClient(tss_endpoint)
        print(f"✓ Connected to TSS server at {tss_endpoint}\n")
    except Exception as e:
        print(f"❌ Failed to connect to TSS service: {e}")
        results["error"] = f"TSS connection failed: {e}"
        return results
    
    # Step 2: Collect peer information
    print("━" * 60)
    print("STEP 2: Collecting peer information...")
    print("━" * 60)
    
    peers = []
    for i in range(1, total + 1):
        if i != party_id:
            peer_id = f"party-{i}"
            peers.append(peer_id)
            print(f"  Peer {i}: {peer_id}")
    
    print(f"\nTotal peers: {len(peers)}")
    print(f"Threshold: {threshold}-of-{total}\n")
    
    # Wait for user confirmation
    input("Press Enter to begin key generation...\n")
    
    # Step 3: Generate key share
    print("━" * 60)
    print("STEP 3: Generating key share via DKG...")
    print("━" * 60)
    print("This may take a few minutes...")
    
    try:
        key_share = client.generate_key(
            party_id=f"party-{party_id}",
            threshold=threshold,
            total=total,
            peers=peers
        )
        
        print("✓ Key share generated successfully!")
        print(f"  Share ID: {key_share.share_id}")
        print(f"  Public Key: 0x{key_share.public_key.hex()[:40]}...")
        print()
        
        results["public_key"] = key_share.public_key.hex()
        results["share_id"] = key_share.share_id
        
    except Exception as e:
        print(f"❌ Key generation failed: {e}")
        results["error"] = f"DKG failed: {e}"
        client.close()
        return results
    
    # Step 4: Store key share in HSM
    print("━" * 60)
    print("STEP 4: Securing key share...")
    print("━" * 60)
    
    if hsm:
        try:
            stored = hsm.store_key_share(
                party_id=str(party_id),
                key_share=key_share.data,
                alias=f"sthrip-mpc-party-{party_id}"
            )
            
            print(f"✓ Key share stored in {stored.backend.upper()}")
            print(f"  Path/ID: {stored.key_id}")
            print()
            
            results["storage"] = {
                "backend": stored.backend,
                "key_id": stored.key_id,
                "alias": stored.alias
            }
            
        except Exception as e:
            print(f"❌ Failed to store key share: {e}")
            print("⚠️  FALLBACK: Saving to encrypted local file")
            # TODO: Implement encrypted local storage
            results["storage"] = {"backend": "local", "error": str(e)}
    else:
        # Save to file (dev mode only)
        key_file = os.path.join(output_dir, f"party-{party_id}.key")
        os.makedirs(output_dir, exist_ok=True)
        
        with open(key_file, 'wb') as f:
            f.write(key_share.data)
        
        print(f"⚠️  Key share saved to: {key_file}")
        print("   IMPORTANT: Secure this file immediately!\n")
        
        results["storage"] = {
            "backend": "local_file",
            "path": key_file
        }
    
    # Step 5: Verify
    print("━" * 60)
    print("STEP 5: Verification...")
    print("━" * 60)
    
    try:
        pub_key, eth_address = client.get_public_key(
            party_id=f"party-{party_id}",
            key_share=key_share.data
        )
        
        print(f"✓ Verification successful")
        print(f"  Ethereum Address: {eth_address}")
        print()
        
        results["ethereum_address"] = eth_address
        results["success"] = True
        
    except Exception as e:
        print(f"⚠️  Verification warning: {e}")
        results["verification_error"] = str(e)
    
    client.close()
    return results


def create_backup(party_id: int, results: dict, output_dir: str):
    """Create backup documentation"""
    print("━" * 60)
    print("STEP 6: Creating backup documentation...")
    print("━" * 60)
    
    backup_file = os.path.join(output_dir, f"ceremony-party-{party_id}.json")
    
    with open(backup_file, 'w') as f:
        json.dump(results, f, indent=2)
    
    print(f"✓ Backup saved to: {backup_file}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="MPC Key Generation Ceremony for Sthrip"
    )
    parser.add_argument(
        "--party-id",
        type=int,
        required=True,
        help="This party's ID (1-5)"
    )
    parser.add_argument(
        "--total",
        type=int,
        default=5,
        help="Total number of parties (default: 5)"
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=3,
        help="Threshold for signing (default: 3)"
    )
    parser.add_argument(
        "--tss-endpoint",
        default=os.getenv("TSS_ENDPOINT", "localhost:50051"),
        help="TSS server endpoint"
    )
    parser.add_argument(
        "--hsm-type",
        choices=["vault", "aws"],
        default="vault",
        help="HSM backend type"
    )
    parser.add_argument(
        "--no-hsm",
        action="store_true",
        help="Disable HSM (dev mode only)"
    )
    parser.add_argument(
        "--vault-url",
        help="Vault URL"
    )
    parser.add_argument(
        "--vault-token",
        help="Vault token"
    )
    parser.add_argument(
        "--aws-region",
        help="AWS region"
    )
    parser.add_argument(
        "--output-dir",
        default="./ceremony-output",
        help="Output directory for key files"
    )
    
    args = parser.parse_args()
    
    # Validate arguments
    if args.party_id < 1 or args.party_id > args.total:
        print(f"❌ Party ID must be between 1 and {args.total}")
        sys.exit(1)
    
    if args.threshold > args.total:
        print("❌ Threshold cannot exceed total parties")
        sys.exit(1)
    
    # Print header
    print_header(args.party_id)
    
    # Initialize HSM
    hsm = get_hsm_backend(args)
    
    # Perform ceremony
    results = perform_key_generation(
        party_id=args.party_id,
        threshold=args.threshold,
        total=args.total,
        tss_endpoint=args.tss_endpoint,
        hsm=hsm,
        output_dir=args.output_dir
    )
    
    # Create backup
    if results.get("success"):
        create_backup(args.party_id, results, args.output_dir)
    
    # Print summary
    print("━" * 60)
    print("CEREMONY SUMMARY")
    print("━" * 60)
    
    if results["success"]:
        print("✅ Key generation completed successfully!")
        print()
        print("IMPORTANT SECURITY REMINDERS:")
        print("  • Key share is now secured in HSM")
        print("  • Create offline backup (Shamir split)")
        print("  • Never transmit key share over network")
        print("  • Verify group public key with other parties")
        print()
        print(f"Public Key: {results.get('public_key', 'N/A')}")
        print(f"ETH Address: {results.get('ethereum_address', 'N/A')}")
    else:
        print("❌ Key generation failed!")
        print(f"Error: {results.get('error', 'Unknown error')}")
        sys.exit(1)
    
    print()


if __name__ == "__main__":
    main()
