"""
TDD tests for Payment Channel feature — channel_repo extensions + channel_service.

RED phase: these tests are written before implementation.
"""

import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch, MagicMock

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance,
    PaymentChannel, ChannelUpdate, FeeCollection,
    ChannelStatus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CHANNEL_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    PaymentChannel.__table__,
    ChannelUpdate.__table__,
    FeeCollection.__table__,
]


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=_CHANNEL_TEST_TABLES)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def db(session_factory):
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _make_agent(db, name="agent-a"):
    agent = Agent(agent_name=name, is_active=True)
    db.add(agent)
    db.flush()
    rep = AgentReputation(agent_id=agent.id, trust_score=50)
    db.add(rep)
    balance = AgentBalance(agent_id=agent.id, token="XMR", available=Decimal("10.0"))
    db.add(balance)
    db.flush()
    return agent


def _make_channel_hash(a_id, b_id):
    raw = f"{a_id}{b_id}{secrets.token_hex(4)}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# TestChannelRepoExtended — repo-layer tests
# ---------------------------------------------------------------------------

class TestChannelRepoExtended:
    """Test the new ChannelRepository methods added for P3b."""

    def test_open_with_deposit_sets_balances_and_status(self, db):
        from sthrip.db.channel_repo import ChannelRepository

        agent_a = _make_agent(db, "chan-a")
        agent_b = _make_agent(db, "chan-b")
        ch_hash = _make_channel_hash(agent_a.id, agent_b.id)

        repo = ChannelRepository(db)
        channel = repo.open_with_deposit(
            channel_hash=ch_hash,
            agent_a_id=agent_a.id,
            agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
            deposit_b=Decimal("3.0"),
            settlement_period=3600,
        )
        db.flush()

        assert channel.status == ChannelStatus.OPEN
        assert channel.deposit_a == Decimal("5.0")
        assert channel.deposit_b == Decimal("3.0")
        assert channel.balance_a == Decimal("5.0")
        assert channel.balance_b == Decimal("3.0")
        assert channel.capacity == Decimal("8.0")
        assert channel.nonce == 0

    def test_submit_update_creates_channel_update_record(self, db):
        from sthrip.db.channel_repo import ChannelRepository

        agent_a = _make_agent(db, "upd-a")
        agent_b = _make_agent(db, "upd-b")
        ch_hash = _make_channel_hash(agent_a.id, agent_b.id)

        repo = ChannelRepository(db)
        channel = repo.open_with_deposit(
            channel_hash=ch_hash,
            agent_a_id=agent_a.id,
            agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
            deposit_b=Decimal("3.0"),
            settlement_period=3600,
        )
        db.flush()

        update = repo.submit_update(
            channel_id=channel.id,
            nonce=1,
            balance_a=Decimal("4.0"),
            balance_b=Decimal("4.0"),
            signature_a="sig-a-1",
            signature_b="sig-b-1",
        )
        db.flush()

        assert update.channel_id == channel.id
        assert update.nonce == 1
        assert update.balance_a == Decimal("4.0")
        assert update.balance_b == Decimal("4.0")
        assert update.signature_a == "sig-a-1"
        assert update.signature_b == "sig-b-1"

    def test_get_latest_update_returns_highest_nonce(self, db):
        from sthrip.db.channel_repo import ChannelRepository

        agent_a = _make_agent(db, "lat-a")
        agent_b = _make_agent(db, "lat-b")
        ch_hash = _make_channel_hash(agent_a.id, agent_b.id)

        repo = ChannelRepository(db)
        channel = repo.open_with_deposit(
            channel_hash=ch_hash,
            agent_a_id=agent_a.id,
            agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
            deposit_b=Decimal("3.0"),
            settlement_period=3600,
        )
        db.flush()

        repo.submit_update(channel.id, 1, Decimal("4.0"), Decimal("4.0"), "sa1", "sb1")
        repo.submit_update(channel.id, 2, Decimal("3.5"), Decimal("4.5"), "sa2", "sb2")
        db.flush()

        latest = repo.get_latest_update(channel.id)
        assert latest is not None
        assert latest.nonce == 2

    def test_get_latest_update_returns_none_when_no_updates(self, db):
        from sthrip.db.channel_repo import ChannelRepository

        agent_a = _make_agent(db, "none-a")
        agent_b = _make_agent(db, "none-b")
        ch_hash = _make_channel_hash(agent_a.id, agent_b.id)

        repo = ChannelRepository(db)
        channel = repo.open_with_deposit(
            channel_hash=ch_hash,
            agent_a_id=agent_a.id,
            agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
            deposit_b=Decimal("0"),
            settlement_period=3600,
        )
        db.flush()

        result = repo.get_latest_update(channel.id)
        assert result is None

    def test_initiate_settlement_sets_closing_status_and_closes_at(self, db):
        from sthrip.db.channel_repo import ChannelRepository

        agent_a = _make_agent(db, "sett-a")
        agent_b = _make_agent(db, "sett-b")
        ch_hash = _make_channel_hash(agent_a.id, agent_b.id)

        repo = ChannelRepository(db)
        channel = repo.open_with_deposit(
            channel_hash=ch_hash,
            agent_a_id=agent_a.id,
            agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
            deposit_b=Decimal("3.0"),
            settlement_period=3600,
        )
        db.flush()

        rows = repo.initiate_settlement(
            channel_id=channel.id,
            nonce=1,
            balance_a=Decimal("4.0"),
            balance_b=Decimal("4.0"),
            sig_a="sa",
            sig_b="sb",
        )
        db.flush()

        assert rows >= 1
        db.refresh(channel)
        assert channel.status == ChannelStatus.CLOSING
        assert channel.closes_at is not None

    def test_settle_channel_transitions_closing_to_settled(self, db):
        from sthrip.db.channel_repo import ChannelRepository

        agent_a = _make_agent(db, "fin-a")
        agent_b = _make_agent(db, "fin-b")
        ch_hash = _make_channel_hash(agent_a.id, agent_b.id)

        repo = ChannelRepository(db)
        channel = repo.open_with_deposit(
            channel_hash=ch_hash,
            agent_a_id=agent_a.id,
            agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
            deposit_b=Decimal("3.0"),
            settlement_period=3600,
        )
        db.flush()
        repo.initiate_settlement(channel.id, 1, Decimal("4.0"), Decimal("4.0"), "sa", "sb")
        db.flush()

        rows = repo.settle(channel.id)
        db.flush()

        assert rows >= 1
        db.refresh(channel)
        assert channel.status == ChannelStatus.SETTLED

    def test_finalize_close_transitions_settled_to_closed(self, db):
        from sthrip.db.channel_repo import ChannelRepository

        agent_a = _make_agent(db, "fc-a")
        agent_b = _make_agent(db, "fc-b")
        ch_hash = _make_channel_hash(agent_a.id, agent_b.id)

        repo = ChannelRepository(db)
        channel = repo.open_with_deposit(
            channel_hash=ch_hash,
            agent_a_id=agent_a.id,
            agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
            deposit_b=Decimal("3.0"),
            settlement_period=3600,
        )
        db.flush()
        repo.initiate_settlement(channel.id, 1, Decimal("4.0"), Decimal("4.0"), "sa", "sb")
        db.flush()
        repo.settle(channel.id)
        db.flush()

        rows = repo.finalize_close(channel.id)
        db.flush()

        assert rows >= 1
        db.refresh(channel)
        assert channel.status == ChannelStatus.CLOSED
        assert channel.closed_at is not None

    def test_dispute_updates_state_during_closing(self, db):
        from sthrip.db.channel_repo import ChannelRepository

        agent_a = _make_agent(db, "dis-a")
        agent_b = _make_agent(db, "dis-b")
        ch_hash = _make_channel_hash(agent_a.id, agent_b.id)

        repo = ChannelRepository(db)
        channel = repo.open_with_deposit(
            channel_hash=ch_hash,
            agent_a_id=agent_a.id,
            agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
            deposit_b=Decimal("3.0"),
            settlement_period=3600,
        )
        db.flush()
        repo.initiate_settlement(channel.id, 1, Decimal("4.0"), Decimal("4.0"), "sa", "sb")
        db.flush()

        rows = repo.dispute(
            channel_id=channel.id,
            nonce=2,
            balance_a=Decimal("2.0"),
            balance_b=Decimal("6.0"),
            sig_a="sa2",
            sig_b="sb2",
        )
        db.flush()

        assert rows >= 1
        db.refresh(channel)
        assert channel.nonce == 2
        assert channel.balance_a == Decimal("2.0")
        assert channel.balance_b == Decimal("6.0")

    def test_get_channels_ready_to_settle_returns_expired_closing(self, db):
        from sthrip.db.channel_repo import ChannelRepository

        agent_a = _make_agent(db, "rts-a")
        agent_b = _make_agent(db, "rts-b")
        ch_hash = _make_channel_hash(agent_a.id, agent_b.id)

        repo = ChannelRepository(db)
        channel = repo.open_with_deposit(
            channel_hash=ch_hash,
            agent_a_id=agent_a.id,
            agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
            deposit_b=Decimal("3.0"),
            settlement_period=3600,
        )
        db.flush()
        repo.initiate_settlement(channel.id, 1, Decimal("4.0"), Decimal("4.0"), "sa", "sb")
        db.flush()

        # Manually backdate closes_at so it's already expired
        db.query(PaymentChannel).filter(
            PaymentChannel.id == channel.id
        ).update({"closes_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
        db.flush()

        results = repo.get_channels_ready_to_settle()
        assert any(c.id == channel.id for c in results)

    def test_get_channels_ready_to_settle_excludes_future_channels(self, db):
        from sthrip.db.channel_repo import ChannelRepository

        agent_a = _make_agent(db, "exc-a")
        agent_b = _make_agent(db, "exc-b")
        ch_hash = _make_channel_hash(agent_a.id, agent_b.id)

        repo = ChannelRepository(db)
        channel = repo.open_with_deposit(
            channel_hash=ch_hash,
            agent_a_id=agent_a.id,
            agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
            deposit_b=Decimal("3.0"),
            settlement_period=3600,
        )
        db.flush()
        repo.initiate_settlement(channel.id, 1, Decimal("4.0"), Decimal("4.0"), "sa", "sb")
        db.flush()
        # closes_at is now + 3600, still in the future — should not appear

        results = repo.get_channels_ready_to_settle()
        assert not any(c.id == channel.id for c in results)


# ---------------------------------------------------------------------------
# TestChannelService — service-layer tests
# ---------------------------------------------------------------------------

class TestChannelService:
    """Test ChannelService business logic."""

    @pytest.fixture(autouse=True)
    def _patches(self):
        """Patch audit_log and queue_webhook for all service tests."""
        with (
            patch("sthrip.services.channel_service.audit_log"),
            patch("sthrip.services.channel_service.queue_webhook"),
        ):
            yield

    def test_open_channel_deducts_balance_and_returns_dict(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "svc-open-a")
        agent_b = _make_agent(db, "svc-open-b")

        svc = ChannelService()
        result = svc.open_channel(
            db=db,
            agent_a_id=agent_a.id,
            agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
            deposit_b=Decimal("0"),
            settlement_period=3600,
        )

        assert result["status"] == "open"
        assert result["deposit_a"] == "5.0"
        assert result["deposit_b"] == "0"
        assert result["balance_a"] == "5.0"
        assert result["balance_b"] == "0"
        assert "channel_id" in result

        # Balance deducted
        balance = db.query(AgentBalance).filter(
            AgentBalance.agent_id == agent_a.id
        ).first()
        assert balance.available == Decimal("5.0")  # 10 - 5

    def test_open_channel_insufficient_balance_raises(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "insuf-a")
        agent_b = _make_agent(db, "insuf-b")

        svc = ChannelService()
        with pytest.raises(ValueError, match="[Ii]nsufficient"):
            svc.open_channel(
                db=db,
                agent_a_id=agent_a.id,
                agent_b_id=agent_b.id,
                deposit_a=Decimal("999.0"),
                deposit_b=Decimal("0"),
            )

    def test_open_channel_self_raises(self, db):
        from sthrip.services.channel_service import ChannelService

        agent = _make_agent(db, "self-open")

        svc = ChannelService()
        with pytest.raises(ValueError, match="[Ss]elf|[Ss]ame|different"):
            svc.open_channel(
                db=db,
                agent_a_id=agent.id,
                agent_b_id=agent.id,
                deposit_a=Decimal("1.0"),
            )

    def test_open_channel_zero_deposit_raises(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "zero-a")
        agent_b = _make_agent(db, "zero-b")

        svc = ChannelService()
        with pytest.raises(ValueError):
            svc.open_channel(
                db=db,
                agent_a_id=agent_a.id,
                agent_b_id=agent_b.id,
                deposit_a=Decimal("0"),
                deposit_b=Decimal("0"),
            )

    def test_submit_update_stores_new_state(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "upd-svc-a")
        agent_b = _make_agent(db, "upd-svc-b")

        svc = ChannelService()
        channel = svc.open_channel(
            db=db,
            agent_a_id=agent_a.id,
            agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
        )
        channel_id = channel["channel_id"]

        result = svc.submit_update(
            db=db,
            channel_id=channel_id,
            agent_id=agent_a.id,
            nonce=1,
            balance_a=Decimal("4.0"),
            balance_b=Decimal("1.0"),
            signature_a="sig-a",
            signature_b="sig-b",
        )

        assert result["nonce"] == 1
        assert result["balance_a"] == "4.0"
        assert result["balance_b"] == "1.0"

    def test_submit_update_rejects_stale_nonce(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "nonce-a")
        agent_b = _make_agent(db, "nonce-b")

        svc = ChannelService()
        ch = svc.open_channel(
            db=db, agent_a_id=agent_a.id, agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
        )
        cid = ch["channel_id"]

        svc.submit_update(db, cid, agent_a.id, 1, Decimal("4.0"), Decimal("1.0"), "sa", "sb")

        with pytest.raises(ValueError, match="[Nn]once"):
            svc.submit_update(db, cid, agent_a.id, 1, Decimal("4.0"), Decimal("1.0"), "sa", "sb")

    def test_submit_update_rejects_conservation_violation(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "cons-a")
        agent_b = _make_agent(db, "cons-b")

        svc = ChannelService()
        ch = svc.open_channel(
            db=db, agent_a_id=agent_a.id, agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
        )
        cid = ch["channel_id"]

        with pytest.raises(ValueError, match="[Cc]onservation|[Bb]alance|[Tt]otal"):
            svc.submit_update(
                db, cid, agent_a.id, 1,
                Decimal("4.0"), Decimal("2.0"),  # 4+2=6 != 5 total
                "sa", "sb",
            )

    def test_submit_update_rejects_non_participant(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "perm-a")
        agent_b = _make_agent(db, "perm-b")
        outsider = _make_agent(db, "outsider")

        svc = ChannelService()
        ch = svc.open_channel(
            db=db, agent_a_id=agent_a.id, agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
        )

        with pytest.raises(PermissionError):
            svc.submit_update(
                db, ch["channel_id"], outsider.id, 1,
                Decimal("4.0"), Decimal("1.0"), "sa", "sb",
            )

    def test_settle_fee_calculation(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "fee-a")
        agent_b = _make_agent(db, "fee-b")

        svc = ChannelService()
        ch = svc.open_channel(
            db=db, agent_a_id=agent_a.id, agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
        )
        cid = ch["channel_id"]

        # agent_a sends 2 XMR to agent_b: deposit_a was 5, new balance_a=3, transfer=2
        result = svc.settle(
            db=db,
            channel_id=cid,
            agent_id=agent_a.id,
            nonce=1,
            balance_a=Decimal("3.0"),
            balance_b=Decimal("2.0"),
            sig_a="sa",
            sig_b="sb",
        )

        # fee = 1% of |balance_a - deposit_a| = 1% of 2 = 0.02
        assert Decimal(result["fee"]) == Decimal("0.02")
        assert result["status"] == "closing"

    def test_settle_rejects_missing_signatures(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "nosig-a")
        agent_b = _make_agent(db, "nosig-b")

        svc = ChannelService()
        ch = svc.open_channel(
            db=db, agent_a_id=agent_a.id, agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
        )

        with pytest.raises(ValueError, match="[Ss]ignature"):
            svc.settle(
                db=db,
                channel_id=ch["channel_id"],
                agent_id=agent_a.id,
                nonce=1,
                balance_a=Decimal("3.0"),
                balance_b=Decimal("2.0"),
                sig_a="",
                sig_b="sb",
            )

    def test_close_after_settlement_credits_balances(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "close-a")
        agent_b = _make_agent(db, "close-b")

        svc = ChannelService()
        ch = svc.open_channel(
            db=db, agent_a_id=agent_a.id, agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
        )
        cid = ch["channel_id"]

        # Settle: agent_a sends 2 to agent_b; deposit_a=5, balance_a=3, balance_b=2
        svc.settle(
            db=db, channel_id=cid, agent_id=agent_a.id,
            nonce=1, balance_a=Decimal("3.0"), balance_b=Decimal("2.0"),
            sig_a="sa", sig_b="sb",
        )
        # Move channel to SETTLED directly
        from uuid import UUID as _UUID
        db.query(PaymentChannel).filter(PaymentChannel.id == _UUID(cid)).update(
            {"status": ChannelStatus.SETTLED}
        )
        db.flush()

        result = svc.close(db=db, channel_id=cid, agent_id=agent_a.id)

        assert result["status"] == "closed"
        # agent_a should have: 5 (initial) - 5 (deposit) + 3 (return) - fee
        # agent_b should have: 10 (initial) + 2 (return)
        bal_b = db.query(AgentBalance).filter(
            AgentBalance.agent_id == agent_b.id
        ).first()
        assert bal_b.available == Decimal("12.0")

    def test_dispute_during_closing_updates_state(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "disp-svc-a")
        agent_b = _make_agent(db, "disp-svc-b")

        svc = ChannelService()
        ch = svc.open_channel(
            db=db, agent_a_id=agent_a.id, agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
        )
        cid = ch["channel_id"]

        svc.settle(
            db=db, channel_id=cid, agent_id=agent_a.id,
            nonce=1, balance_a=Decimal("3.0"), balance_b=Decimal("2.0"),
            sig_a="sa", sig_b="sb",
        )

        result = svc.dispute(
            db=db, channel_id=cid, agent_id=agent_b.id,
            nonce=2, balance_a=Decimal("2.0"), balance_b=Decimal("3.0"),
            sig_a="sa2", sig_b="sb2",
        )

        assert result["nonce"] == 2
        assert result["balance_a"] == "2.0"
        assert result["balance_b"] == "3.0"

    def test_dispute_rejects_lower_nonce(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "lown-a")
        agent_b = _make_agent(db, "lown-b")

        svc = ChannelService()
        ch = svc.open_channel(
            db=db, agent_a_id=agent_a.id, agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
        )
        cid = ch["channel_id"]

        svc.settle(
            db=db, channel_id=cid, agent_id=agent_a.id,
            nonce=3, balance_a=Decimal("3.0"), balance_b=Decimal("2.0"),
            sig_a="sa", sig_b="sb",
        )

        with pytest.raises(ValueError, match="[Nn]once"):
            svc.dispute(
                db=db, channel_id=cid, agent_id=agent_b.id,
                nonce=2, balance_a=Decimal("2.0"), balance_b=Decimal("3.0"),
                sig_a="sa2", sig_b="sb2",
            )

    def test_get_channel_returns_dict_for_participant(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "get-a")
        agent_b = _make_agent(db, "get-b")

        svc = ChannelService()
        ch = svc.open_channel(
            db=db, agent_a_id=agent_a.id, agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
        )

        result = svc.get_channel(db=db, channel_id=ch["channel_id"], agent_id=agent_a.id)
        assert result["channel_id"] == ch["channel_id"]

    def test_get_channel_raises_for_non_participant(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "nopart-a")
        agent_b = _make_agent(db, "nopart-b")
        outsider = _make_agent(db, "nopart-out")

        svc = ChannelService()
        ch = svc.open_channel(
            db=db, agent_a_id=agent_a.id, agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
        )

        with pytest.raises(PermissionError):
            svc.get_channel(db=db, channel_id=ch["channel_id"], agent_id=outsider.id)

    def test_get_channel_raises_for_unknown(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "unk-a")

        svc = ChannelService()
        with pytest.raises(LookupError):
            svc.get_channel(db=db, channel_id=uuid4(), agent_id=agent_a.id)

    def test_list_channels_returns_agent_channels(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "list-a")
        agent_b = _make_agent(db, "list-b")
        agent_c = _make_agent(db, "list-c")

        svc = ChannelService()
        svc.open_channel(db=db, agent_a_id=agent_a.id, agent_b_id=agent_b.id, deposit_a=Decimal("1.0"))
        svc.open_channel(db=db, agent_a_id=agent_a.id, agent_b_id=agent_c.id, deposit_a=Decimal("1.0"))

        result = svc.list_channels(db=db, agent_id=agent_a.id, limit=10, offset=0)
        assert result["total"] == 2
        assert len(result["channels"]) == 2

    def test_auto_settle_expired_settles_closing_channels(self, db):
        from sthrip.services.channel_service import ChannelService

        agent_a = _make_agent(db, "auto-a")
        agent_b = _make_agent(db, "auto-b")

        svc = ChannelService()
        ch = svc.open_channel(
            db=db, agent_a_id=agent_a.id, agent_b_id=agent_b.id,
            deposit_a=Decimal("5.0"),
        )
        cid = ch["channel_id"]

        svc.settle(
            db=db, channel_id=cid, agent_id=agent_a.id,
            nonce=1, balance_a=Decimal("5.0"), balance_b=Decimal("0"),
            sig_a="sa", sig_b="sb",
        )
        # Backdate closes_at
        from uuid import UUID as _UUID
        db.query(PaymentChannel).filter(PaymentChannel.id == _UUID(cid)).update(
            {"closes_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
        )
        db.flush()

        count = svc.auto_settle_expired(db)
        assert count >= 1
