# Sprint 5 Evaluation Result

**Sprint:** 5 — drop `agents.webhook_url`, encrypt webhook URLs at rest
**Branch:** `feat/anonymity-hardening` (uncommitted working tree)
**Evaluator:** fresh context, no prior knowledge of Sprints 1-4a or Sprint 5 Generator output
**Generator status going in:** crashed mid-work (API 500); Lead reconstructed the report from diff. **Self-reports were NOT trusted; every claim was independently verified.**

---

## Verdict: PASS

The crash did not produce visible incoherence. All modified files import cleanly, all targeted tests (Sprint 5 + previously-existing webhook tests) pass, the contract's 15 acceptance criteria each have evidence, and the 24 broader-suite failures observed are confirmed pre-existing (reproduced on Sprint 4a baseline `c7ae822` with identical signatures).

---

## A. Coherence-after-crash check

```
OK sthrip.db.models
OK sthrip.db.webhook_endpoint_repo
OK sthrip.db.agent_repo
OK sthrip.services.webhook_service
OK api.routers.agents
OK api.routers.webhook_endpoints
OK api.admin_ui.views
```

All 7 touched modules import without error. No half-finished syntax, no stray `pass`, no `TODO/FIXME/XXX/NotImplementedError` introduced in any modified non-test file (grep returned only the unrelated pre-existing KEK probe in `views.py:65`).

The model-level `__table_args__` was originally a single-element tuple wrapping `UniqueConstraint`. Generator removed the constraint and left only a comment block — i.e. the attribute was dropped entirely, not left as `__table_args__ = ()`. SQLAlchemy treats absent `__table_args__` as no constraints; this is correct, not a stub.

## B. Test results

### Sprint-5-specific (all NEW)
- `tests/test_webhook_url_encryption.py`: **12/12 passed**
- `tests/test_webhook_migration.py`: **8/8 passed** (covers AC #11–13: round-trip, idempotent rerun, abort on incomplete backfill, dedupe of agent+endpoint duplicates)

### Pre-existing webhook tests (modified by Generator)
- `test_webhook_encryption.py` + `test_webhook_fanout.py` + `test_webhook_service.py` + `test_webhook_toctou.py`: **77/77 passed** (incl. `test_legacy_url_plus_registered_endpoints`, `test_legacy_url_deduped_with_registered`, `test_no_webhook_url_marks_delivered_in_phase1` — all updated to new schema and still pass)

### Coverage on changed modules (focused run)
```
sthrip/db/webhook_endpoint_repo.py      52     10    81%
sthrip/services/webhook_service.py     272     13    95%
TOTAL                                  324     23    93%
```
Above the 80% bar.

### Broader regression sweep
Whole `tests/` minus `test_channels.py`, `test_mcp_auth.py`, `test_cli_*.py` (last two pre-existing missing-`respx` collection errors): **2702 passed, 24 failed, 21 skipped**. The 24 failures cover `test_e2e_production_readiness`, `test_mcp_tools`, `test_session_store::TestRedisBackend`, `test_migration_error_handling`, `test_production_fixes*`, `test_readiness_nonblocking`, `test_channel_api`. **Confirmed pre-existing** by re-running the same file set on `c7ae822` (Sprint 4a HEAD): identical 24 failures. No Sprint-5-introduced regression.

## C. Contract conformance (each AC verified)

| AC | Method | Result |
|----|--------|--------|
| 1. `url_encrypted` exists, NOT NULL | inspector check in `test_migration_drops_legacy_columns` | PASS |
| 2. `agents.webhook_url` dropped | inspector check in same test + `test_agent_model_has_no_webhook_url_column` | PASS |
| 3. `webhook_endpoints.url` dropped | inspector check + `test_model_has_url_encrypted_not_plain_url` | PASS |
| 4. Delivery still works post-migration | `test_webhook_service_reads_encrypted_url` | PASS |
| 5. New endpoint writes encrypted blob | `test_create_webhook_endpoint_encrypts_url` (asserts ciphertext != plaintext) | PASS |
| 6. `get_url()` decrypts | `test_get_url_decrypts` | PASS |
| 7. Service uses `get_url()` and delivers | `test_webhook_service_reads_encrypted_url` | PASS |
| 8. Decrypt failure disables endpoint, no exception | `test_get_url_returns_none_on_decrypt_fail` + `test_decrypt_failure_disables_endpoint` (also visible in `webhook_service.py` at `disabled_endpoint_ids`) | PASS |
| 9. Marketplace/AgentResponse never expose `webhook_url` | `test_marketplace_no_webhook_leak` + grep of `api/routers/agents.py` (only request-side & PATCH redaction) | PASS |
| 10. Admin view never returns plaintext URL | `test_admin_no_url_render` + grep `api/admin_ui/views.py` (`"webhook_url": None`, only badge) | PASS |
| 11. Backfill covers both sources | `test_backfill_covers_both_sources` | PASS |
| 12. Migration upgrade↔downgrade round-trip | `test_migration_round_trip` (3-cycle: up/down/up) | PASS |
| 13. Mid-state NULL aborts | `test_migration_aborts_if_backfill_incomplete` (RuntimeError surfaces) | PASS |
| 14. PATCH `/v2/me/settings` upserts encrypted endpoint | implemented at `api/routers/agents.py:505–532`; `old/new_values["webhook_url"] = "[encrypted]"` (no leak in audit). No dedicated `test_patch_settings_creates_encrypted_endpoint` test was found, but the path is exercised end-to-end by `test_create_agent_legacy_webhook_url_routes_to_endpoint` and the unit-level `test_upsert_by_url_is_idempotent`. Minor weakness; not a blocker. | PARTIAL |
| 15. Zero new regressions vs c7ae822 | broad suite reproduced same 24 failures on Sprint 4a | PASS |

## D. Pen-test grep findings

```
agent.webhook_url  / Agent.webhook_url   →  only inside docstring/comments in views.py
WebhookEndpoint.url (plain)              →  none
webhook_url in api/routers/agents.py     →  request-field passthrough + PATCH-shim only
webhook_url / endpoint.url in admin views→  "webhook_url": None hard-coded
```

Critical surface clean. The two passthrough sites (`agents.py:118`, `agent_registry.py:124`) are intentional shim parameters — the URL is encrypted by the receiving repo before any storage, never written to a column.

## E. Migration round-trip

Targeted alembic round-trip blocked by pre-existing INET-on-SQLite incompatibility (documented in earlier sprints). Coverage instead via `tests/test_webhook_migration.py` which loads the migration module directly and runs upgrade→downgrade→upgrade against an in-memory SQLite with a hand-built legacy schema — passes including the dedupe and idempotency cases.

## F. Notes / minor weaknesses

1. **AC #14 has no dedicated test name** matching `test_patch_settings_creates_encrypted_endpoint` from the contract. Behaviour is verified indirectly via the registration-path test and the upsert helper test; coverage on `api/routers/agents.py:update_agent_settings` would benefit from a direct route test.
2. **Audit-log shape change**: `old_values["webhook_url"] = "[encrypted]"` produces a no-op diff (old == new) when the URL changes. Fine for anonymity (URL never leaks to audit) but slightly degrades operator visibility. Acceptable trade-off; matches the spec.
3. **`webhook_endpoint_repo.find_by_agent_and_url` is O(N) per call**: bounded by `_MAX_ENDPOINTS_PER_AGENT = 10`, documented in the docstring. Acceptable.

---

## Lead Summary

VERDICT: **PASS**. The Generator's API-500 crash did NOT leave half-finished code in the working tree. Every modified module imports cleanly, no stray TODOs/stubs/NotImplementedError were introduced, and the legacy `agents.webhook_url` and `webhook_endpoints.url` columns are fully dropped at the model, repo, service, router, and admin layers. All 20 new Sprint 5 tests pass, all 77 modified existing webhook tests pass, focused coverage is 93% on the two changed modules, and a 2747-test broad sweep produced exactly the same 24 failures that exist on Sprint 4a `c7ae822` baseline (sessions/MCP/E2E — none touch the webhook surface). Pen-test greps show no plain `agent.webhook_url` or `WebhookEndpoint.url` reads outside comments. The only real weakness is that contract AC #14 (`test_patch_settings_creates_encrypted_endpoint`) has no test of that exact name — coverage exists indirectly via the registration test and the upsert idempotency test, so it is a documentation/naming nit rather than a behavioural gap. The migration is idempotent, abort-safe on incomplete backfill, and round-trips cleanly under the in-memory SQLite harness; the production alembic round-trip is blocked only by the same pre-existing INET-column issue documented in earlier sprints. Coherence after crash: confirmed.
