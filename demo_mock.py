"""
DEMO MODE - Sthrip without real Monero
Use this to test and demonstrate functionality
"""

import time
import random
import hashlib
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List

# Mock classes for demo

@dataclass
class MockPayment:
    tx_hash: str
    amount: float
    to_address: str
    status: str
    fee: float
    timestamp: datetime
    confirmations: int = 0
    
    @property
    def is_confirmed(self):
        return self.confirmations >= 10

@dataclass
class MockWalletInfo:
    address: str
    balance: float
    unlocked_balance: float
    height: int

class MockSthrip:
    """
    Mock version of Sthrip for demonstration.
    Simulates transactions without real blockchain.
    """
    
    def __init__(self, agent_name: str = "demo-agent"):
        self.agent_name = agent_name
        self.address = f"44DEMO{hashlib.md5(agent_name.encode()).hexdigest()[:90]}"
        self.balance = 10.0  # Start with 10 XMR demo
        self.transactions: List[MockPayment] = []
        self.stealth_addresses = []
        self.address_counter = 0
        
        print(f"🤖 Mock Agent initialized: {agent_name}")
        print(f"   Address: {self.address[:50]}...")
        print(f"   Balance: {self.balance} XMR (DEMO)")
    
    def get_info(self) -> MockWalletInfo:
        """Get wallet info"""
        return MockWalletInfo(
            address=self.address,
            balance=self.balance,
            unlocked_balance=self.balance,
            height=1234567
        )
    
    @property
    def balance_xmr(self) -> float:
        return self.balance
    
    def create_stealth_address(self, purpose: str = "demo") -> dict:
        """Create mock stealth address"""
        self.address_counter += 1
        stealth = f"8BDEMO{hashlib.md5(f'{self.address}{self.address_counter}'.encode()).hexdigest()[:91]}"
        
        addr_info = {
            "address": stealth,
            "index": self.address_counter,
            "purpose": purpose,
            "created_at": datetime.now()
        }
        self.stealth_addresses.append(addr_info)
        
        print(f"🎭 Stealth address created:")
        print(f"   Address: {stealth}")
        print(f"   Purpose: {purpose}")
        
        return addr_info
    
    def pay(self, to_address: str, amount: float, memo: str = None, **kwargs) -> MockPayment:
        """Simulate payment"""
        
        if amount > self.balance:
            raise ValueError(f"Insufficient funds: {self.balance} XMR available")
        
        # Generate fake tx hash
        tx_hash = hashlib.sha256(
            f"{self.address}{to_address}{amount}{time.time()}".encode()
        ).hexdigest()
        
        # Create payment
        payment = MockPayment(
            tx_hash=tx_hash,
            amount=amount,
            to_address=to_address,
            status="pending",
            fee=random.uniform(0.0001, 0.001),
            timestamp=datetime.now()
        )
        
        self.transactions.append(payment)
        self.balance -= (amount + payment.fee)
        
        print(f"💸 Payment sent (DEMO):")
        print(f"   To: {to_address[:40]}...")
        print(f"   Amount: {amount} XMR")
        print(f"   Fee: {payment.fee:.6f} XMR")
        print(f"   TX: {tx_hash[:50]}...")
        print(f"   Remaining: {self.balance:.4f} XMR")
        
        # Simulate confirmation after delay
        import threading
        def confirm():
            time.sleep(2)
            payment.confirmations = 10
            payment.status = "confirmed"
        
        threading.Thread(target=confirm, daemon=True).start()
        
        return payment
    
    def get_payments(self, incoming=True, outgoing=True, limit=10) -> List[MockPayment]:
        """Get transaction history"""
        return self.transactions[-limit:]
    
    def churn(self, amount: float, rounds: int = 3, delay_hours: float = 1) -> List[MockPayment]:
        """Simulate churn"""
        print(f"\n🔄 Churning {amount} XMR ({rounds} rounds)...")
        
        payments = []
        current_amount = amount
        
        for i in range(rounds):
            # Create intermediate address
            intermediate = self.create_stealth_address(f"churn-round-{i+1}")
            
            # Small variance
            variance = random.uniform(-0.001, 0.001)
            current_amount = max(0.0001, current_amount + variance)
            
            # Send
            payment = self.pay(
                to_address=intermediate["address"],
                amount=current_amount,
                memo=f"Churn round {i+1}"
            )
            payments.append(payment)
            
            print(f"   Round {i+1} complete: {current_amount:.6f} XMR")
            
            # In real: wait hours. In demo: just continue
            if i < rounds - 1:
                print(f"   (In real: waiting {delay_hours} hours)")
        
        print(f"✅ Churn complete! Chain broken.")
        return payments
    
    def create_escrow(self, seller_address: str, arbiter_address: str, 
                      amount: float, description: str, **kwargs) -> dict:
        """Create mock escrow"""
        escrow_id = hashlib.sha256(
            f"{self.address}{seller_address}{amount}{time.time()}".encode()
        ).hexdigest()[:16]
        
        print(f"\n🛡️  Escrow created (DEMO):")
        print(f"   ID: {escrow_id}")
        print(f"   Amount: {amount} XMR")
        print(f"   Seller: {seller_address[:30]}...")
        print(f"   Arbiter: {arbiter_address[:30]}...")
        print(f"   Desc: {description}")
        
        return {
            "id": escrow_id,
            "status": "pending",
            "amount": amount,
            "description": description
        }


def run_full_demo():
    """Run complete demo scenario"""
    
    print("=" * 70)
    print("🥷 STHRIP DEMO - Full Agent Workflow")
    print("=" * 70)
    print("\n⚠️  DEMO MODE: No real transactions, no real XMR")
    print("   This simulates how Sthrip works\n")
    
    # Create two agents
    print("\n" + "─" * 70)
    print("STEP 1: Creating agents")
    print("─" * 70)
    
    seller = MockSthrip("Data-Seller-Agent")
    buyer = MockSthrip("Data-Buyer-Agent")
    
    print(f"\n✅ Created 2 agents")
    
    # Step 2: Seller creates service
    print("\n" + "─" * 70)
    print("STEP 2: Seller sets up service")
    print("─" * 70)
    
    service_price = 0.5
    stealth = seller.create_stealth_address("weather-api-payment")
    
    print(f"\nService: Weather API")
    print(f"Price: {service_price} XMR")
    print(f"Payment address: {stealth['address'][:50]}...")
    
    # Step 3: Buyer sends payment
    print("\n" + "─" * 70)
    print("STEP 3: Buyer sends payment")
    print("─" * 70)
    
    print(f"\nBuyer balance before: {buyer.balance_xmr} XMR")
    
    payment = buyer.pay(
        to_address=stealth["address"],
        amount=service_price,
        memo="Payment for weather API"
    )
    
    print(f"\nBuyer balance after: {buyer.balance_xmr:.4f} XMR")
    
    # Step 4: Privacy - Churn
    print("\n" + "─" * 70)
    print("STEP 4: Privacy enhancement (Churn)")
    print("─" * 70)
    
    print("\nBuyer wants to hide payment source...")
    buyer.churn(amount=2.0, rounds=3, delay_hours=24)
    
    # Step 5: Escrow example
    print("\n" + "─" * 70)
    print("STEP 5: Create escrow deal")
    print("─" * 70)
    
    arbiter = MockSthrip("Trusted-Arbiter-Agent")
    
    escrow = buyer.create_escrow(
        seller_address=seller.address,
        arbiter_address=arbiter.address,
        amount=1.0,
        description="AI model purchase"
    )
    
    # Summary
    print("\n" + "=" * 70)
    print("📊 DEMO SUMMARY")
    print("=" * 70)
    
    print(f"\nSeller Agent:")
    print(f"   Address: {seller.address[:50]}...")
    print(f"   Balance: {seller.balance_xmr:.4f} XMR")
    print(f"   Transactions: {len(seller.transactions)}")
    
    print(f"\nBuyer Agent:")
    print(f"   Address: {buyer.address[:50]}...")
    print(f"   Balance: {buyer.balance_xmr:.4f} XMR")
    print(f"   Transactions: {len(buyer.transactions)}")
    
    print(f"\nArbiter Agent:")
    print(f"   Address: {arbiter.address[:50]}...")
    print(f"   Created escrows: 1")
    
    print("\n" + "=" * 70)
    print("✅ DEMO COMPLETE!")
    print("=" * 70)
    print("\nTo use with REAL Monero:")
    print("  1. Install monero-wallet-rpc")
    print("  2. Create wallet: monero-wallet-cli --generate-new-wallet")
    print("  3. Start RPC: monero-wallet-rpc --wallet-file ...")
    print("  4. Use real Sthrip instead of MockSthrip")
    print("\nDocs: AGENT_INTEGRATION.md")


def interactive_demo():
    """Interactive demo mode"""
    
    print("\n🎮 INTERACTIVE DEMO MODE")
    print("=" * 60)
    
    agent = MockSthrip("My-Agent")
    
    while True:
        print(f"\n💰 Balance: {agent.balance_xmr:.4f} XMR")
        print("\nCommands:")
        print("  1. Create stealth address")
        print("  2. Send payment")
        print("  3. View history")
        print("  4. Churn funds")
        print("  5. Create escrow")
        print("  0. Exit")
        
        try:
            choice = input("\nChoice: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        
        if choice == "1":
            purpose = input("Purpose (optional): ").strip() or "demo"
            agent.create_stealth_address(purpose)
            
        elif choice == "2":
            to = input("To address: ").strip()
            try:
                amt = float(input("Amount (XMR): ").strip())
                memo = input("Memo (optional): ").strip() or None
                agent.pay(to, amt, memo)
            except ValueError as e:
                print(f"Error: {e}")
                
        elif choice == "3":
            txs = agent.get_payments()
            print(f"\n📜 Transactions ({len(txs)}):")
            for tx in txs:
                print(f"  {tx.timestamp.strftime('%H:%M')} - {tx.amount:.4f} XMR")
                
        elif choice == "4":
            try:
                amt = float(input("Amount to churn: ").strip())
                rounds = int(input("Rounds (default 3): ").strip() or "3")
                agent.churn(amt, rounds)
            except ValueError as e:
                print(f"Error: {e}")
                
        elif choice == "5":
            seller = input("Seller address: ").strip()
            arbiter = input("Arbiter address: ").strip()
            try:
                amt = float(input("Amount: ").strip())
                desc = input("Description: ").strip()
                agent.create_escrow(seller, arbiter, amt, desc)
            except ValueError as e:
                print(f"Error: {e}")
                
        elif choice == "0":
            print("Goodbye!")
            break
        else:
            print("Invalid choice")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "interactive":
        interactive_demo()
    else:
        run_full_demo()
