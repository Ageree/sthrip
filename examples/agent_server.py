"""
Example: AI Agent as a service provider accepting anonymous payments
"""

from stealthpay import StealthPay
import time


class AnonymousAgentService:
    """
    AI Agent that provides services for anonymous XMR payments.
    No identity, no registration, no KYC.
    """
    
    def __init__(self, rpc_host="127.0.0.1", rpc_port=18082):
        self.agent = StealthPay(rpc_host=rpc_host, rpc_port=rpc_port)
        self.stealth_addresses = {}  # Track payments
        
    def offer_service(self, service_name: str, price_xmr: float):
        """
        Create a payment request for a service.
        Returns stealth address where client should pay.
        """
        # Generate unique stealth address for this request
        stealth = self.agent.create_stealth_address(
            purpose=f"service:{service_name}",
            label=f"price:{price_xmr}"
        )
        
        self.stealth_addresses[stealth.address] = {
            "service": service_name,
            "price": price_xmr,
            "paid": False,
            "stealth": stealth
        }
        
        return {
            "service": service_name,
            "price_xmr": price_xmr,
            "payment_address": stealth.address,
            "note": "Send exactly the specified amount to this address. "
                    "Payment will be detected automatically."
        }
    
    def check_payment(self, address: str) -> bool:
        """Check if payment was received for specific address"""
        if address not in self.stealth_addresses:
            return False
        
        # Get all incoming payments
        payments = self.agent.get_payments(incoming=True, outgoing=False)
        
        expected_price = self.stealth_addresses[address]["price"]
        
        for payment in payments:
            # Check if payment matches expected amount (with small tolerance)
            if abs(payment.amount - expected_price) < 0.0001:
                if payment.is_confirmed:
                    self.stealth_addresses[address]["paid"] = True
                    return True
        
        return False
    
    def provide_service(self, address: str) -> dict:
        """Provide service after payment confirmed"""
        if not self.check_payment(address):
            return {
                "error": "Payment not received or not confirmed",
                "status": "pending"
            }
        
        service_info = self.stealth_addresses[address]
        
        # Here you would actually provide the service
        # For demo, we just return success
        return {
            "service": service_info["service"],
            "status": "delivered",
            "access_url": f"https://api.agent.example/access/{address[:8]}",
            "note": "Service access granted. This URL is valid for 24 hours."
        }
    
    def get_stats(self):
        """Get service statistics"""
        total = len(self.stealth_addresses)
        paid = sum(1 for v in self.stealth_addresses.values() if v["paid"])
        
        return {
            "total_requests": total,
            "paid_requests": paid,
            "pending_requests": total - paid,
            "balance_xmr": self.agent.balance
        }


# Example usage
if __name__ == "__main__":
    print("🤖 Starting Anonymous Agent Service...")
    
    service = AnonymousAgentService()
    
    # Offer a service
    print("\n📢 Offering service: Web Search API")
    offer = service.offer_service("web_search", price_xmr=0.01)
    
    print(f"\n💳 Payment request created:")
    print(f"   Service: {offer['service']}")
    print(f"   Price: {offer['price_xmr']} XMR")
    print(f"   Pay to: {offer['payment_address']}")
    print(f"\n   {offer['note']}")
    
    # Simulate waiting for payment
    print("\n⏳ Waiting for payment...")
    print("   (In real scenario, client would send XMR to the address)")
    
    # Check payment status
    address = offer['payment_address']
    is_paid = service.check_payment(address)
    print(f"\n   Paid: {is_paid}")
    
    # If paid, provide service
    if is_paid:
        result = service.provide_service(address)
        print(f"\n✅ {result}")
    
    # Show stats
    stats = service.get_stats()
    print(f"\n📊 Stats: {stats}")
