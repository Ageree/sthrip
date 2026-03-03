"""
Agent Registry for discovery and reputation
Public API for agent discovery
"""

import hashlib
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import desc, func, and_

from ..db.database import get_db
from ..db.models import Agent, AgentReputation, AgentTier
from ..db.repository import AgentRepository, ReputationRepository


@dataclass
class AgentProfile:
    """Public agent profile"""
    id: str
    agent_name: str
    did: Optional[str]
    tier: str
    
    # Wallet addresses (public)
    xmr_address: Optional[str]
    base_address: Optional[str]
    solana_address: Optional[str]
    
    # Reputation
    trust_score: int
    total_transactions: int
    average_rating: float
    
    # Services offered
    services: List[Dict[str, Any]]
    
    # Metadata
    verified_at: Optional[str]
    last_seen_at: Optional[str]
    created_at: str


class AgentRegistry:
    """
    Agent Registry for discovery
    
    Features:
    - Agent registration and verification
    - Public profile lookup
    - Search and discovery
    - Reputation queries
    """
    
    def __init__(self):
        pass
    
    def register_agent(
        self,
        agent_name: str,
        webhook_url: Optional[str] = None,
        privacy_level: str = "medium",
        xmr_address: Optional[str] = None,
        base_address: Optional[str] = None,
        solana_address: Optional[str] = None
    ) -> Dict:
        """
        Register new agent
        
        Returns:
            Registration result with API key (shown once)
        """
        with get_db() as db:
            # Check if name exists
            existing = db.query(Agent).filter(
                Agent.agent_name == agent_name
            ).first()
            
            if existing:
                raise ValueError(f"Agent name '{agent_name}' already taken")
            
            # Create agent
            repo = AgentRepository(db)
            agent = repo.create_agent(
                agent_name=agent_name,
                webhook_url=webhook_url,
                privacy_level=privacy_level
            )
            
            # Update wallet addresses
            if xmr_address or base_address or solana_address:
                repo.update_wallet_addresses(
                    agent.id,
                    xmr_address=xmr_address,
                    base_address=base_address,
                    solana_address=solana_address
                )
            
            db.commit()
            
            return {
                "agent_id": str(agent.id),
                "agent_name": agent_name,
                "api_key": agent._plain_api_key,  # Shown only once!
                "tier": agent.tier,
                "created_at": agent.created_at.isoformat(),
                "message": "Store this API key securely - it cannot be retrieved again"
            }
    
    def get_profile(self, agent_name: str) -> Optional[AgentProfile]:
        """Get public agent profile"""
        with get_db() as db:
            agent = db.query(Agent).filter(
                Agent.agent_name == agent_name,
                Agent.is_active == True
            ).first()
            
            if not agent:
                return None
            
            # Get reputation
            rep = None
            if agent.reputation:
                rep = agent.reputation
            
            return AgentProfile(
                id=str(agent.id),
                agent_name=agent.agent_name,
                did=agent.did,
                tier=agent.tier.value,
                xmr_address=agent.xmr_address,
                base_address=agent.base_address,
                solana_address=agent.solana_address,
                trust_score=rep.trust_score if rep else 0,
                total_transactions=rep.total_transactions if rep else 0,
                average_rating=float(rep.average_rating) if rep else 0.0,
                services=[],  # TODO: Add services table
                verified_at=agent.verified_at.isoformat() if agent.verified_at else None,
                last_seen_at=agent.last_seen_at.isoformat() if agent.last_seen_at else None,
                created_at=agent.created_at.isoformat()
            )
    
    def get_profile_by_address(
        self,
        address: str,
        network: str = "monero"
    ) -> Optional[AgentProfile]:
        """Find agent by wallet address"""
        with get_db() as db:
            if network == "monero":
                agent = db.query(Agent).filter(Agent.xmr_address == address).first()
            elif network == "base":
                agent = db.query(Agent).filter(Agent.base_address == address).first()
            elif network == "solana":
                agent = db.query(Agent).filter(Agent.solana_address == address).first()
            else:
                return None
            
            if not agent:
                return None
            
            return self.get_profile(agent.agent_name)
    
    def discover_agents(
        self,
        min_trust_score: Optional[int] = None,
        tier: Optional[str] = None,
        verified_only: bool = False,
        limit: int = 100,
        offset: int = 0
    ) -> List[AgentProfile]:
        """
        Discover agents with filters
        
        Args:
            min_trust_score: Minimum trust score (0-100)
            tier: Filter by tier (free, verified, premium, enterprise)
            verified_only: Only verified agents
            limit: Max results
            offset: Pagination offset
        """
        with get_db() as db:
            query = db.query(Agent).filter(Agent.is_active == True)
            
            if tier:
                query = query.filter(Agent.tier == tier)
            
            if verified_only:
                query = query.filter(Agent.verified_at.isnot(None))
            
            if min_trust_score:
                query = query.join(AgentReputation).filter(
                    AgentReputation.trust_score >= min_trust_score
                )
            
            agents = query.order_by(desc(Agent.created_at)).offset(offset).limit(limit).all()
            
            profiles = []
            for agent in agents:
                profile = self._agent_to_profile(agent)
                if profile:
                    profiles.append(profile)
            
            return profiles
    
    def search_agents(
        self,
        query_str: str,
        limit: int = 20
    ) -> List[AgentProfile]:
        """Search agents by name"""
        with get_db() as db:
            agents = db.query(Agent).filter(
                Agent.agent_name.ilike(f"%{query_str}%"),
                Agent.is_active == True
            ).limit(limit).all()
            
            return [self._agent_to_profile(a) for a in agents if self._agent_to_profile(a)]
    
    def get_leaderboard(self, limit: int = 100) -> List[Dict]:
        """Get top agents by trust score"""
        with get_db() as db:
            repo = ReputationRepository(db)
            top = repo.get_leaderboard(limit=limit)
            
            return [
                {
                    "rank": i + 1,
                    "agent_id": str(rep.agent_id),
                    "agent_name": rep.agent.agent_name if rep.agent else None,
                    "trust_score": rep.trust_score,
                    "total_transactions": rep.total_transactions,
                    "average_rating": float(rep.average_rating),
                    "tier": rep.agent.tier.value if rep.agent else None
                }
                for i, rep in enumerate(top)
            ]
    
    def verify_agent(
        self,
        agent_id: str,
        verified_by: str,
        tier: str = "verified"
    ) -> Dict:
        """
        Verify agent (admin only)
        
        Args:
            agent_id: Agent to verify
            verified_by: Admin identifier
            tier: New tier (verified, premium, enterprise)
        """
        with get_db() as db:
            agent = db.query(Agent).filter(Agent.id == agent_id).first()
            
            if not agent:
                raise ValueError("Agent not found")
            
            agent.tier = tier
            agent.verified_at = datetime.utcnow()
            agent.verified_by = verified_by
            
            db.commit()
            
            return {
                "agent_id": agent_id,
                "agent_name": agent.agent_name,
                "tier": tier,
                "verified_at": agent.verified_at.isoformat(),
                "verified_by": verified_by
            }
    
    def update_services(
        self,
        agent_id: str,
        services: List[Dict[str, Any]]
    ) -> Dict:
        """Update agent services (TODO: implement services table)"""
        # TODO: Implement services table
        return {
            "agent_id": agent_id,
            "services": services,
            "updated": True
        }
    
    def _agent_to_profile(self, agent: Agent) -> Optional[AgentProfile]:
        """Convert agent model to profile"""
        if not agent:
            return None
        
        rep = agent.reputation
        
        return AgentProfile(
            id=str(agent.id),
            agent_name=agent.agent_name,
            did=agent.did,
            tier=agent.tier.value,
            xmr_address=agent.xmr_address,
            base_address=agent.base_address,
            solana_address=agent.solana_address,
            trust_score=rep.trust_score if rep else 0,
            total_transactions=rep.total_transactions if rep else 0,
            average_rating=float(rep.average_rating) if rep else 0.0,
            services=[],
            verified_at=agent.verified_at.isoformat() if agent.verified_at else None,
            last_seen_at=agent.last_seen_at.isoformat() if agent.last_seen_at else None,
            created_at=agent.created_at.isoformat()
        )
    
    def get_stats(self) -> Dict:
        """Get registry statistics"""
        with get_db() as db:
            total_agents = db.query(Agent).filter(Agent.is_active == True).count()
            
            by_tier = {}
            for tier in AgentTier:
                count = db.query(Agent).filter(
                    Agent.tier == tier,
                    Agent.is_active == True
                ).count()
                by_tier[tier.value] = count
            
            verified_count = db.query(Agent).filter(
                Agent.verified_at.isnot(None),
                Agent.is_active == True
            ).count()
            
            # Active in last 24h
            day_ago = datetime.utcnow() - timedelta(hours=24)
            active_recent = db.query(Agent).filter(
                Agent.last_seen_at >= day_ago
            ).count()
            
            return {
                "total_agents": total_agents,
                "by_tier": by_tier,
                "verified_count": verified_count,
                "active_last_24h": active_recent
            }


# Global registry
_registry: Optional[AgentRegistry] = None


def get_registry() -> AgentRegistry:
    """Get global agent registry"""
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry
