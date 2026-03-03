"""
StealthPay integration for LangChain
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

from stealthpay import StealthPay


if LANGCHAIN_AVAILABLE:
    class StealthPayBaseTool(BaseTool):
        """Base tool with StealthPay client"""
        
        stealthpay: StealthPay = Field(default=None)
        
        def __init__(self, stealthpay: StealthPay, **kwargs):
            super().__init__(**kwargs)
            self.stealthpay = stealthpay


    class SendPaymentInput(BaseModel):
        """Input for sending payment"""
        to_address: str = Field(description="Recipient's Monero address")
        amount: float = Field(description="Amount in XMR to send")
        memo: Optional[str] = Field(default=None, description="Optional private memo")


    class StealthPaySendTool(StealthPayBaseTool):
        """Tool for sending anonymous payments"""
        
        name: str = "stealthpay_send"
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
                payment = self.stealthpay.pay(
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


    class StealthPayBalanceTool(StealthPayBaseTool):
        """Tool for checking wallet balance"""
        
        name: str = "stealthpay_balance"
        description: str = "Check your current wallet balance in XMR"
        args_schema: Type[BaseModel] = GetBalanceInput
        
        def _run(
            self,
            run_manager: Optional[CallbackManagerForToolRun] = None
        ) -> str:
            info = self.stealthpay.get_info()
            return f"Balance: {info.balance:.6f} XMR, Unlocked: {info.unlocked_balance:.6f} XMR"


    class CreateStealthAddressInput(BaseModel):
        """Input for creating stealth address"""
        purpose: Optional[str] = Field(default="payment", description="Purpose of the address")


    class StealthPayAddressTool(StealthPayBaseTool):
        """Tool for generating stealth addresses"""
        
        name: str = "stealthpay_address"
        description: str = "Generate a new stealth address for receiving payments"
        args_schema: Type[BaseModel] = CreateStealthAddressInput
        
        def _run(
            self,
            purpose: Optional[str] = "payment",
            run_manager: Optional[CallbackManagerForToolRun] = None
        ) -> str:
            stealth = self.stealthpay.create_stealth_address(purpose=purpose)
            return f"New stealth address: {stealth.address}"


    def get_stealthpay_tools(stealthpay: StealthPay):
        """Get all StealthPay tools for LangChain"""
        return [
            StealthPaySendTool(stealthpay=stealthpay),
            StealthPayBalanceTool(stealthpay=stealthpay),
            StealthPayAddressTool(stealthpay=stealthpay),
        ]

else:
    def get_stealthpay_tools(stealthpay: StealthPay):
        raise ImportError("LangChain not installed. Run: pip install langchain")
