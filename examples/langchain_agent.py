"""
Example: LangChain agent with Sthrip capabilities
This agent can make payments autonomously
"""

import os
import sys

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sthrip import Sthrip
from sthrip.integrations.langchain import get_sthrip_tools

# Try to import LangChain
try:
    from langchain.agents import initialize_agent, AgentType
    from langchain.llms import OpenAI
    from langchain.chat_models import ChatOpenAI
    LANGCHAIN_AVAILABLE = True
except ImportError:
    LANGCHAIN_AVAILABLE = False
    print("LangChain not installed. Install with: pip install langchain openai")


def create_payment_agent():
    """
    Create a LangChain agent that can make payments.
    
    This agent can:
    - Check wallet balance
    - Send payments
    - Generate addresses
    - Create escrows
    """
    
    if not LANGCHAIN_AVAILABLE:
        return None
    
    # Initialize Sthrip
    sthrip = Sthrip.from_env()
    
    # Get tools
    tools = get_sthrip_tools(sthrip)
    
    # Initialize LLM
    llm = ChatOpenAI(temperature=0, model="gpt-4")
    
    # Create agent
    agent = initialize_agent(
        tools,
        llm,
        agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION,
        verbose=True,
        handle_parsing_errors=True
    )
    
    return agent


def demo_payment_task():
    """Demo: Agent pays for a service"""
    
    print("=" * 60)
    print("🤖 LangChain Agent with Sthrip Demo")
    print("=" * 60)
    
    if not LANGCHAIN_AVAILABLE:
        print("\n❌ LangChain not available. Install with:")
        print("   pip install langchain openai")
        return
    
    # Create agent
    agent = create_payment_agent()
    if not agent:
        return
    
    # Example tasks
    tasks = [
        "Check my wallet balance",
        "Create a new address for receiving payments",
        # Note: Real payment requires actual XMR and address
        # "Send 0.01 XMR to 44test... for data purchase",
    ]
    
    for task in tasks:
        print(f"\n📝 Task: {task}")
        print("-" * 40)
        
        try:
            result = agent.run(task)
            print(f"\n✅ Result: {result}")
        except Exception as e:
            print(f"\n❌ Error: {e}")


def demo_autonomous_agent():
    """
    Demo: Autonomous agent that decides when to pay
    
    This simulates an agent that:
    1. Needs to buy weather data
    2. Checks if it has enough balance
    3. Creates payment address
    4. Makes payment
    5. Confirms transaction
    """
    
    print("\n" + "=" * 60)
    print("🤖 Autonomous Payment Agent Demo")
    print("=" * 60)
    
    if not LANGCHAIN_AVAILABLE:
        print("\n❌ LangChain not available")
        return
    
    agent = create_payment_agent()
    if not agent:
        return
    
    # Complex task
    task = """
    You are an AI agent that needs to buy weather data for Tokyo.
    
    The data costs 0.001 XMR.
    
    Your task:
    1. Check your wallet balance
    2. If you have enough funds, create a stealth address to give to the seller
    3. If balance is insufficient, report that you cannot proceed
    
    Do not actually send any payment without user confirmation.
    Just check balance and prepare the address.
    """
    
    print(f"\n📝 Complex Task:")
    print(task)
    print("-" * 40)
    
    try:
        result = agent.run(task)
        print(f"\n✅ Agent decision: {result}")
    except Exception as e:
        print(f"\n❌ Error: {e}")


class AutonomousResearchAgent:
    """
    Production-ready autonomous agent that:
    - Researches topics
    - Buys data when needed
    - Tracks expenses
    - Maintains privacy
    """
    
    def __init__(self, budget_xmr: float = 1.0):
        self.sthrip = Sthrip.from_env()
        self.budget = budget_xmr
        self.spent = 0.0
        self.purchases = []
        
        if LANGCHAIN_AVAILABLE:
            self.langchain_agent = create_payment_agent()
        else:
            self.langchain_agent = None
    
    def can_afford(self, amount: float) -> bool:
        """Check if agent can afford purchase"""
        info = self.sthrip.get_info()
        return info.balance >= amount and (self.spent + amount) <= self.budget
    
    def buy_data_if_needed(
        self,
        service_url: str,
        service_name: str,
        max_price: float
    ) -> dict:
        """
        Intelligently decide whether to buy data.
        
        Returns data if purchased, error otherwise.
        """
        # Check if we can afford it
        if not self.can_afford(max_price):
            return {
                "error": "Insufficient funds",
                "balance": self.sthrip.get_info().balance,
                "budget_remaining": self.budget - self.spent
            }
        
        # Buy the data
        from examples.data_buying_agent import DataBuyingAgent
        buyer = DataBuyingAgent()
        buyer.sthrip = self.sthrip
        
        result = buyer.buy_data(service_url, service_name)
        
        if "error" not in result:
            self.spent += max_price
            self.purchases.append({
                "service": service_name,
                "price": max_price,
                "data": result.get("data")
            })
        
        return result
    
    def research(self, topic: str) -> dict:
        """
        Research a topic, buying data as needed.
        
        Example:
            agent.research("weather in Tokyo tomorrow")
            - Checks if we have weather API
            - If not, buys access
            - Returns weather data
        """
        print(f"🔍 Researching: {topic}")
        
        # This is a simplified example
        # Real implementation would parse topic, determine needs, etc.
        
        return {
            "topic": topic,
            "budget_used": self.spent,
            "budget_remaining": self.budget - self.spent,
            "purchases": len(self.purchases)
        }


def main():
    """Main entry point"""
    print("\nSelect demo:")
    print("1. Basic payment tasks")
    print("2. Autonomous agent")
    print("3. Production agent example")
    
    try:
        choice = input("\nChoice (1-3): ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = "1"
    
    if choice == "1":
        demo_payment_task()
    elif choice == "2":
        demo_autonomous_agent()
    elif choice == "3":
        agent = AutonomousResearchAgent(budget_xmr=0.1)
        result = agent.research("example topic")
        print(f"\nResult: {result}")
    else:
        print("Invalid choice")


if __name__ == "__main__":
    main()
