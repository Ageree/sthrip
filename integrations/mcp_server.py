"""
StealthPay MCP (Model Context Protocol) Server
For Claude, Cursor, and other MCP-compatible AI assistants

This allows AI assistants to make payments through StealthPay
"""

import json
import asyncio
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

# MCP imports (optional)
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        Resource,
        Tool,
        TextContent,
        ErrorData,
        INTERNAL_ERROR,
        INVALID_PARAMS
    )
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    Server = object

from stealthpay import StealthPay


if MCP_AVAILABLE:
    class StealthPayMCPServer:
        """MCP Server for StealthPay"""
        
        def __init__(self, stealthpay: StealthPay):
            self.stealthpay = stealthpay
            self.server = Server("stealthpay-mcp")
            self._setup_handlers()
        
        def _setup_handlers(self):
            """Setup MCP handlers"""
            
            @self.server.list_resources()
            async def list_resources() -> List[Resource]:
                """List available resources"""
                return [
                    Resource(
                        uri="stealthpay://balance",
                        name="Wallet Balance",
                        mimeType="application/json",
                        description="Current wallet balance and address"
                    ),
                    Resource(
                        uri="stealthpay://payments",
                        name="Payment History",
                        mimeType="application/json",
                        description="Recent payment history"
                    )
                ]
            
            @self.server.read_resource()
            async def read_resource(uri: str) -> str:
                """Read resource data"""
                if uri == "stealthpay://balance":
                    info = self.stealthpay.get_info()
                    return json.dumps({
                        "address": info.address,
                        "balance": info.balance,
                        "unlocked_balance": info.unlocked_balance,
                        "height": info.height
                    })
                
                elif uri == "stealthpay://payments":
                    payments = self.stealthpay.get_payments(limit=10)
                    return json.dumps([
                        {
                            "tx_hash": p.tx_hash,
                            "amount": p.amount,
                            "status": p.status.value,
                            "timestamp": p.timestamp.isoformat()
                        }
                        for p in payments
                    ])
                
                raise ValueError(f"Unknown resource: {uri}")
            
            @self.server.list_tools()
            async def list_tools() -> List[Tool]:
                """List available tools"""
                return [
                    Tool(
                        name="send_payment",
                        description="Send an anonymous XMR payment",
                        inputSchema={
                            "type": "object",
                            "properties": {
                                "to_address": {
                                    "type": "string",
                                    "description": "Recipient's Monero address"
                                },
                                "amount": {
                                    "type": "number",
                                    "description": "Amount in XMR"
                                },
                                "memo": {
                                    "type": "string",
                                    "description": "Optional private memo"
                                }
                            },
                            "required": ["to_address", "amount"]
                        }
                    ),
                    Tool(
                        name="get_balance",
                        description="Get wallet balance",
                        inputSchema={
                            "type": "object",
                            "properties": {}
                        }
                    ),
                    Tool(
                        name="create_stealth_address",
                        description="Generate new stealth address for receiving",
                        inputSchema={
                            "type": "object",
                            "properties": {
                                "purpose": {
                                    "type": "string",
                                    "description": "Purpose of the address"
                                }
                            }
                        }
                    ),
                    Tool(
                        name="create_escrow",
                        description="Create 2-of-3 multisig escrow",
                        inputSchema={
                            "type": "object",
                            "properties": {
                                "seller_address": {"type": "string"},
                                "arbiter_address": {"type": "string"},
                                "amount": {"type": "number"},
                                "description": {"type": "string"}
                            },
                            "required": ["seller_address", "arbiter_address", "amount", "description"]
                        }
                    )
                ]
            
            @self.server.call_tool()
            async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
                """Handle tool calls"""
                try:
                    if name == "send_payment":
                        payment = self.stealthpay.pay(
                            to_address=arguments["to_address"],
                            amount=arguments["amount"],
                            memo=arguments.get("memo"),
                            privacy_level="high"
                        )
                        return [TextContent(
                            type="text",
                            text=f"Payment sent successfully!\n"
                                 f"Transaction: {payment.tx_hash}\n"
                                 f"Amount: {payment.amount} XMR\n"
                                 f"Fee: {payment.fee:.6f} XMR"
                        )]
                    
                    elif name == "get_balance":
                        info = self.stealthpay.get_info()
                        return [TextContent(
                            type="text",
                            text=f"Balance: {info.balance:.6f} XMR\n"
                                 f"Unlocked: {info.unlocked_balance:.6f} XMR\n"
                                 f"Address: {info.address}"
                        )]
                    
                    elif name == "create_stealth_address":
                        stealth = self.stealthpay.create_stealth_address(
                            purpose=arguments.get("purpose", "mcp-payment")
                        )
                        return [TextContent(
                            type="text",
                            text=f"New stealth address generated:\n{stealth.address}\n"
                                 f"Share this with the sender. Can only be used once."
                        )]
                    
                    elif name == "create_escrow":
                        escrow = self.stealthpay.create_escrow(
                            seller_address=arguments["seller_address"],
                            arbiter_address=arguments["arbiter_address"],
                            amount=arguments["amount"],
                            description=arguments["description"]
                        )
                        return [TextContent(
                            type="text",
                            text=f"Escrow created!\n"
                                 f"ID: {escrow.id}\n"
                                 f"Amount: {escrow.amount} XMR\n"
                                 f"Status: {escrow.status.value}"
                        )]
                    
                    else:
                        raise ValueError(f"Unknown tool: {name}")
                        
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"Error: {str(e)}"
                    )]
        
        async def run(self):
            """Run the MCP server"""
            async with stdio_server(self.server) as streams:
                await self.server.run(
                    streams[0],
                    streams[1],
                    self.server.create_initialization_options()
                )


    async def main():
        """Main entry point"""
        stealthpay = StealthPay.from_env()
        server = StealthPayMCPServer(stealthpay)
        await server.run()

else:
    async def main():
        raise ImportError("MCP not installed. Run: pip install mcp")


if __name__ == "__main__":
    if MCP_AVAILABLE:
        asyncio.run(main())
    else:
        print("MCP not available. Install with: pip install mcp")
