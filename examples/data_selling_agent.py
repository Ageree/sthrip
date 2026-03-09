"""
Example: AI Agent that sells data via API and accepts XMR payments
This agent runs a service and sells data to other agents
"""

import json
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

from sthrip import Sthrip


class DataSellingAgent:
    """
    Agent that sells research data for XMR.
    
    Example services:
    - Weather data
    - Stock prices
    - News sentiment
    - Translation
    """
    
    def __init__(self, port=8080):
        self.port = port
        self.sthrip = Sthrip.from_env()
        self.pending_payments = {}  # Track payment requests
        self.price_per_request = 0.001  # XMR
        
        # Service catalog
        self.services = {
            "weather": {
                "price": 0.001,
                "description": "Current weather for any city",
                "endpoint": "/api/weather"
            },
            "stock_price": {
                "price": 0.002,
                "description": "Real-time stock prices",
                "endpoint": "/api/stock"
            },
            "sentiment": {
                "price": 0.005,
                "description": "Sentiment analysis of text",
                "endpoint": "/api/sentiment"
            }
        }
    
    def create_payment_request(self, service: str) -> dict:
        """
        Create payment request for a service.
        Returns stealth address where client should pay.
        """
        if service not in self.services:
            raise ValueError(f"Unknown service: {service}")
        
        # Generate unique stealth address
        stealth = self.sthrip.create_stealth_address(
            purpose=f"{service}-{int(time.time())}"
        )
        
        request_id = f"req_{int(time.time())}_{hash(stealth.address) % 10000}"
        
        self.pending_payments[request_id] = {
            "service": service,
            "address": stealth.address,
            "price": self.services[service]["price"],
            "created_at": time.time(),
            "paid": False,
            "delivered": False
        }
        
        return {
            "request_id": request_id,
            "service": service,
            "price_xmr": self.services[service]["price"],
            "payment_address": stealth.address,
            "instructions": f"Send exactly {self.services[service]['price']} XMR to the address above. Payment will be detected automatically.",
            "expires_in": 600  # 10 minutes
        }
    
    def check_payment(self, request_id: str) -> bool:
        """Check if payment was received for request"""
        if request_id not in self.pending_payments:
            return False
        
        req = self.pending_payments[request_id]
        
        if req["paid"]:
            return True
        
        # Check recent payments
        payments = self.sthrip.get_payments(incoming=True, limit=50)
        
        for payment in payments:
            # Check if amount matches (with small tolerance)
            if abs(payment.amount - req["price"]) < 0.0001:
                if payment.is_confirmed:
                    req["paid"] = True
                    req["payment_tx"] = payment.tx_hash
                    return True
        
        return False
    
    def provide_service(self, request_id: str, **params) -> dict:
        """Provide service after payment confirmed"""
        if not self.check_payment(request_id):
            return {
                "error": "Payment not received",
                "status": "pending_payment",
                "request_id": request_id
            }
        
        req = self.pending_payments[request_id]
        service = req["service"]
        
        # Generate response based on service
        if service == "weather":
            result = self._get_weather(params.get("city", "London"))
        elif service == "stock_price":
            result = self._get_stock_price(params.get("symbol", "AAPL"))
        elif service == "sentiment":
            result = self._analyze_sentiment(params.get("text", ""))
        else:
            result = {"error": "Unknown service"}
        
        req["delivered"] = True
        
        return {
            "request_id": request_id,
            "service": service,
            "data": result,
            "payment_confirmed": True
        }
    
    def _get_weather(self, city: str) -> dict:
        """Mock weather service"""
        # In production: call real weather API
        return {
            "city": city,
            "temperature": 22,
            "condition": "sunny",
            "source": "mock_data"
        }
    
    def _get_stock_price(self, symbol: str) -> dict:
        """Mock stock service"""
        return {
            "symbol": symbol,
            "price": 150.0,
            "change": "+1.5%",
            "source": "mock_data"
        }
    
    def _analyze_sentiment(self, text: str) -> dict:
        """Mock sentiment service"""
        return {
            "text_preview": text[:50] if text else "",
            "sentiment": "positive",
            "score": 0.75,
            "source": "mock_analysis"
        }
    
    def list_services(self) -> dict:
        """List available services"""
        return {
            "agent_name": "Data Provider Agent",
            "agent_address": self.sthrip.address,
            "services": self.services
        }


class AgentHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for agent API"""
    
    agent = None  # Set by server
    
    def do_GET(self):
        if self.path == "/":
            self._send_json(self.agent.list_services())
        
        elif self.path.startswith("/request/"):
            service = self.path.split("/")[-1]
            try:
                result = self.agent.create_payment_request(service)
                self._send_json(result)
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
        
        else:
            self._send_json({"error": "Not found"}, 404)
    
    def do_POST(self):
        if self.path.startswith("/deliver/"):
            request_id = self.path.split("/")[-1]
            
            # Read POST body
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            params = json.loads(body) if body else {}
            
            result = self.agent.provide_service(request_id, **params)
            status = 200 if "error" not in result else 402
            self._send_json(result, status)
        
        else:
            self._send_json({"error": "Not found"}, 404)
    
    def _send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())
    
    def log_message(self, format, *args):
        # Suppress logs
        pass


def run_agent_server(port=8080):
    """Run the agent server"""
    agent = DataSellingAgent(port=port)
    AgentHTTPHandler.agent = agent
    
    server = HTTPServer(('0.0.0.0', port), AgentHTTPHandler)
    
    print(f"🤖 Data Selling Agent started on port {port}")
    print(f"   Address: {agent.sthrip.address}")
    print(f"   Services: {list(agent.services.keys())}")
    print(f"\n   Endpoints:")
    print(f"   GET  /              - List services")
    print(f"   GET  /request/<svc> - Create payment request")
    print(f"   POST /deliver/<id>  - Get data after payment")
    print(f"\n   Press Ctrl+C to stop")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n   Stopping...")
        server.shutdown()


if __name__ == "__main__":
    run_agent_server()
