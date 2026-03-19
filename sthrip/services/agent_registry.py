"""
Agent Registry for discovery and reputation
Public API for agent discovery
"""

import hashlib
import threading
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import desc, func, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from sqlalchemy.types import String as SAString

from ..db.database import get_db
from ..db.models import Agent, AgentReputation, AgentTier
from ..db.repository import AgentRepository, ReputationRepository
from ..utils import escape_ilike


def _apply_capability_filter(db, query, capability: str):
    """Apply capability filter using dialect-appropriate JSON containment.

    PostgreSQL: uses native @> (JSONB contains) via the GIN index.
    SQLite/other: falls back to LIKE on the serialized JSON text.
    """
    dialect = db.bind.dialect.name if db.bind else "sqlite"
    if dialect == "postgresql":
        import json
        query = query.filter(
            Agent.capabilities.op("@>")(func.cast(json.dumps([capability]), SAString))
        )
    else:
        # SQLite: capabilities stored as JSON text, e.g. '["translation", "code-review"]'
        query = query.filter(
            func.cast(Agent.capabilities, SAString).contains(f'"{capability}"')
        )
    return query


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

    # Marketplace
    capabilities: List[str]
    pricing: Dict[str, str]
    description: Optional[str]
    accepts_escrow: bool

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
        solana_address: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
        pricing: Optional[Dict[str, Any]] = None,
        description: Optional[str] = None,
        accepts_escrow: Optional[bool] = None,
    ) -> Dict:
        """
        Register new agent

        Returns:
            Registration result with API key (shown once)
        """
        with get_db() as db:
            # Rely solely on IntegrityError for uniqueness enforcement.
            # A pre-check query would create a timing oracle that lets
            # attackers enumerate existing agent names by measuring
            # response latency (DB query vs immediate duplicate check).
            repo = AgentRepository(db)
            try:
                agent, credentials = repo.create_agent(
                    agent_name=agent_name,
                    webhook_url=webhook_url,
                    privacy_level=privacy_level
                )

                if xmr_address or base_address or solana_address:
                    repo.update_wallet_addresses(
                        agent.id,
                        xmr_address=xmr_address,
                        base_address=base_address,
                        solana_address=solana_address
                    )

                # Set marketplace fields if provided
                if capabilities is not None:
                    agent.capabilities = capabilities
                if pricing is not None:
                    agent.pricing = pricing
                if description is not None:
                    agent.description = description
                if accepts_escrow is not None:
                    agent.accepts_escrow = accepts_escrow

                db.flush()
            except IntegrityError:
                db.rollback()
                raise ValueError("Registration failed")

            return {
                "agent_id": str(agent.id),
                "agent_name": agent_name,
                "api_key": credentials["api_key"],
                "webhook_secret": credentials["webhook_secret"],
                "tier": agent.tier,
                "created_at": agent.created_at.isoformat(),
                "message": "Store API key and webhook secret securely - they cannot be retrieved again"
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
            
            return self._agent_to_profile(agent)

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
        capability: Optional[str] = None,
        accepts_escrow: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[AgentProfile]:
        """
        Discover agents with filters

        Args:
            min_trust_score: Minimum trust score (0-100)
            tier: Filter by tier (free, verified, premium, enterprise)
            verified_only: Only verified agents
            capability: Filter by capability (JSON contains)
            accepts_escrow: Filter agents that accept escrow
            limit: Max results
            offset: Pagination offset
        """
        with get_db() as db:
            query = db.query(Agent).options(
                joinedload(Agent.reputation)
            ).filter(Agent.is_active == True)

            if tier:
                query = query.filter(Agent.tier == tier)

            if verified_only:
                query = query.filter(Agent.verified_at.isnot(None))

            if min_trust_score is not None:
                query = query.join(AgentReputation).filter(
                    AgentReputation.trust_score >= min_trust_score
                )

            if capability is not None:
                query = _apply_capability_filter(db, query, capability)

            if accepts_escrow is not None:
                query = query.filter(Agent.accepts_escrow == accepts_escrow)

            agents = query.order_by(desc(Agent.created_at)).offset(offset).limit(limit).all()

            profiles = []
            for agent in agents:
                profile = self._agent_to_profile(agent)
                if profile:
                    profiles.append(profile)

            return profiles

    def count_agents(
        self,
        min_trust_score: Optional[int] = None,
        tier: Optional[str] = None,
        verified_only: bool = False,
        capability: Optional[str] = None,
        accepts_escrow: Optional[bool] = None,
    ) -> int:
        """Count agents matching the given filters."""
        with get_db() as db:
            query = db.query(func.count(Agent.id)).filter(Agent.is_active == True)
            if tier:
                query = query.filter(Agent.tier == tier)
            if verified_only:
                query = query.filter(Agent.verified_at.isnot(None))
            if min_trust_score is not None:
                query = query.join(AgentReputation).filter(
                    AgentReputation.trust_score >= min_trust_score
                )
            if capability is not None:
                query = _apply_capability_filter(db, query, capability)
            if accepts_escrow is not None:
                query = query.filter(Agent.accepts_escrow == accepts_escrow)
            return query.scalar() or 0

    def search_agents(
        self,
        query_str: str,
        limit: int = 20
    ) -> List[AgentProfile]:
        """Search agents by name"""
        with get_db() as db:
            agents = db.query(Agent).options(
                joinedload(Agent.reputation)
            ).filter(
                Agent.agent_name.ilike(f"%{escape_ilike(query_str)}%"),
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
            # Coerce string to UUID object so the filter works correctly
            # across both PostgreSQL (native UUID) and SQLite (test backend).
            try:
                agent_uuid = _uuid.UUID(agent_id) if isinstance(agent_id, str) else agent_id
            except ValueError:
                raise ValueError("Agent not found")

            agent = db.query(Agent).filter(Agent.id == agent_uuid).first()

            if not agent:
                raise ValueError("Agent not found")

            agent.tier = tier
            agent.verified_at = datetime.now(timezone.utc)
            agent.verified_by = verified_by

            # flush() sends SQL to the DB within the current transaction without
            # committing. The single authoritative commit is performed by the
            # get_db() context manager on successful exit. An explicit commit()
            # here would create a double-commit, breaking rollback guarantees.
            db.flush()

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
        """Update agent services (services table not yet implemented)."""
        return {
            "agent_id": agent_id,
            "services": services,
            "updated": True
        }
    
    _PRIVATE_PRIVACY_LEVELS = frozenset({"high", "paranoid"})

    def _agent_to_profile(self, agent: Agent) -> Optional[AgentProfile]:
        """Convert agent model to profile.

        Redacts xmr_address for agents with high or paranoid privacy.
        """
        if not agent:
            return None

        rep = agent.reputation

        # Redact ALL wallet addresses for high/paranoid privacy levels
        privacy = (
            agent.privacy_level.value
            if hasattr(agent.privacy_level, "value")
            else str(agent.privacy_level)
        )
        is_private = privacy in self._PRIVATE_PRIVACY_LEVELS
        xmr_address = None if is_private else agent.xmr_address
        base_address = None if is_private else agent.base_address
        solana_address = None if is_private else agent.solana_address

        return AgentProfile(
            id=str(agent.id),
            agent_name=agent.agent_name,
            did=agent.did,
            tier=agent.tier.value,
            xmr_address=xmr_address,
            base_address=base_address,
            solana_address=solana_address,
            trust_score=rep.trust_score if rep else 0,
            total_transactions=rep.total_transactions if rep else 0,
            average_rating=float(rep.average_rating) if rep else 0.0,
            services=[],
            capabilities=agent.capabilities if agent.capabilities else [],
            pricing=agent.pricing if agent.pricing else {},
            description=agent.description,
            accepts_escrow=agent.accepts_escrow if agent.accepts_escrow is not None else True,
            verified_at=agent.verified_at.isoformat() if agent.verified_at else None,
            last_seen_at=agent.last_seen_at.isoformat() if agent.last_seen_at else None,
            created_at=agent.created_at.isoformat()
        )
    
    def get_stats(self) -> Dict:
        """Get registry statistics (optimized: 3 queries instead of 6)."""
        with get_db() as db:
            # Single query: count + group by tier
            rows = db.query(
                Agent.tier, func.count(Agent.id)
            ).filter(Agent.is_active == True).group_by(Agent.tier).all()

            by_tier = {row[0].value: row[1] for row in rows}
            total = sum(by_tier.values())

            verified_count = db.query(func.count(Agent.id)).filter(
                Agent.is_active == True, Agent.verified_at.isnot(None)
            ).scalar()

            # Active in last 24h
            day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
            active_recent = db.query(func.count(Agent.id)).filter(
                Agent.last_seen_at >= day_ago
            ).scalar()

            return {
                "total_agents": total,
                "by_tier": by_tier,
                "verified_count": verified_count,
                "active_last_24h": active_recent,
            }


# Global registry
_registry: Optional[AgentRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> AgentRegistry:
    """Get global agent registry"""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = AgentRegistry()
    return _registry
