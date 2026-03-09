#!/usr/bin/env python3
"""
Generate MPC key shares for bridge nodes.

Usage:
    python scripts/generate_mpc_keys.py --nodes 5 --threshold 3 --output ./data/mpc_keys/
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sthrip.bridge.tss.dkg import DistributedKeyGenerator, SecureKeyStorage


def main():
    parser = argparse.ArgumentParser(description="Generate MPC key shares")
    parser.add_argument("--nodes", type=int, default=5, help="Number of nodes")
    parser.add_argument("--threshold", type=int, default=3, help="Threshold for signing")
    parser.add_argument("--output", type=str, required=True, help="Output directory")
    parser.add_argument("--password", type=str, default=None, help="Encryption password")
    
    args = parser.parse_args()
    
    print(f"Generating {args.nodes} key shares with threshold {args.threshold}...")
    
    # Create DKG
    dkg = DistributedKeyGenerator(n=args.nodes, threshold=args.threshold)
    
    # Generate shares
    shares = dkg.generate_key_shares()
    
    # Create secure storage
    storage = SecureKeyStorage(
        encryption_key=args.password.encode() if args.password else None
    )
    
    # Create output directory
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Save shares
    for share in shares:
        # Store in secure storage
        storage_id = storage.store_share(share)
        
        # Save to file
        share_file = output_dir / f"node_{share.party_id}_share.enc"
        with open(share_file, 'wb') as f:
            f.write(storage._storage[share.party_id])
        
        # Save public info
        public_file = output_dir / f"node_{share.party_id}_public.json"
        with open(public_file, 'w') as f:
            json.dump(share.to_dict(), f, indent=2)
        
        print(f"  Node {share.party_id}: {share_file}")
    
    # Save group public key
    group_public = shares[0].public_key.hex()
    with open(output_dir / "group_public_key.txt", 'w') as f:
        f.write(group_public)
    
    # Save manifest
    manifest = {
        "nodes": args.nodes,
        "threshold": args.threshold,
        "group_public_key": group_public,
        "node_ids": [f"mpc_node_{i}" for i in range(1, args.nodes + 1)]
    }
    
    with open(output_dir / "manifest.json", 'w') as f:
        json.dump(manifest, f, indent=2)
    
    print(f"\nKey shares saved to: {output_dir}")
    print(f"Group public key: {group_public[:40]}...")
    print("\nIMPORTANT: Keep key shares secure and distribute to individual nodes!")


if __name__ == "__main__":
    main()
