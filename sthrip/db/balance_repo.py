"""
BalanceRepository — data-access layer for AgentBalance records.

NOTE on immutability: Balance mutations (deposit, deduct, credit) modify the
ORM object directly under row-level locking.  This is an accepted exception to
the project's immutability guidelines — all other layers pass immutable
dicts/Pydantic models.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from .models import AgentBalance


class BalanceRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create(self, agent_id: UUID, token: str = "XMR") -> AgentBalance:
        """Get balance record, create if not exists. Uses savepoint for safe race handling."""
        balance = self.db.query(AgentBalance).filter(
            AgentBalance.agent_id == agent_id,
            AgentBalance.token == token,
        ).first()
        if balance:
            return balance

        is_sqlite = self.db.bind and self.db.bind.dialect.name == "sqlite"
        try:
            savepoint = None if is_sqlite else self.db.begin_nested()
            balance = AgentBalance(agent_id=agent_id, token=token)
            self.db.add(balance)
            self.db.flush()
            return balance
        except IntegrityError:
            if savepoint is not None:
                savepoint.rollback()
            else:
                self.db.rollback()
            balance = self.db.query(AgentBalance).filter(
                AgentBalance.agent_id == agent_id,
                AgentBalance.token == token,
            ).first()
            if balance is None:
                raise RuntimeError(
                    f"Balance record for agent {agent_id} vanished after race condition"
                )
            return balance

    def _get_for_update(self, agent_id: UUID, token: str = "XMR") -> AgentBalance:
        """Get balance with row-level lock for safe mutations.

        Uses savepoint-retry to handle the race condition when two concurrent
        requests try to create the first balance for a new agent.
        """
        is_sqlite = self.db.bind and self.db.bind.dialect.name == "sqlite"

        if is_sqlite:
            balance = self.db.query(AgentBalance).filter(
                AgentBalance.agent_id == agent_id,
                AgentBalance.token == token
            ).first()
        else:
            balance = self.db.query(AgentBalance).filter(
                AgentBalance.agent_id == agent_id,
                AgentBalance.token == token
            ).with_for_update().first()

        if not balance:
            try:
                savepoint = None if is_sqlite else self.db.begin_nested()
                balance = AgentBalance(agent_id=agent_id, token=token)
                self.db.add(balance)
                self.db.flush()
            except IntegrityError:
                if savepoint is not None:
                    savepoint.rollback()
                else:
                    self.db.rollback()
                # Re-query after race: the other transaction already created it
                if is_sqlite:
                    balance = self.db.query(AgentBalance).filter(
                        AgentBalance.agent_id == agent_id,
                        AgentBalance.token == token
                    ).first()
                else:
                    balance = self.db.query(AgentBalance).filter(
                        AgentBalance.agent_id == agent_id,
                        AgentBalance.token == token
                    ).with_for_update().first()
        if balance is None:
            raise RuntimeError(
                f"Balance record for agent {agent_id} could not be created or found"
            )
        return balance

    def get_available(self, agent_id: UUID, token: str = "XMR") -> Decimal:
        """Get available balance."""
        balance = self.get_or_create(agent_id, token)
        return balance.available or Decimal("0")

    def deposit(self, agent_id: UUID, amount: Decimal, token: str = "XMR") -> AgentBalance:
        """Credit agent balance after deposit confirmed."""
        balance = self._get_for_update(agent_id, token)
        balance.available = (balance.available or Decimal("0")) + amount
        balance.total_deposited = (balance.total_deposited or Decimal("0")) + amount
        balance.updated_at = datetime.now(timezone.utc)
        return balance

    def deduct(self, agent_id: UUID, amount: Decimal, token: str = "XMR") -> AgentBalance:
        """Deduct from available balance (for hub routing). Uses row lock to prevent double-spend."""
        balance = self._get_for_update(agent_id, token)
        if (balance.available or Decimal("0")) < amount:
            raise ValueError("Insufficient balance")
        balance.available = balance.available - amount
        balance.updated_at = datetime.now(timezone.utc)
        return balance

    def withdraw(self, agent_id: UUID, amount: Decimal, token: str = "XMR") -> AgentBalance:
        """Deduct from available and increment total_withdrawn (for actual withdrawals)."""
        balance = self._get_for_update(agent_id, token)
        available = balance.available or Decimal("0")
        if available < amount:
            raise ValueError("Insufficient balance")
        balance.available = available - amount
        balance.total_withdrawn = (balance.total_withdrawn or Decimal("0")) + amount
        balance.updated_at = datetime.now(timezone.utc)
        return balance

    def credit(self, agent_id: UUID, amount: Decimal, token: str = "XMR") -> AgentBalance:
        """Credit to available balance (receiving hub payment). Uses row lock."""
        balance = self._get_for_update(agent_id, token)
        balance.available = (balance.available or Decimal("0")) + amount
        balance.updated_at = datetime.now(timezone.utc)
        return balance

    def add_pending(self, agent_id: UUID, amount: Decimal, token: str = "XMR") -> AgentBalance:
        """Add amount to pending balance (for unconfirmed deposits)."""
        balance = self._get_for_update(agent_id, token)
        balance.pending = (balance.pending or Decimal("0")) + amount
        balance.updated_at = datetime.now(timezone.utc)
        return balance

    def clear_pending_on_confirm(self, agent_id: UUID, amount: Decimal, token: str = "XMR") -> AgentBalance:
        """Move amount from pending to available (when deposit confirms)."""
        balance = self._get_for_update(agent_id, token)
        current_pending = balance.pending or Decimal("0")
        if current_pending < amount:
            logging.getLogger("sthrip").critical(
                "Pending balance underflow for agent %s: pending=%s, confirm_amount=%s. "
                "Possible accounting inconsistency.",
                agent_id, current_pending, amount,
            )
        balance.pending = max(current_pending - amount, Decimal("0"))
        balance.updated_at = datetime.now(timezone.utc)
        return balance

    def set_deposit_address(self, agent_id: UUID, address: str, token: str = "XMR") -> AgentBalance:
        """Set the deposit subaddress for an agent."""
        balance = self.get_or_create(agent_id, token)
        balance.deposit_address = address
        return balance
