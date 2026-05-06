# Sprint 2 Contract: Marketplace `is_public` opt-in

## What I will build

Adds an explicit opt-in visibility flag on `agents`. Existing rows hard-cut to invisible (per Lead Q3). Default registration leaves description/pricing/capabilities empty so the agent has no fingerprint to leak even after opt-in. `discover_agents`, `marketplace`, and direct profile lookup honour the flag; self-lookup bypasses it. CLI/SDK gain explicit `publish` / `unpublish` affordances.

### Files touched

| File | Change |
|------|--------|
| `sthrip/db/models.py` | `Agent.is_public BOOLEAN NOT NULL DEFAULT false` (indexed) |
| `migrations/versions/r9s0t1u2v3w4_agent_is_public.py` | NEW — add column, index, downgrade drops both |
| `api/schemas.py` | `AgentRegistration`: explicitly drops any client-supplied `is_public` (always false at registration). `AgentResponse` adds `is_public`. `AgentProfileResponse` adds `is_public`. `AgentSettingsUpdate` adds `is_public: Optional[bool]`. `AgentMarketplaceResponse` adds `is_public`. |
| `sthrip/services/agent_registry.py` | `register_agent` always sets `is_public=False`; `discover_agents`, `count_agents`, `search_agents`, `get_profile`, `get_profile_by_address` filter `is_public=True` for non-self callers; `_agent_to_profile` carries `is_public`; `AgentProfile` dataclass gains `is_public` |
| `api/routers/agents.py` | `register_agent` ignores `is_public` from request; `marketplace`, `GET /v2/agents`, `GET /v2/agents/{name}` filter on `is_public`; `update_agent_settings` accepts `is_public`; `GET /v2/me` returns `is_public` |
| `cli/agent_cli/commands/me.py` | `sthrip me publish` / `sthrip me unpublish` |
| `sdk/sthrip/client.py` | `update_profile(... is_public=None)` |
| `tests/test_marketplace_is_public.py` | NEW — 8+ tests |
| `tests/test_marketplace.py` | UPDATE — fixture explicit publishes, +1 default-registration negative test |

### Migration name

`r9s0t1u2v3w4_agent_is_public.py` (down_revision = `q8r9s0t1u2v3`).

## Specific testable acceptance criteria

1. **Default registration is hidden from marketplace** — register without explicit publish → `GET /v2/agents/marketplace` does not return the agent. *Verified by* `test_default_registration_not_in_marketplace`.
2. **Default registration is hidden from `/v2/agents` discovery** — same for `GET /v2/agents`. *Verified by* `test_default_registration_not_in_discover`.
3. **Default registration is hidden from direct lookup by other agents** — `GET /v2/agents/{name}` returns 404 when caller is anonymous or another agent. *Verified by* `test_get_profile_other_returns_404_when_private`.
4. **Self-lookup bypasses the flag** — `GET /v2/me` and other self-routes still see the agent's own profile when it is private. *Verified by* `test_get_profile_self_returns_200_when_private` + `test_v2_me_works_when_private`.
5. **`publish` flips visibility on** — `PATCH /v2/me/settings {"is_public": true}` makes the agent appear in marketplace. *Verified by* `test_publish_makes_visible`.
6. **`unpublish` flips visibility off** — `PATCH /v2/me/settings {"is_public": false}` removes from marketplace. *Verified by* `test_unpublish_hides`.
7. **Default profile fields are empty** — fresh registration → `description=None`, `pricing={}`, `capabilities=[]` (per AD-4 zero-default). *Verified by* `test_default_profile_fields_empty`.
8. **Registration body cannot smuggle `is_public=true`** — passing `is_public: true` in the registration POST does NOT publish the agent. *Verified by* `test_register_cannot_set_is_public`.
9. **Migration default-false applies to legacy rows** — pre-seed an agent row with no `is_public`, run the migration, observe `is_public=False` afterwards (hard cut, per Lead Q3). *Verified by* `test_migration_existing_rows_default_false`.
10. **Alembic round-trip works** — `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` succeeds.

## How verified

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip"
pytest tests/test_marketplace_is_public.py -v
pytest --cov=sthrip/services/agent_registry --cov=api/routers/agents \
       --cov-report=term --cov-fail-under=80 \
       tests/test_marketplace_is_public.py tests/test_marketplace.py
# regression sweep on closely related suites
pytest tests/test_api.py tests/test_marketplace.py tests/test_register_agent_commit.py -x
# alembic round-trip (sqlite)
alembic upgrade head && alembic downgrade -1 && alembic upgrade head
```

Coverage target: **≥80% on changed modules** (`sthrip/services/agent_registry.py`, `api/routers/agents.py`).

## GitNexus impact

```
discover_agents (sthrip/services/agent_registry.py) → upstream
  risk: LOW
  d=1: api/routers/agents.py:marketplace (CALLS), api/routers/agents.py:discover_agents (CALLS)
  affected processes: marketplace (8 hits), discover_agents (8 hits)
  affected modules: Routers (direct), Services (direct)

AgentRegistration (api/schemas.py) → upstream
  risk: MEDIUM
  d=1 (10 imports): api/routers/{webhook_endpoints,spending_policy,reputation,payments,
       multisig_escrow,messages,health,escrow,balance,agents}.py
  d=2: examples/langchain_agent.py
  Note: it's a Pydantic class re-imported widely. We only add a default-false `is_public`
  on the response side. No required fields are introduced. Backward-compatible.

Agent (sthrip/db/models.py) → upstream
  ~50+ test files import the model. Adding a NOT-NULL column with `server_default="false"`
  is safe for all existing INSERTs. Read paths that don't filter on the new column
  remain correct.
```

## Out of scope

- SDK migration prompts beyond a `is_public` parameter on `update_profile()`. CHANGELOG note will accompany the SDK bump but the SDK release itself is not in this sprint.
- Email-blast to existing agents — operational task, not in code scope.
- `agents.webhook_url` removal (Sprint 5).
- Encryption of marketplace fields (out of project scope per AD-4 — opt-in is sufficient).
- Discovery-by-guess hardening beyond the 404 (rate-limiter + 404 already cover; per-agent enumeration timing oracles are a separate hardening task).
- Tor sidecar (Sprint 6).
