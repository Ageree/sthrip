"""
Sthrip integration for LangChain
Enables LangChain agents to make anonymous payments
"""

from typing import Optional, Type
from pydantic import BaseModel, Field

# Optional import - only if LangChain installed
try:
    from langchain.tools import BaseTool
    from langchain.callbacks.manager import CallbackManagerForToolRun
    LANGCHAIN_AVAILABLE = True
except ImportError:
    LANGCHAIN_AVAILABLE = False
    BaseTool = object
    CallbackManagerForToolRun = None

from sthrip import Sthrip


if LANGCHAIN_AVAILABLE:
    class SthripBaseTool(BaseTool):
        """Base tool with Sthrip client"""
        
        sthrip: Sthrip = Field(default=None)
        
        def __init__(self, sthrip: Sthrip, **kwargs):
            super().__init__(**kwargs)
            self.sthrip = sthrip


    class SendPaymentInput(BaseModel):
        """Input for sending payment"""
        to_address: str = Field(description="Recipient's Monero address")
        amount: float = Field(description="Amount in XMR to send")
        memo: Optional[str] = Field(default=None, description="Optional private memo")


    class SthripSendTool(SthripBaseTool):
        """Tool for sending anonymous payments"""
        
        name: str = "sthrip_send"
        description: str = """
        Send an anonymous payment in XMR (Monero) to another agent or address.
        Use this when you need to pay for a service or data.
        The payment will be completely private and untraceable.
        """
        args_schema: Type[BaseModel] = SendPaymentInput
        
        def _run(
            self,
            to_address: str,
            amount: float,
            memo: Optional[str] = None,
            run_manager: Optional[CallbackManagerForToolRun] = None
        ) -> str:
            try:
                payment = self.sthrip.pay(
                    to_address=to_address,
                    amount=amount,
                    memo=memo,
                    privacy_level="high"
                )
                return f"Payment sent: {payment.tx_hash}, Amount: {amount} XMR"
            except Exception as e:
                return f"Error: {str(e)}"


    class GetBalanceInput(BaseModel):
        """Input for getting balance"""
        pass


    class SthripBalanceTool(SthripBaseTool):
        """Tool for checking wallet balance"""
        
        name: str = "sthrip_balance"
        description: str = "Check your current wallet balance in XMR"
        args_schema: Type[BaseModel] = GetBalanceInput
        
        def _run(
            self,
            run_manager: Optional[CallbackManagerForToolRun] = None
        ) -> str:
            info = self.sthrip.get_info()
            return f"Balance: {info.balance:.6f} XMR, Unlocked: {info.unlocked_balance:.6f} XMR"


    class CreateStealthAddressInput(BaseModel):
        """Input for creating stealth address"""
        purpose: Optional[str] = Field(default="payment", description="Purpose of the address")


    class SthripAddressTool(SthripBaseTool):
        """Tool for generating stealth addresses"""
        
        name: str = "sthrip_address"
        description: str = "Generate a new stealth address for receiving payments"
        args_schema: Type[BaseModel] = CreateStealthAddressInput
        
        def _run(
            self,
            purpose: Optional[str] = "payment",
            run_manager: Optional[CallbackManagerForToolRun] = None
        ) -> str:
            stealth = self.sthrip.create_stealth_address(purpose=purpose)
            return f"New stealth address: {stealth.address}"


    def get_sthrip_tools(sthrip: Sthrip):
        """Get all Sthrip tools for LangChain"""
        return [
            SthripSendTool(sthrip=sthrip),
            SthripBalanceTool(sthrip=sthrip),
            SthripAddressTool(sthrip=sthrip),
        ]

else:
    def get_sthrip_tools(sthrip: Sthrip):
        raise ImportError("LangChain not installed. Run: pip install langchain")
