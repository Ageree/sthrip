"""
Example: AI Agent that buys data from other agents
This agent consumes services from data-selling agents
"""

import requests
import time

from stealthpay import StealthPay


class DataBuyingAgent:
    """
    Agent that buys data/services from other agents using XMR.
    
    Example workflow:
    1. Discover service from another agent
    2. Create payment request (get stealth address)
    3. Send XMR payment
    4. Wait for confirmation
    5. Receive data
    """
    
    def __init__(self):
        self.stealthpay = StealthPay.from_env()
        self.purchases = []
    
    def discover_service(self, agent_url: str) -> dict:
        """
        Discover available services from an agent.
        
        Args:
            agent_url: URL of the selling agent (e.g., http://localhost:8080)
        
        Returns:
            Service catalog
        """
        try:
            response = requests.get(f"{agent_url}/", timeout=10)
            return response.json()
        except Exception as e:
            return {"error": f"Failed to connect: {e}"}
    
    def buy_data(
        self,
        agent_url: str,
        service: str,
        params: dict = None,
        max_wait_seconds: int = 600
    ) -> dict:
        """
        Buy data from another agent.
        
        Complete workflow:
        1. Request payment address
        2. Send XMR
        3. Wait for confirmation
        4. Get data
        
        Args:
            agent_url: Seller's URL
            service: Service name (e.g., "weather")
            params: Service parameters (e.g., {"city": "London"})
            max_wait_seconds: Max time to wait
        
        Returns:
            Purchased data or error
        """
        params = params or {}
        
        print(f"🔍 Buying '{service}' from {agent_url}")
        
        # Step 1: Create payment request
        print("   Step 1: Creating payment request...")
        try:
            response = requests.get(
                f"{agent_url}/request/{service}",
                timeout=10
            )
            payment_req = response.json()
            
            if "error" in payment_req:
                return payment_req
            
            print(f"   💰 Price: {payment_req['price_xmr']} XMR")
            print(f"   📍 Pay to: {payment_req['payment_address'][:40]}...")
            
        except Exception as e:
            return {"error": f"Failed to create request: {e}"}
        
        # Step 2: Send payment
        print("   Step 2: Sending payment...")
        try:
            payment = self.stealthpay.pay(
                to_address=payment_req['payment_address'],
                amount=payment_req['price_xmr'],
                memo=f"Payment for {service}",
                privacy_level="high"
            )
            print(f"   ✅ Payment sent: {payment.tx_hash[:30]}...")
            
        except Exception as e:
            return {"error": f"Payment failed: {e}"}
        
        # Step 3: Wait for confirmation
        print("   Step 3: Waiting for confirmation...")
        confirmed = False
        start_time = time.time()
        
        while time.time() - start_time < max_wait_seconds:
            # Check our payment status
            status = self.stealthpay.get_payment(payment.tx_hash)
            if status and status.is_confirmed:
                confirmed = True
                print(f"   ✅ Payment confirmed!")
                break
            
            print(f"   ⏳ Waiting... ({int(time.time() - start_time)}s)")
            time.sleep(30)  # Check every 30 seconds
        
        if not confirmed:
            return {
                "error": "Payment not confirmed in time",
                "tx_hash": payment.tx_hash
            }
        
        # Step 4: Get data
        print("   Step 4: Requesting data...")
        try:
            response = requests.post(
                f"{agent_url}/deliver/{payment_req['request_id']}",
                json=params,
                timeout=10
            )
            data = response.json()
            
            if "error" in data:
                return data
            
            print(f"   ✅ Data received!")
            
            # Record purchase
            self.purchases.append({
                "service": service,
                "price": payment_req['price_xmr'],
                "tx_hash": payment.tx_hash,
                "data": data
            })
            
            return data
            
        except Exception as e:
            return {"error": f"Failed to get data: {e}"}
    
    def get_purchase_history(self) -> list:
        """Get history of all purchases"""
        return self.purchases
    
    def auto_buy(
        self,
        agent_url: str,
        services: list,
        budget_xmr: float
    ) -> list:
        """
        Automatically buy multiple services within budget.
        
        Args:
            agent_url: Seller URL
            services: List of service names to buy
            budget_xmr: Maximum total spend
        
        Returns:
            List of purchased data
        """
        print(f"🤖 Auto-buying from {agent_url}")
        print(f"   Budget: {budget_xmr} XMR")
        print(f"   Services: {services}")
        
        # First, discover services and prices
        catalog = self.discover_service(agent_url)
        
        if "error" in catalog:
            return [catalog]
        
        available = catalog.get("services", {})
        total_cost = 0
        purchases = []
        
        for service in services:
            if service not in available:
                print(f"   ⚠️  Service not available: {service}")
                continue
            
            price = available[service]["price"]
            
            if total_cost + price > budget_xmr:
                print(f"   ⚠️  Budget exceeded, skipping: {service}")
                continue
            
            print(f"\n   Buying: {service} ({price} XMR)")
            result = self.buy_data(agent_url, service)
            
            if "error" not in result:
                total_cost += price
                purchases.append(result)
                print(f"   ✅ Success! Remaining budget: {budget_xmr - total_cost:.4f} XMR")
            else:
                print(f"   ❌ Failed: {result['error']}")
        
        print(f"\n📊 Summary:")
        print(f"   Purchased: {len(purchases)}/{len(services)} services")
        print(f"   Total spent: {total_cost:.4f} XMR")
        
        return purchases


def demo():
    """Demo of buying agent"""
    buyer = DataBuyingAgent()
    
    print("=" * 60)
    print("🤖 Data Buying Agent Demo")
    print("=" * 60)
    
    # Show balance
    info = buyer.stealthpay.get_info()
    print(f"\n💰 My balance: {info.balance:.4f} XMR")
    print(f"📍 My address: {info.address[:40]}...")
    
    # Discover services
    seller_url = "http://localhost:8080"
    print(f"\n🔍 Discovering services at {seller_url}...")
    
    catalog = buyer.discover_service(seller_url)
    
    if "error" in catalog:
        print(f"❌ Could not connect: {catalog['error']}")
        print("\n   (Start the data_selling_agent.py first)")
        return
    
    print(f"   Found agent: {catalog.get('agent_name', 'Unknown')}")
    print(f"   Services:")
    for name, details in catalog.get("services", {}).items():
        print(f"      - {name}: {details['price']} XMR - {details['description']}")
    
    # Buy weather data
    print(f"\n🌤️  Buying weather data...")
    result = buyer.buy_data(
        agent_url=seller_url,
        service="weather",
        params={"city": "Tokyo"}
    )
    
    if "error" not in result:
        print(f"\n   Data received:")
        print(f"   {result.get('data', {})}")
    else:
        print(f"\n   Error: {result['error']}")
    
    # Show purchase history
    print(f"\n📜 Purchase history:")
    for p in buyer.get_purchase_history():
        print(f"   - {p['service']}: {p['price']} XMR")


if __name__ == "__main__":
    demo()
