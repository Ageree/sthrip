# Sprint 2 Generator Report

## Status: ready-for-review

All contract acceptance criteria are met. Zero regressions vs Sprint 1
baseline. New tests (18) green; coverage on changed modules 83%; alembic
upgrade -> downgrade -> upgrade round-trip succeeded.

## Files changed (git diff --stat)

```
.harness/anonymize-platform/state.json |  15 ++++--
api/routers/agents.py                  |  11 +++++
api/schemas.py                         |   9 ++++
cli/agent_cli/commands/me.py           |  18 ++++++++
sdk/sthrip/client.py                   |  10 +++-
sthrip/db/models.py                    |  10 ++++
sthrip/services/agent_registry.py      |  84 ++++++++++++++++++++++++++--------
tests/test_access_control.py           |  56 ++++++++++++-----------
tests/test_discovery_v2.py             |  20 +++++++-
tests/test_marketplace.py              |  23 +++++++++-
+ NEW migrations/versions/r9s0t1u2v3w4_agent_is_public.py
+ NEW tests/test_marketplace_is_public.py
+ NEW .harness/anonymize-platform/sprint-2-contract.md
```

## Test results

### New sprint-2 tests
- `tests/test_marketplace_is_public.py`: **18 passed** (covers all 10 contract ACs)
  - TestDefaultRegistrationHidden (2)
  - TestPublishToggle (2)
  - TestProfileLookupGate (4)
  - TestDefaultProfileFieldsEmpty (2)
  - TestRegisterCannotSetIsPublic (1)
  - TestMigrationHardCut (1)
  - TestGetProfileByAddressGate (3)
  - TestUpdateSettingsIsPublic (3)

### Regression sweep
Compared `git stash` baseline vs Sprint-2 branch on
```
pytest tests/ --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py --ignore=tests/test_zk_reputation.py
```

- **Sprint 2 added regressions: 0**
- The two ignored cli tests fail on baseline due to a missing `respx` package
  (pre-existing). `test_zk_reputation.py` skipped (slow, irrelevant to this sprint).
- Pre-existing baseline failures (~24): idempotency_keys table missing in test
  fixture, migration_error_handling using shared `os.environ` patches, MCP
  server tools count drift, session_store redis-mock setup. All untouched by
  Sprint 2.

### Coverage on changed modules

```
Name                                Stmts   Miss  Cover
api/routers/agents.py                 200     34    83%
sthrip/services/agent_registry.py     179     30    83%
TOTAL                                 379     64    83%
```

Both modules ≥80%. Missed lines are pre-existing (verify_agent admin path,
get_leaderboard, get_stats, get_registry singleton init), NOT in the Sprint 2
diff scope.

## Migration round-trip

```
$ python /tmp/sprint2_alembic_smoke.py
OK — round-trip upgrade -> downgrade -> upgrade succeeded
```

The smoke harness:
1. Creates a minimal `agents` table without `is_public`.
2. Pre-seeds a legacy row with `agent_name='legacy-agent'`.
3. Runs `migration.upgrade()` — verifies `is_public` column added,
   `ix_agents_is_public` index created.
4. Reads the legacy row — confirms hard-cut: `is_public=False`.
5. Runs `migration.downgrade()` — verifies column and index removed.
6. Runs `migration.upgrade()` again — verifies idempotent re-creation.

Full alembic CLI `upgrade head` against in-memory sqlite is blocked by a
**pre-existing** sqlite incompatibility on the unrelated `api_sessions.ip_address`
column (PostgreSQL `INET` type, not supported by sqlite). This is independent
of Sprint 2 — the Sprint 1 migration `q8r9s0t1u2v3` ships without that issue
either, so the team's deploy path is Postgres-only. Round-trip on the new
migration in isolation is verified above.

## Existing tests modified (and why)

| File | Change | Reason |
|------|--------|--------|
| `tests/test_marketplace.py` | `_register()` helper now publishes via `PATCH /v2/me/settings {"is_public": true}` after registering | Existing assertions (24 tests) presume marketplace/discover visibility; default-private behaviour is asserted in `test_marketplace_is_public.py` instead |
| `tests/test_discovery_v2.py` | `_register()` helper now publishes after registering | 13 tests assert sort/filter/response shape on marketplace endpoint; need agents to be visible |
| `tests/test_access_control.py` | TestPrivacyGating tests (5) refactored to use a new local `_register_and_publish` helper instead of inlined POST | Tests assert ``xmr_address`` redaction in the public profile/discover responses, which presumes the agent is reachable |

Total existing tests touched: **42** (24 in test_marketplace.py + 13 in test_discovery_v2.py + 5 in test_access_control.py). All now green.

## Self-check vs contract acceptance criteria

| AC | Status | Test |
|----|--------|------|
| 1. Default registration hidden from marketplace | PASS | `test_default_registration_not_in_marketplace` |
| 2. Default registration hidden from `/v2/agents` discovery | PASS | `test_default_registration_not_in_discover` |
| 3. Direct lookup 404 for non-self callers | PASS | `test_get_profile_other_returns_404_when_private` |
| 4. Self-lookup bypasses gate | PASS | `test_get_profile_self_returns_200_when_private`, `test_v2_me_works_when_private`, `test_by_address_self_bypasses_gate` |
| 5. publish flips visibility on | PASS | `test_publish_makes_visible` |
| 6. unpublish flips visibility off | PASS | `test_unpublish_hides` |
| 7. Default profile fields empty | PASS | `test_default_profile_fields_empty` |
| 8. Registration body cannot smuggle is_public=true | PASS | `test_register_cannot_set_is_public` |
| 9. Migration default-false applies to legacy rows | PASS | `test_migration_existing_rows_default_false` + manual smoke |
| 10. Alembic round-trip works | PASS | `/tmp/sprint2_alembic_smoke.py` |

## Notes for Evaluator

1. **`AgentRegistration` schema unchanged** — `is_public` is NOT added to it.
   The contract said "ignored OR 422"; we chose ignored (Pydantic silently
   drops unknown fields by default). The defence-in-depth is at the
   service layer: `register_agent` always assigns `agent.is_public = False`
   regardless of any kwargs. Test 8 passes both branches.

2. **`AgentResponse.is_public: bool = False`** — declared with a default so
   existing call-sites (10 routers import this schema) keep compiling. Sprint
   1 returned no `is_public` field; clients that don't deserialise it are
   unaffected.

3. **`PATCH /v2/me/settings`** — added `is_public` rather than a new endpoint.
   The contract allowed either; the existing endpoint was the lower-disruption
   path (one schema, one router function, one audit-log entry already wired).
   `update_agent_settings` audits the old/new values exactly the same way it
   audits `accepts_escrow`, so the trail is consistent.

4. **`get_profile_by_address` signature change** — added an optional
   `requesting_agent_id` parameter for symmetry with `get_profile`. The
   only caller in the codebase (the SDK's address-based lookup) does not
   pass it today; this is non-breaking. Sprint 3+ consumers can pass the
   id when they have it.

5. **`AgentProfile` dataclass added a required `is_public` field**. Every
   construction site is `_agent_to_profile`, which I updated. Searched for
   any direct `AgentProfile(...)` instantiation outside the service module
   — there isn't one (it's an internal DTO). Confirmed via
   `gitnexus_context({name: "AgentProfile"})` returning only the registry as
   a definer.

6. **Impact analysis** — `discover_agents` returned LOW risk; `marketplace`
   returned 0 callers (only the route handler calls it). `AgentRegistration`
   imported by 10 routers — but those imports are for type annotation, not
   for instantiating; my change only adds a default-false flag, which is
   wire-compatible. `Agent` model imported by ~50 tests — adding a
   `NOT NULL DEFAULT false` column is safe for all existing INSERTs because
   SQLAlchemy lets the server-default fill in, and the `Boolean(default=False)`
   ORM-level default applies on Python-side construction.

7. **What I'm worried about** —
   - **API consumers reading `is_public` field they didn't expect.** Old
     clients that hard-code response shape may complain about extra keys.
     Most JSON clients ignore unknown fields, but a strict-validating client
     would break. Contract notes a CHANGELOG entry will accompany the SDK
     bump; I haven't written that CHANGELOG (out of scope per spec).
   - **PostgreSQL `server_default='false'` vs SQLite.** SQLite stores it as
     int 0; SQLAlchemy's Boolean type abstracts this, so reads return Python
     `False`. Verified in `test_migration_existing_rows_default_false`.
   - **Production rollout side-effect.** Per Lead Q3 the hard cut is
     intentional, but every existing prod agent will silently disappear from
     the marketplace on deploy. The SDK migration prompt is out of scope
     (per contract Out of Scope section), so operators must read
     PRIVACY_FEATURES.md / SDK CHANGELOG. The DB migration itself is correct.
   - **Sprint 1 idempotency_keys baseline failure.** Not introduced here, but
     it's worth flagging back to Lead — the test fixture is missing a table
     it should have and 23+ pre-existing tests fail because of it. Out of
     Sprint 2 scope but Lead may want a follow-up sprint to fix the fixture.

## Out of scope confirmations

- No SDK migration prompts (just `is_public` parameter added to `update_profile`).
- No email-blast to existing agents (operational task).
- No `agents.webhook_url` removal (Sprint 5).
- No marketplace-field encryption (out of project scope per AD-4).
- No production deploy / Railway changes (per workflow rules).
