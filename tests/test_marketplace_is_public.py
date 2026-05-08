"""Sprint 2 (anonymity-hardening): Marketplace `is_public` opt-in tests.

Covers AC #3 from .harness/anonymize-platform/user-criteria.md:
- `agents.is_public` defaults to false
- discover/marketplace/profile filter on it for non-self callers
- self-lookup bypasses the flag
- registration cannot set is_public=true (always defaults to false)
- default profile fields (description/pricing/capabilities) are empty
- legacy rows hard-cut to is_public=false on migration

Reuses the shared ``client`` fixture from ``tests/conftest.py``.
"""
from __future__ import annotations

import uuid as _uuid

import pytest

from sthrip.db.models import Agent

# Valid stagenet address shape for API registration (95 chars, base58, '5'-prefix).
_VALID_XMR_ADDR = "5" + "a" * 94


def _auth(api_key: str) -> dict:
    """Bearer auth header."""
    return {"Authorization": f"Bearer {api_key}"}


def _register(client, name: str, **extra) -> str:
    """Register an agent with a baseline payload; return its API key."""
    payload = {"agent_name": name, "xmr_address": _VALID_XMR_ADDR}
    payload.update(extra)
    r = client.post("/v2/agents/register", json=payload)
    assert r.status_code == 201, f"Registration of '{name}' failed: {r.text}"
    return r.json()["api_key"]


def _publish(client, api_key: str) -> None:
    """Flip ``is_public=true`` for the calling agent."""
    r = client.patch(
        "/v2/me/settings",
        json={"is_public": True},
        headers=_auth(api_key),
    )
    assert r.status_code == 200, f"publish failed: {r.text}"
    assert "is_public" in r.json()["updated"]


# ---------------------------------------------------------------------------
# 1. Default registration -> not in marketplace / discovery
# ---------------------------------------------------------------------------


class TestDefaultRegistrationHidden:
    """A freshly-registered agent must NOT appear in any public listing."""

    def test_default_registration_not_in_marketplace(self, client):
        _register(client, "private-by-default-1")
        r = client.get("/v2/agents/marketplace")
        assert r.status_code == 200
        names = [item["agent_name"] for item in r.json()["items"]]
        assert "private-by-default-1" not in names

    def test_default_registration_not_in_discover(self, client):
        _register(client, "private-by-default-2")
        r = client.get("/v2/agents")
        assert r.status_code == 200
        names = [item["agent_name"] for item in r.json()["items"]]
        assert "private-by-default-2" not in names


# ---------------------------------------------------------------------------
# 2. publish / unpublish toggle
# ---------------------------------------------------------------------------


class TestPublishToggle:
    """update_profile(is_public=True/False) flips marketplace visibility."""

    def test_publish_makes_visible(self, client):
        key = _register(client, "publishable")
        _publish(client, key)

        r = client.get("/v2/agents/marketplace")
        names = [item["agent_name"] for item in r.json()["items"]]
        assert "publishable" in names

        r2 = client.get("/v2/agents")
        names2 = [item["agent_name"] for item in r2.json()["items"]]
        assert "publishable" in names2

    def test_unpublish_hides(self, client):
        key = _register(client, "now-you-see-me")
        _publish(client, key)
        # confirm visible
        names = [it["agent_name"] for it in client.get("/v2/agents/marketplace").json()["items"]]
        assert "now-you-see-me" in names

        # unpublish
        r = client.patch(
            "/v2/me/settings",
            json={"is_public": False},
            headers=_auth(key),
        )
        assert r.status_code == 200
        assert "is_public" in r.json()["updated"]

        names_after = [it["agent_name"] for it in client.get("/v2/agents/marketplace").json()["items"]]
        assert "now-you-see-me" not in names_after


# ---------------------------------------------------------------------------
# 3. Direct profile lookup is gated for non-self callers
# ---------------------------------------------------------------------------


class TestProfileLookupGate:
    """GET /v2/agents/{name} returns 404 to other callers when private."""

    def test_get_profile_other_returns_404_when_private(self, client):
        # Agent A registers privately. Agent B (or anonymous) tries to fetch.
        _register(client, "alice-private")
        r = client.get("/v2/agents/alice-private")
        assert r.status_code == 404

    def test_get_profile_self_returns_200_when_private(self, client):
        # Self-lookup via GET /v2/me succeeds even when private.
        key = _register(client, "alice-self")
        r = client.get("/v2/me", headers=_auth(key))
        assert r.status_code == 200
        assert r.json()["agent_name"] == "alice-self"

    def test_v2_me_works_when_private(self, client):
        """Authenticated /v2/me routes never gate on is_public."""
        key = _register(client, "stay-hidden")
        r = client.get("/v2/me", headers=_auth(key))
        assert r.status_code == 200
        # is_public surfaces on /v2/me so the agent can introspect their state
        assert r.json().get("is_public") is False

    def test_get_profile_returns_200_after_publish(self, client):
        """Once the agent publishes, the public profile endpoint is reachable."""
        key = _register(client, "bob-public")
        _publish(client, key)
        r = client.get("/v2/agents/bob-public")
        assert r.status_code == 200
        assert r.json()["agent_name"] == "bob-public"


# ---------------------------------------------------------------------------
# 4. Default profile fields are empty (no fingerprint leak)
# ---------------------------------------------------------------------------


class TestDefaultProfileFieldsEmpty:
    """Per AD-4: default profile carries no description/pricing/capabilities."""

    def test_default_profile_fields_empty(self, client):
        key = _register(client, "no-fingerprint")
        # /v2/me confirms the in-DB defaults
        r = client.get("/v2/me", headers=_auth(key))
        assert r.status_code == 200
        data = r.json()
        assert data["description"] is None
        assert data["pricing"] == {}
        assert data["capabilities"] == []
        # accepts_escrow has a long-standing default of true; not in scope.

    def test_publish_with_empty_profile_is_allowed(self, client):
        """An agent may opt-in to be public without filling in fingerprint fields.

        We deliberately do not block this — the threat model is "leak by default,
        not leak by accident". Once they explicitly publish they have consented.
        """
        key = _register(client, "consenting-blank")
        _publish(client, key)

        r = client.get("/v2/agents/consenting-blank")
        assert r.status_code == 200
        body = r.json()
        assert body["description"] is None
        assert body["pricing"] == {}
        assert body["capabilities"] == []


# ---------------------------------------------------------------------------
# 5. Registration body cannot bypass the gate
# ---------------------------------------------------------------------------


class TestRegisterCannotSetIsPublic:
    """Passing is_public=true in the registration POST must not publish."""

    def test_register_cannot_set_is_public(self, client):
        # Server may accept (silently drop) or reject — either way the agent
        # MUST NOT end up public.
        r = client.post(
            "/v2/agents/register",
            json={
                "agent_name": "sneaky",
                "xmr_address": _VALID_XMR_ADDR,
                "is_public": True,
            },
        )
        # Allow either behaviour: 201 with field ignored, OR 422 hard-reject.
        assert r.status_code in (201, 422), f"unexpected status: {r.status_code} {r.text}"

        # Marketplace must not see the agent regardless.
        names = [it["agent_name"] for it in client.get("/v2/agents/marketplace").json()["items"]]
        assert "sneaky" not in names

        # Direct lookup must 404 too.
        assert client.get("/v2/agents/sneaky").status_code == 404


# ---------------------------------------------------------------------------
# 6. Migration: existing rows default to is_public=false (hard cut)
# ---------------------------------------------------------------------------


class TestMigrationHardCut:
    """Pre-seeding an Agent row without is_public yields is_public=False at read time.

    The SQLite schema in conftest creates the column from the model definition,
    so the server-default of False applies automatically. This mirrors the
    behaviour of the real Alembic migration which sets
    ``server_default='false'`` on column ADD with no backfill — every existing
    row is read as False.
    """

    def test_migration_existing_rows_default_false(self, db_engine, db_session_factory):
        # Seed an agent directly in the DB without specifying is_public.
        session = db_session_factory()
        try:
            agent = Agent(
                id=_uuid.uuid4(),
                agent_name="legacy-agent",
                xmr_address=_VALID_XMR_ADDR,
            )
            session.add(agent)
            session.commit()

            # Re-read and assert default.
            fetched = session.query(Agent).filter(
                Agent.agent_name == "legacy-agent"
            ).first()
            assert fetched is not None
            assert fetched.is_public is False, (
                "Hard cut violated — legacy rows must read as is_public=False"
            )
        finally:
            session.close()


# ---------------------------------------------------------------------------
# 7. update_profile via PATCH /v2/me/settings — full happy path
# ---------------------------------------------------------------------------


class TestGetProfileByAddressGate:
    """get_profile_by_address must honour the same is_public gate as get_profile."""

    def test_by_address_returns_none_when_private(self, client):
        """Direct service-level lookup of a private agent by address yields None."""
        from sthrip.services.agent_registry import get_registry

        # Use a unique xmr_address so we can find this agent specifically.
        addr = "5" + "b" * 94
        _register(client, "addr-private", xmr_address=addr)

        # Bypass the API: call the registry directly so we can pass requesting_agent_id.
        registry = get_registry()
        # Anonymous caller -> gate blocks -> None.
        assert registry.get_profile_by_address(addr, network="monero") is None

    def test_by_address_returns_profile_after_publish(self, client):
        from sthrip.services.agent_registry import get_registry

        addr = "5" + "c" * 94
        key = _register(client, "addr-public", xmr_address=addr)
        _publish(client, key)
        registry = get_registry()
        prof = registry.get_profile_by_address(addr, network="monero")
        assert prof is not None
        assert prof.agent_name == "addr-public"
        assert prof.is_public is True

    def test_by_address_self_bypasses_gate(self, client):
        """When the caller's id matches the target's id, gate is bypassed."""
        from sthrip.db.database import get_db
        from sthrip.db.models import Agent
        from sthrip.services.agent_registry import get_registry

        addr = "5" + "d" * 94
        _register(client, "addr-self", xmr_address=addr)

        # Look up the agent's own id from the DB.
        with get_db() as db:
            row = db.query(Agent).filter(Agent.agent_name == "addr-self").first()
            self_id = str(row.id)

        registry = get_registry()
        prof = registry.get_profile_by_address(
            addr, network="monero", requesting_agent_id=self_id,
        )
        assert prof is not None
        assert prof.agent_name == "addr-self"
        # is_public is still false (the agent didn't opt in)
        assert prof.is_public is False


class TestUpdateSettingsIsPublic:
    """PATCH /v2/me/settings with is_public field."""

    def test_settings_endpoint_accepts_is_public(self, client):
        key = _register(client, "settings-flip")
        r = client.patch(
            "/v2/me/settings",
            json={"is_public": True},
            headers=_auth(key),
        )
        assert r.status_code == 200
        assert "is_public" in r.json()["updated"]

    def test_settings_endpoint_rejects_non_bool(self, client):
        key = _register(client, "non-bool")
        r = client.patch(
            "/v2/me/settings",
            json={"is_public": "yes-please"},
            headers=_auth(key),
        )
        assert r.status_code == 422

    def test_settings_idempotent_publish_publish(self, client):
        """Publishing twice in a row is a no-op (still public)."""
        key = _register(client, "double-publish")
        _publish(client, key)
        _publish(client, key)
        names = [it["agent_name"] for it in client.get("/v2/agents/marketplace").json()["items"]]
        assert "double-publish" in names
