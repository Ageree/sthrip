"""
StealthPay CLI - Command line interface for agents
Quick way to manage payments without writing code
"""

import os
import sys
import json
from decimal import Decimal
from datetime import datetime

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stealthpay import StealthPay
from stealthpay.privacy import PrivacyConfig, TransactionTiming, calculate_privacy_score
from cli.swap_commands import add_swap_subparser, handle_swap_command
from cli.bridge_commands import add_bridge_subparser, handle_bridge_command


def main():
    """Simple CLI without click dependency"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='🥷 StealthPay CLI - Anonymous payments for AI Agents'
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # Balance
    balance_parser = subparsers.add_parser('balance', help='Check wallet balance')
    
    # Send
    send_parser = subparsers.add_parser('send', help='Send XMR payment')
    send_parser.add_argument('to_address', help='Recipient address')
    send_parser.add_argument('amount', type=float, help='Amount in XMR')
    send_parser.add_argument('--memo', '-m', help='Private memo')
    
    # Address
    addr_parser = subparsers.add_parser('address', help='Manage addresses')
    addr_parser.add_argument('--create', '-c', action='store_true', help='Create new address')
    addr_parser.add_argument('--purpose', '-p', default='cli', help='Address purpose')
    
    # History
    hist_parser = subparsers.add_parser('history', help='Payment history')
    hist_parser.add_argument('--limit', '-n', type=int, default=10, help='Number of txs')
    
    # Churn
    churn_parser = subparsers.add_parser('churn', help='Churn funds for privacy')
    churn_parser.add_argument('amount', type=float, help='Amount to churn')
    churn_parser.add_argument('--rounds', '-r', type=int, default=3, help='Rounds')
    
    # Swap commands
    add_swap_subparser(subparsers)
    
    # Bridge commands
    add_bridge_subparser(subparsers)
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    # Connect to wallet
    try:
        agent = StealthPay.from_env()
    except Exception as e:
        print(f"❌ Error connecting to wallet: {e}")
        return
    
    # Execute command
    if args.command == 'balance':
        info = agent.get_info()
        print(f"\n💰 Wallet Balance")
        print(f"Address: {info.address}")
        print(f"Balance: {info.balance:.6f} XMR")
        print(f"Unlocked: {info.unlocked_balance:.6f} XMR")
    
    elif args.command == 'send':
        print(f"\n💸 Sending {args.amount} XMR to {args.to_address[:20]}...")
        try:
            payment = agent.pay(args.to_address, args.amount, memo=args.memo)
            print(f"✅ Sent! TX: {payment.tx_hash}")
            print(f"Fee: {payment.fee:.6f} XMR")
        except Exception as e:
            print(f"❌ Error: {e}")
    
    elif args.command == 'address':
        if args.create:
            stealth = agent.create_stealth_address(purpose=args.purpose)
            print(f"\n🎭 New Stealth Address")
            print(f"Address: {stealth.address}")
            print(f"Index: {stealth.index}")
            print(f"Purpose: {args.purpose}")
        else:
            info = agent.get_info()
            print(f"Primary address: {info.address}")
    
    elif args.command == 'history':
        payments = agent.get_payments(limit=args.limit)
        print(f"\n📜 Last {len(payments)} transactions:")
        for p in payments:
            direction = "📥" if p.from_address else "📤"
            print(f"{direction} {p.amount:.6f} XMR - {p.timestamp.strftime('%Y-%m-%d %H:%M')}")
    
    elif args.command == 'churn':
        print(f"\n🔄 Churning {args.amount} XMR ({args.rounds} rounds)...")
        try:
            payments = agent.churn(args.amount, rounds=args.rounds)
            print(f"✅ Churn complete! {len(payments)} transactions")
        except Exception as e:
            print(f"❌ Error: {e}")
    
    elif args.command == 'swap':
        handle_swap_command(args)
    
    elif args.command == 'bridge':
        handle_bridge_command(args)


if __name__ == '__main__':
    main()
