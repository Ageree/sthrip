# Product Spec: Anonymize Sthrip Platform

> Plan to harden Sthrip from "pseudonymous + Monero-on-chain" into a hub
> where a database leak or a Railway subpoena does not yield the payment
> graph or the participants' deanonymizing fingerprints. Branch:
> `feat/anonymity-hardening`. Generator/Evaluator team is recreated
> after each sprint to keep contexts fresh.

---

## Current State

Findings from a read-only pass over `sthrip/db/models.py`,
`sthrip/services/audit_logger.py`, `api/routers/{agents,escrow,wellknown}.py`,
`api/schemas.py`, and `migrations/versions/`.

### What is stored in plaintext today
- **`audit_log.ip_address`** — raw `String(45)` IPv4/IPv6, written by
  `log_event(...)` from `api/deps.py`, `api/main_v2.py`,
  `api/routers/{payments,admin,agents,balance}.py`. The IP is also baked
  into the F-11 HMAC chain link
  (`hash_chain_link(... ip=ip_address ...)` in
  `sthrip/services/audit_logger.py:148` and the migration
  `o6p7q8r9s0t1_audit_hmac_chain.py:73`).
- **`audit_log.request_body`** — arbitrary JSON, only redacted by an
  allowlist of sensitive keys
  (`_SENSITIVE_KEYS = {"api_key","password","secret",...}` in
  `audit_logger.py:71`). Anything not in that set is persisted verbatim,
  including counterparty agent names, deal descriptions, and pricing
  details that the caller passed into `details=...`.
- **`transactions.from_agent_id` / `to_agent_id`** — unencrypted FKs,
  indexed for queries. Joining with `agents.agent_name` reveals the
  full payment graph.
- **`escrow_deals.buyer_id` / `seller_id`** — same shape as transactions,
  with `description`, `amount`, status, and timestamps, indexed by
  participant.
- **`escrow_milestones.description`** — plaintext free-form text per
  milestone (sequence 1..10).
- **`agents.webhook_url`** — `Text NULL` legacy column on the
  `agents` row; still read on every webhook delivery in
  `webhook_service.py:288` (`legacy_url = agent.webhook_url`). The
  `webhook_endpoints` table holds an encrypted `secret_encrypted` but
  the `url` column itself is plaintext `String(2048)`.
- **`agents.description` / `capabilities` / `pricing`** — public
  marketplace fields, no `is_public` gate. Every agent appears in
  `GET /v2/agents/marketplace` and `GET /v2/agents` results regardless
  of the agent's intent (`api/routers/agents.py:182` and
  `sthrip/services/agent_registry.py:197`).
- **`message_relays.from_agent_id` / `to_agent_id`** — unencrypted FKs
  even though ciphertext is encrypted client-side.
- **`/.well-known/agent-payments.json`** — claims `description: "Anonymous
  payments for AI agents"` but exposes only the clearnet Railway URL,
  no `onion_endpoint`.

### What is already good
- F-11 HMAC chain on `audit_log` is in place and well-tested. We must
  keep it valid through the IP-redaction migration.
- `webhook_endpoints.secret_encrypted` is Fernet-encrypted at rest.
- `agents.encryption_public_key` exists for NaCl Box message relay
  (E2E in `message_relays`); ciphertext is never plaintext on the hub.
- `agents.api_key_hash` is HMAC-derived with a separate key
  (`API_KEY_HMAC_SECRET`), distinct from `AUDIT_HMAC_KEY`.

### Public claims that already overshoot reality
`PRIVACY_FEATURES.md` advertises CoinJoin, Submarine Swaps, Tor hidden
service, ZK proofs as "Combined ≤ 3 min, fully unlinkable". None of
the routing path is currently behind Tor; Submarine Swaps and CoinJoin
are research code, not in the request path. `THREAT_MODEL.md` covers
TSS/MPC/bridge concerns from the cross-chain era and never names the
hub-as-deanonymizer threat. The honest re-write of these two
documents is one of the deliverables in Sprint 7.

---

## Architecture Decisions

### AD-1. IP scrubbing: keyed-HMAC with weekly-rotated salt over
                 raw-string deletion
- **Decision:** replace `audit_log.ip_address: VARCHAR(45)` storage with
  a 32-byte HMAC computed over the raw IP using a salt that is rotated
  weekly. Old salts are destroyed after the rotation. Column is
  renamed to `ip_hmac` and the chain link uses the HMAC, not the raw
  string. Salt rotation is a separate `ip_salts` table with
  `(salt_id, secret, created_at, retired_at)` and a cron that retires
  >7-day-old rows.
- **Why HMAC over delete:** rate-limiting and abuse forensics need the
  ability to say "the same IP submitted N requests in window W". A
  plain hash leaks via rainbow table on the 4.3 B IPv4 space; HMAC
  with a rotating salt is forward-secret after rotation while
  preserving short-window correlation for abuse detection.
- **Trade-off accepted:** within a salt window (≤7 d) two requests
  from the same IP collide; outside the window they do not. This
  matches "abuse detection yes, long-tail forensics no".

### AD-2. Payment graph: encryption-at-rest with envelope keys split
                 between hub and operator HSM
- **Decision:** add a `participant_envelope` column to `transactions`,
  `escrow_deals`, `escrow_milestones`, and `message_relays`. The
  envelope contains `{from_agent_id, to_agent_id, amount, description}`
  encrypted with AES-256-GCM under a per-row data-encryption-key (DEK).
  The DEK is itself encrypted twice — once with the hub's KEK and once
  with the operator KEK. Reading any participant in plaintext
  requires both KEKs.
- **Why envelope over blinded tokens:** the hub *must* see participants
  at routing time (the system is custodial). True blinded tokens would
  require redesigning routing as MPC, which is the
  out-of-scope CoinJoin/MPC roadmap item. Envelope encryption gives
  the achievable property "ADMIN_API_KEY alone cannot decrypt the
  graph; runtime memory still sees the plan during a single request".
- **Plaintext FK columns become nullable, then drop in Sprint 4.** Until
  drop, dual-write keeps the existing query plans working. We
  deliberately accept N+1 on read-paths during the migration window
  because all hot reads go through `*_repo.py` modules we control.

### AD-3. .onion endpoint: Railway sidecar tor container
- **Decision:** add a Tor sidecar service in
  `railway/tor-sidecar-deploy/` that runs `tor` with a hidden service
  v3 mapping `:80 → sthrip-api.railway.internal:8000`. Persistent
  hidden-service keys live in a Railway-mounted volume.
- **Why sidecar over separate VPS:** keeps secrets in the same
  Railway project (single billing/IAM blast radius), reuses the
  private network for the upstream, and avoids a second deploy
  pipeline. The tor process never touches the public internet's
  inbound side — it only descends to the rendezvous network.
- **Trade-off accepted:** Railway CDN egress is observable to Railway
  (already a constraint of running on Railway). Mitigation is
  documented in `THREAT_MODEL.md` rather than re-architected.

### AD-4. Marketplace visibility: opt-in `is_public` flag with safe
                 default
- **Decision:** `agents.is_public BOOLEAN NOT NULL DEFAULT false`
  added by migration. `discover_agents` and the
  `GET /v2/agents/marketplace` handler filter on `is_public=true`.
  `GET /v2/agents/{name}` returns 404 if the agent is not public,
  preserving discovery-by-guess resistance.
- **Why opt-in over whitelist:** whitelist requires admin curation
  and is a centralization point. Opt-in keeps onboarding self-service.
- **Defaults zeroed:** `description`, `pricing` default to NULL/empty,
  `capabilities` to `[]`. SDK calls `update_profile` with explicit
  values when the agent intends to be visible.
- **Legacy rows:** the migration leaves `is_public=false` for all
  existing agents — they vanish from marketplace until they
  explicitly opt back in. This is a behaviour break that we surface
  in `PRIVACY_FEATURES.md`'s honest roadmap.

### AD-5. Webhook URL: dedicated encrypted table, drop legacy column
                 in two phases
- **Decision:** remove `agents.webhook_url`. Move all webhook URLs into
  `webhook_endpoints` (already exists, already encrypts the secret).
  Add `webhook_endpoints.url_encrypted TEXT NOT NULL`, drop the plain
  `url` column. Marketplace JSON never includes URLs.
- **Optional onion relay (Sprint 6):** when an agent's `url_encrypted`
  decodes to an `.onion` host, the hub routes the outbound webhook
  through the Tor SOCKS5 proxy embedded in the sidecar. SDK fetches
  on the agent side use `httpx.AsyncClient(proxy="socks5h://...:9050")`
  via opt-in.
- **Legacy migration:** Sprint 5 ships dual-read (plain `url` OR
  `url_encrypted`). Sprint 5b cuts over and drops the plain column.
- **Trade-off accepted:** Tor adds 0.5–3 s to webhook latency. The
  hub still queues retries; SLAs that promise sub-second delivery
  remain on clearnet `https://`.

### AD-6. Backwards compatibility for accumulated audit_log rows
- **Decision:** in Sprint 1 the migration replaces `ip_address` with
  `ip_hmac` *and rebackfills the chain*. The migration uses each row's
  original IP to compute the HMAC under a stable bootstrap salt
  (`ip_salt_bootstrap`), then computes a new `entry_hmac` for that
  row using the HMAC value in the same chain-link slot the raw IP
  occupied. After backfill the bootstrap salt is destroyed.
- **Why rebackfill over chain split:** preserves continuous chain
  verification. The alternative — keeping the old `ip_address` field
  in the chain hash forever — would force us to keep raw IPs forever,
  defeating the goal.
- **Cost accepted:** the backfill is a one-shot maintenance window
  proportional to `audit_log` size (~bounded; we know this is small
  on prod today). Migration is wrapped in an advisory lock so no
  writer races us.

---

## Data Flow Diagrams

### Audit-log write — before
```
HTTP request                    audit_log row
─────────────                  ─────────────────────────
client_ip = "203.0.113.42" ──► ip_address = "203.0.113.42"
request_body = {...}        ──► request_body = {raw}     ─►HMAC chain
agent_id = ABC              ──► agent_id = ABC
```

### Audit-log write — after (Sprint 1)
```
HTTP request                    audit_log row
─────────────                  ─────────────────────────
client_ip = "203.0.113.42"     salt = current_ip_salt() (rotated weekly)
                             │
                             ▼
                          ip_hmac = HMAC(salt, ip)
                                                          ─►HMAC chain
request_body = {...}        ──► allowlist filter ──►
                                request_body = {agent_name?, action_type?}
```

### Payment write — before
```
client → POST /v2/payments/hub-routing
   │
   ▼
transactions row: from_agent_id=A, to_agent_id=B, amount=100 XMR
   ▲
   └── readable by anyone with ADMIN_API_KEY
```

### Payment write — after (Sprint 3 dual-write, Sprint 4 cutover)
```
client → POST /v2/payments/hub-routing
   │
   ▼
DEK = random_32_bytes()
envelope = AES-GCM(DEK, {from, to, amount, memo})
DEK_hub = AES-GCM(KEK_hub, DEK)
DEK_op  = AES-GCM(KEK_op,  DEK)
   │
   ▼
transactions row:
  from_agent_id=NULL  to_agent_id=NULL  amount=NULL
  participant_envelope = envelope || DEK_hub || DEK_op
  amount_redacted_bucket = "100-1000 XMR"  (for accounting cron only)

   ▲
   └── ADMIN_API_KEY alone cannot decrypt; needs operator KEK too
```

### Marketplace browse — before vs after
```
BEFORE                                AFTER (Sprint 2)
──────────                            ────────────────
GET /v2/agents/marketplace            GET /v2/agents/marketplace
  ├── all is_active agents              ├── filter agents.is_public=true
  ├── webhook_url leaked? no              ├── webhook_url column removed
  └── pricing/desc always shown         └── pricing/desc only if explicitly
                                            set after is_public flip
```

### Webhook delivery — after Sprint 6
```
agent registers .onion url ──► webhook_endpoints.url_encrypted (Fernet)
                                                │
event fires ──► webhook_service                 │
                  │                             ▼
                  ├── decrypt url
                  ├── if endswith(".onion") ─►  httpx via SOCKS5 → Tor sidecar
                  └── else                  ─►  httpx clearnet
```

---

## Sprints

> 4–7 sprints suggested; we land on 7 because each maps to a single
> reviewable PR with one migration and one set of tests. Quick wins
> first, large schema changes split, infra deploy isolated, doc
> rewrite last.

### Sprint 1: Audit-log IP scrubbing + request_body allowlist
- **Цель:** raw IPs disappear from `audit_log`; `request_body` only
  contains explicitly-allowed keys; HMAC chain stays valid.
- **Скоп:**
  - Add `ip_salts` table + rotation cron (weekly).
  - Rename `audit_log.ip_address → ip_hmac`, recompute HMACs in chain
    using bootstrap salt.
  - Replace `_SENSITIVE_KEYS` redaction with per-action allowlist
    map: `{action: {allowed_keys}}`. Default deny.
  - Update all `audit_log(...)` call-sites in
    `api/routers/{payments,admin,agents,balance}.py`,
    `api/deps.py`, `api/main_v2.py` to pass already-allowlisted dicts.
  - Update `_hash_chain_link(... ip=...)` callers to feed `ip_hmac`.
- **Файлы:** `sthrip/db/models.py`, `sthrip/services/audit_logger.py`,
  `migrations/versions/q8r9s0t1u2v3_audit_ip_hmac.py`,
  `sthrip/services/ip_salt_service.py` (new),
  `api/routers/*.py` (allowlist updates).
- **Миграция:** да — `q8r9s0t1u2v3_audit_ip_hmac.py`.
- **Тесты:**
  - chain remains verifiable after migration (`verify_chain` returns
    `ok=True` on backfilled rows);
  - same IP within window → same hmac; across rotation → different;
  - allowlist drops disallowed keys without raising;
  - existing 2221 tests stay green (run after).
- **Risk:** HIGH — `audit_log` is on the hot path of every authed
  request. `gitnexus_impact({target: "log_event"})` will show ~30
  call-sites. Mitigation: keep `log_event` signature stable and do
  the redaction inside it; only call-sites need to update the
  per-action allowlist map.

### Sprint 2: Marketplace opt-in (`is_public`) + zero-default profile fields
- **Цель:** marketplace shows only agents who have explicitly opted in.
  Default registration leaves description/pricing/capabilities blank.
- **Скоп:**
  - Add `agents.is_public BOOLEAN NOT NULL DEFAULT false`.
  - Migrate existing rows to `false` (intentional behaviour break).
  - Filter `discover_agents`, `marketplace`, `get_profile`,
    `get_profile_by_address` to require `is_public=true` for non-self
    callers.
  - Schema defaults: `description=None`, `pricing={}`,
    `capabilities=[]` in `AgentRegistration`.
  - SDK `update_profile(is_public=True)` flips the flag; CLI
    `sthrip agent publish` does the same.
- **Файлы:** `sthrip/db/models.py`,
  `sthrip/services/agent_registry.py`,
  `api/routers/agents.py` (3 endpoints),
  `migrations/versions/r9s0t1u2v3w4_agent_is_public.py`,
  `api/schemas.py`, `cli/agent_cli/commands/agent.py`.
- **Миграция:** да — `r9s0t1u2v3w4_agent_is_public.py`.
- **Тесты:**
  - default-registered agent does not appear in marketplace;
  - flipping `is_public` makes them visible;
  - direct profile lookup of a non-public agent → 404 for
    other agents, 200 for self;
  - existing tests that registered agents and asserted marketplace
    appearance need updates — count those and add the explicit
    `update_profile(is_public=True)` step.
- **Risk:** MEDIUM — silent regression in clients that scrape
  marketplace. SDK CHANGELOG entry required. `gitnexus_impact` on
  `discover_agents` and `marketplace` to enumerate fixture updates.

### Sprint 3: Encrypted payment-graph schema + dual-write
- **Цель:** every new transaction/escrow/milestone row carries a
  participant envelope; old plaintext FKs are still written for
  read-path compatibility.
- **Скоп:**
  - New table `crypto_keys` storing wrapped `KEK_hub`,
    `KEK_op` references (operator KEK is HSM-resident; hub stores
    only the wrapped DEKs).
  - Add `participant_envelope BYTEA NULL` column to `transactions`,
    `escrow_deals`, `escrow_milestones`, `message_relays`.
  - Add `amount_bucket TEXT NULL` for accounting cron (no exact
    amount — coarsened to log-bucket like `"10-100 XMR"`).
  - Service-layer wrappers `encrypt_envelope` / `decrypt_envelope`
    in `sthrip/services/envelope_crypto.py`.
  - Dual-write at every insert site
    (`transaction_repo.create`, `escrow_repo.create`, etc).
- **Файлы:** new `sthrip/services/envelope_crypto.py`,
  `sthrip/db/{models,transaction_repo,escrow_repo,milestone_repo}.py`,
  `migrations/versions/s0t1u2v3w4x5_payment_envelope.py`.
- **Миграция:** да — schema-only, no backfill of old rows
  (envelope is added forward-only; old rows keep plaintext, which
  Sprint 4 handles).
- **Тесты:**
  - new rows have non-null `participant_envelope`;
  - decrypt with both KEKs returns original;
  - decrypt with one KEK fails;
  - reads still work because old code-path FKs are populated.
- **Risk:** HIGH — touching every payment write. `gitnexus_impact` on
  `Transaction`, `EscrowDeal`, `EscrowMilestone` will show admin
  views, MCP tools, and aggregations. Mitigation: hide envelope
  behind repo, tests at the repo level.

### Sprint 4: Switch reads to envelope, drop plaintext FKs (cutover)
- **Цель:** all read paths use envelope as the source of truth.
  Plaintext FK columns become nullable then drop. ADMIN_API_KEY
  alone no longer reads the graph.
- **Скоп:**
  - One-shot backfill cron: read each old row, generate envelope,
    write back, clear plaintext FK and amount.
  - Read-path: `transaction_repo.get_for_agent(agent_id)` first
    decrypts envelope, then matches.
  - Admin views (`api/admin_ui/views.py`) gain a "decrypt with
    operator KEK" prompt; without operator KEK they show
    `participant=encrypted, amount=bucket`.
  - Drop `from_agent_id`, `to_agent_id`, `buyer_id`, `seller_id`,
    `amount` columns (with `IF EXISTS`).
- **Файлы:** all repos under `sthrip/db/`, `api/admin_ui/views.py`,
  `migrations/versions/t1u2v3w4x5y6_payment_envelope_cutover.py`,
  `scripts/backfill_payment_envelope.py`.
- **Миграция:** да — `t1u2v3w4x5y6_payment_envelope_cutover.py`,
  destructive on FK columns (after backfill verified).
- **Тесты:**
  - admin endpoint without operator KEK returns redacted view;
  - admin endpoint with operator KEK returns full participants;
  - SDK queries return correct payments for the calling agent;
  - all 2221 existing tests + ~30 new envelope tests green.
- **Risk:** CRITICAL — destructive migration on prod data. Mandatory
  staging dry-run. Backfill is idempotent and rerun-safe; the column
  drop is the point of no return. Document rollback as
  "restore from backup" in the spec contract.

### Sprint 5: webhook_url removal + onion relay opt-in
- **Цель:** `agents.webhook_url` column gone; all webhook URLs
  encrypted in `webhook_endpoints.url_encrypted`. Optional Tor relay
  ready when sidecar lands in Sprint 6.
- **Скоп:**
  - Add `webhook_endpoints.url_encrypted TEXT NOT NULL` (Fernet,
    same key as `secret_encrypted`).
  - Backfill from `agents.webhook_url` into a per-agent endpoint,
    encrypt, then null the column.
  - Drop `agents.webhook_url` (Sprint 5b separate PR if review wants
    a safety gap; Generator may inline if review allows).
  - `webhook_service.py` only reads from `webhook_endpoints`; the
    legacy fallback at line 288 removed.
  - SDK `register_webhook(url)` already encrypts; verify path.
- **Файлы:** `sthrip/db/models.py`,
  `sthrip/services/webhook_service.py`,
  `sthrip/db/webhook_endpoint_repo.py`,
  `migrations/versions/u2v3w4x5y6z7_drop_legacy_webhook_url.py`.
- **Миграция:** да — backfill + drop column.
- **Тесты:**
  - existing webhooks fire after migration;
  - admin view never includes URL;
  - SDK end-to-end webhook test green;
  - decrypt failure on a malformed row falls back to disabling the
    endpoint (already in `webhook_service.py` but exercise it).
- **Risk:** MEDIUM — `gitnexus_impact({target: "webhook_url"})`
  shows ~10 sites including admin UI; one of them
  (`api/admin_ui/views.py:52`) actively renders the URL.

### Sprint 6: Tor `.onion` sidecar deploy + discovery JSON update + SDK SOCKS5 support
- **Цель:** `https://<onion>.onion/v2/...` resolves and serves the
  same API; the discovery JSON advertises it; SDK can connect over
  Tor.
- **Скоп:**
  - `railway/tor-sidecar-deploy/` Dockerfile + `torrc`. Persistent
    volume for hidden-service v3 keys.
  - Wire upstream to `sthrip-api.railway.internal:8000`.
  - `wellknown.py`: add `"onion_endpoint": "<computed-onion>.onion"`
    sourced from env var `STHRIP_ONION_ENDPOINT` so the JSON stays
    static and cache-friendly.
  - SDK `Client(use_tor=True)` opens `httpx.AsyncClient(transport=
    httpx.AsyncHTTPTransport(proxy="socks5h://127.0.0.1:9050"))`.
  - Webhook outbound through SOCKS5 when target host endswith
    `.onion`.
- **Файлы:** new `railway/tor-sidecar-deploy/{Dockerfile,torrc,
  README.md}`, `api/routers/wellknown.py`, `sdk/python/sthrip/client.py`.
- **Миграция:** нет (infra-only).
- **Тесты:**
  - integration test against the sidecar in CI is out of scope;
    add a unit test that asserts `/.well-known/agent-payments.json`
    surfaces `onion_endpoint` when env var is set;
  - SDK unit test that `use_tor=True` configures SOCKS5 transport;
  - manual smoke test in staging documented in the sprint contract.
- **Risk:** LOW for code (no DB), MEDIUM for infra (new service,
  persistent volume, key custody runbook).

### Sprint 7: Honest `PRIVACY_FEATURES.md` rewrite + new `THREAT_MODEL.md`
- **Цель:** docs match reality; threat model lists 8+ scenarios
  with current defence + residual risk per AD-1..AD-5.
- **Скоп:**
  - `PRIVACY_FEATURES.md`: split into "shipped" (audit scrubbing,
    encrypted graph, marketplace opt-in, onion endpoint, encrypted
    webhook URLs) and "roadmap" (CoinJoin, Submarine Swaps,
    zk-SNARKs, MPC routing).
  - Replace `docs/THREAT_MODEL.md` (currently MPC/bridge era) with a
    table covering: external blockchain analyzer, marketplace
    scraper, Railway subpoena, leaked `ADMIN_API_KEY`, webhook
    correlation attacker, on-path network observer, runtime hub
    compromise, malicious insider operator. Each row: threat,
    current defence (with sprint reference), residual risk.
  - Update `README.md` privacy bullets to point to the new files.
- **Файлы:** `PRIVACY_FEATURES.md`, `docs/THREAT_MODEL.md`,
  `README.md`.
- **Миграция:** нет.
- **Тесты:** markdown-lint (project already has it via the docs
  pipeline) + reviewer pass.
- **Risk:** LOW.

---

## Edge Cases

- **HMAC chain integrity at Sprint 1 boundary.** The migration must
  rebackfill `entry_hmac` row-by-row using `ip_hmac` instead of
  `ip_address` *and* must clear the bootstrap salt afterward.
  `verify_chain` is run as the last step of the migration; failure
  raises and aborts the deploy.
- **Migrating accumulated audit_log rows.** Hashing IP under a
  bootstrap salt that is then destroyed gives forward secrecy on
  legacy rows while keeping the chain valid. We do **not** keep
  raw IPs around for a transitional period — the migration window is
  the only window in which raw IPs touch disk after this sprint.
- **KEK rotation.** When `KEK_hub` rotates (Sprint 4+) old envelopes
  are still readable because each row's wrapped DEK was encrypted
  under the rotated key only at write time. Rotation requires a
  rewrap pass: read every envelope, decrypt DEK with old KEK, encrypt
  DEK with new KEK, update row. Document this in
  `docs/KEK_ROTATION.md` (deferred to a post-Sprint-7 ops PR).
- **Encryption-key loss.** If `KEK_op` is lost without a backup, the
  graph is permanently unreadable. Sprint 3 contract mandates
  HSM with shamir-shared backup before launch.
- **Tor latency on webhook callbacks.** Default httpx timeout 10 s
  raised to 30 s for `.onion` targets only. Retry policy already
  exists in `WebhookService`.
- **Marketplace legacy agents with unset `is_public`.** Migration
  forces `is_public=false`. Operators get an email: "your agent
  removed from public marketplace; flip `is_public=true` to opt
  back in".
- **Allowlist drift.** A new endpoint that calls `audit_log` with
  unrecognised details keys gets nothing recorded for those keys
  (default deny). Catch this with a unit test that registers
  expected per-action allowlist coverage and fails if a new action
  ships without one.
- **Onion endpoint key rotation.** The hidden-service v3 private
  key lives on the Railway volume. Rotating it changes the .onion
  address, which forces SDK clients to refresh
  `/.well-known/agent-payments.json`. Document a 14-day grace
  period during which both clearnet and onion serve.

---

## Out of Scope

Repeated from `user-criteria.md` so contracts inherit the boundary:

- ZK proofs / zk-SNARKs (research, not in this branch).
- CoinJoin / Submarine Swaps mixing.
- MPC-based mixing without coordinator.
- Full implementation of every roadmap item in `PRIVACY_FEATURES.md`
  — Sprint 7 honestly demotes them to "future work".
- Smart-contract privacy on the bridge side (separate repo, Slither
  audit pending).

---

## Open Questions for Lead

1. **Operator KEK custody.** Where does the operator KEK live in
   prod? Options: (a) Railway service variable on a separate
   privileged service the API never reaches; (b) external HSM
   (e.g. YubiHSM) plugged into the operator workstation, only
   present during privileged ops; (c) Shamir-shared with three
   operators. AD-2 assumes (b) but Sprint 4 cutover blocks until
   one is chosen.
2. **Salt rotation cadence.** AC says "не реже раза в неделю". Is
   weekly the right cadence, or do we want daily? Daily increases
   abuse-detection blind spots (a single IP flooding across days
   becomes invisible) but shrinks the window of correlatable
   activity inside a leaked snapshot. **Recommendation: weekly
   with a config flag for testing daily.**
3. **Marketplace migration to `is_public=false`.** Hard cut on
   migration day, or 30-day grace where existing agents stay
   visible while we email them? Hard cut respects the threat model
   strictly; grace respects the live ecosystem.
4. **Tor sidecar inbound only, or also outbound for clearnet
   webhooks?** Routing all hub-outbound traffic through Tor would
   hide hub→agent IP correlation but doubles average latency for
   agents on clearnet. **Recommendation: outbound Tor only when
   target is `.onion`.**
5. **`MessageRelay` ciphertext is fine, but `from_agent_id` /
   `to_agent_id` are still plaintext FKs.** Include them in the
   payment-graph envelope (AD-2) or treat the relay table
   separately because messages are ephemeral (TTL 24 h)? **Lean:
   include them — same migration window, same key schedule, no
   need for a separate model.**
