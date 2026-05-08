# Sprint 5 Contract: drop agents.webhook_url, encrypt URLs

## What I will build

### Schema
- `webhook_endpoints.url_encrypted TEXT NOT NULL` (Fernet, same `WEBHOOK_ENCRYPTION_KEY` already used for `secret_encrypted`).
- Drop `webhook_endpoints.url` (legacy plaintext) AND drop `agents.webhook_url`.
- Drop `UniqueConstraint("agent_id", "url", name="uq_agent_webhook_url")` — uniqueness on encrypted ciphertext is meaningless (different ciphertexts for same plaintext under Fernet's random IV). Replace with explicit duplicate-detection at the repo layer (decrypt-then-compare) before insert.

### Migration `u2v3w4x5y6z7_drop_legacy_webhook_url.py`
Phases (idempotent, rerun-safe; same Lead style as Sprint 3):
1. ADD `webhook_endpoints.url_encrypted TEXT NULL` (skip if column already exists).
2. Backfill (in-Python, batched):
   - 2a. For each existing `webhook_endpoints` row with `url IS NOT NULL` → encrypt `url` and write to `url_encrypted` (only if `url_encrypted IS NULL`).
   - 2b. For each `agents` row with `webhook_url IS NOT NULL` AND no existing endpoint pointing at that URL — insert a synthetic endpoint row (encrypt URL, generate `secret_encrypted` from existing `agents.webhook_secret` if present, else freshly-generated whsec_).
3. Verify: assert `0 == COUNT(* WHERE url_encrypted IS NULL)` after backfill — raise RuntimeError on mismatch.
4. ALTER `webhook_endpoints.url_encrypted` SET NOT NULL.
5. Drop unique constraint `uq_agent_webhook_url` (Postgres) / skip on SQLite (recreated tables).
6. Drop `webhook_endpoints.url` IF EXISTS.
7. Drop `agents.webhook_url` IF EXISTS.

Downgrade: best-effort restore of plaintext columns by decrypting `url_encrypted` row-by-row using same Fernet key. If decryption fails for any row → raise (operator must intervene; we don't silently lose data).

### Code changes
- `sthrip/db/models.py` — remove `Agent.webhook_url`; remove `WebhookEndpoint.url`; add `WebhookEndpoint.url_encrypted` (Text, nullable=False); drop the unique constraint.
- `sthrip/db/webhook_endpoint_repo.py` — `create()` now takes plaintext `url`, encrypts internally; new helper `get_url(endpoint)` returns decrypted str or None on failure; new helper `find_by_agent_and_url(agent_id, url)` for duplicate detection.
- `sthrip/services/webhook_service.py` — line ~288 fallback `legacy_url = agent.webhook_url` removed; `delivery_targets.append({"url": ep.url, ...})` becomes `repo.get_url(ep)` and skips/disables the endpoint when decryption fails (existing failure-counter pattern reused).
- `sthrip/db/agent_repo.py` — `create_agent` no longer accepts/sets `webhook_url`; signature still accepts it for backward-compat shimming, internally creates a WebhookEndpoint row instead.
- `sthrip/services/agent_registry.py` — passes `webhook_url` through unchanged (shim path).
- `api/routers/agents.py` — `/v2/me/settings` PATCH webhook_url now upserts into `webhook_endpoints` (delete existing of same agent+url, insert encrypted). Old `setattr(db_agent, "webhook_url", ...)` removed.
- `api/admin_ui/views.py` line 97 — replace `"webhook_url": agent.webhook_url` with `"webhook_url": None` and add `"webhook_endpoint_count": <count>` and `"has_encrypted_webhook": bool(count)`. Templates already do not render the URL (verified via `grep webhook api/admin_ui/templates/`); the dict key is preserved as None for template defensiveness.
- `api/routers/webhook_endpoints.py` — `_endpoint_to_response()` keeps returning the URL because the **owner** of the endpoint always has the right to see their own URL (auth-gated by `Depends(get_current_agent)`); this is not a leak.
- Pydantic `AgentRegistration.webhook_url` and `AgentSettingsUpdate.webhook_url` — kept (request fields, not response). Validator `validate_webhook_url` kept (SSRF guard at the boundary). Per AC #5 the marketplace JSON never includes `webhook_url`; verified via grep that no `*Response` model exposes it.

### SDK
- `sdk/sthrip/client.py` (and `cli/agent_cli/commands/register.py`, `me.py`): unchanged signature; the URL crosses the wire as plaintext, the server encrypts. No client-side crypto.

## Specific testable acceptance criteria
1. `webhook_endpoints.url_encrypted` exists, NOT NULL — verified by inspector check.
2. `agents.webhook_url` column dropped — verified by inspector check.
3. `webhook_endpoints.url` plaintext column dropped — verified by inspector check.
4. Webhook delivery still works after migration: agent with legacy `webhook_url` still receives events post-upgrade — `test_webhook_delivery_after_migration`.
5. New `register_webhook(url)` writes encrypted blob, never plaintext — `test_create_webhook_endpoint_encrypts_url`.
6. `get_url()` decrypts on read — `test_get_url_decrypts`.
7. `webhook_service` reads via `get_url()` and successfully delivers — `test_webhook_service_reads_encrypted_url`.
8. Decrypt failure on a malformed `url_encrypted` row → endpoint disabled, no exception bubbles — `test_decrypt_failure_disables_endpoint`.
9. Marketplace JSON / `AgentResponse` / `discover_agents` returns NEVER expose `webhook_url` for any agent — `test_marketplace_no_webhook_leak`.
10. Admin view (`_serialize_agent` output) never contains plaintext URL — `test_admin_no_url_render`.
11. Backfill processes BOTH `agents.webhook_url` AND existing `webhook_endpoints.url` legacy plaintext — `test_backfill_covers_both_sources`.
12. Migration upgrade-downgrade-upgrade: clean round-trip — `test_migration_round_trip`.
13. Migration with mid-state NULL `url_encrypted` aborts — `test_migration_aborts_if_backfill_incomplete`.
14. PATCH /v2/me/settings {"webhook_url": "..."} now creates an encrypted endpoint, not a plaintext column — `test_patch_settings_creates_encrypted_endpoint`.
15. Zero new regressions in pre-existing webhook tests vs c7ae822.

## How verified
```
export WEBHOOK_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
pytest tests/test_webhook_url_encryption.py tests/test_webhook_migration.py -v
pytest --cov=sthrip/services/webhook_service --cov=sthrip/db/webhook_endpoint_repo --cov-fail-under=80 \
  tests/test_webhook_url_encryption.py tests/test_webhook_migration.py
pytest tests/test_webhook_service.py tests/test_webhooks.py tests/test_webhook_endpoints.py \
  tests/test_webhook_fanout.py tests/test_webhook_toctou.py tests/test_webhook_encryption.py
pytest tests/ -x --ignore=tests/test_channels.py --ignore=tests/test_mcp_auth.py
```

## GitNexus impact (paste output)

```
mcp__gitnexus__impact({target: "webhook_url", direction: "upstream", repo: "sthrip"})

target: api/schemas.py:AgentRegistration.webhook_url (a request-side property, not a column)
risk: LOW
direct callers: 0 (validators only — kept as boundary SSRF guards)
```

GitNexus indexes property-level webhook_url on the Pydantic schema. The actual columns
(`Agent.webhook_url`, `WebhookEndpoint.url`) and the call-graph were enumerated via grep
across the repo (sthrip/, api/, sdk/, cli/, integrations/). Concrete call-sites:

| # | File | Line | Use | Action |
|---|------|------|-----|--------|
| 1 | `sthrip/db/models.py` | 65 | `Agent.webhook_url = Column(...)` | DROP column |
| 2 | `sthrip/db/models.py` | 650 | `WebhookEndpoint.url = Column(...)` plaintext | DROP, replace with `url_encrypted` |
| 3 | `sthrip/db/models.py` | 665 | `UniqueConstraint("agent_id","url",...)` | DROP |
| 4 | `sthrip/db/agent_repo.py` | 45,61 | `create_agent(webhook_url=...)` writes column | shim → create endpoint |
| 5 | `sthrip/services/agent_registry.py` | 99,124 | passes through to repo | unchanged signature |
| 6 | `sthrip/services/webhook_service.py` | 288–328 | legacy `agent.webhook_url` fallback + `ep.url` reads | replace with `get_url(ep)`; remove legacy fallback |
| 7 | `sthrip/services/webhook_service.py` | 314,430 | `ep.url` for logging/delivery dict | use decrypted url; log endpoint id only |
| 8 | `api/routers/agents.py` | 118 | passes `reg.webhook_url` to registry | unchanged (server-side shim handles) |
| 9 | `api/routers/agents.py` | 507 | `setattr(db_agent, "webhook_url", value)` | replace with endpoint upsert |
| 10 | `api/admin_ui/views.py` | 97 | `agent.webhook_url` rendered in dict | replace with redacted/encrypted indicator |
| 11 | `api/routers/webhook_endpoints.py` | 39,77,154 | `endpoint.url` (owner-only auth-gated) | use `get_url()` for response (owner-only is OK) |
| 12 | tests/* | various | rely on column existing | update fixtures, keep behaviour |

Everything else (`alert_webhook_url`, `monitoring.py:_validated_webhook_url`,
`api/docs.py` examples, CLI/SDK/MCP request bodies) — UNCHANGED. Those are outbound
admin-alert URLs (settings.alert_webhook_url) or wire-format request fields, neither
references the dropped column.

## Out of scope
- onion-relay routing (Sprint 6).
- changing Fernet → AES-GCM (out of scope; reuse `WEBHOOK_ENCRYPTION_KEY`).
- per-endpoint key rotation tooling (post-Sprint 7).
- `agents.webhook_secret` column — out of scope; only the URL graph is in scope this sprint.
