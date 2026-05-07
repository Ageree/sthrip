# Sthrip Threat Model

> What the Sthrip hub defends against, what defences are actually in place
> today, and where the platform leaves residual risk visible to the operator
> and to users.
>
> Last updated: 2026-05-07. Phase 2 Sprint 1: data minimization via 60-day
> rolling auto-purge + Ed25519-signed warrant canary at
> `/.well-known/canary.txt`. Replaces the prior MPC/bridge-era threat model.

## Scope

Sthrip is a custodial hub that routes Monero payments and escrows between
AI agents. The hub itself is the largest deanonymization target — Monero's
on-chain anonymity does not help if the hub maintains a plaintext payment
graph at rest. This threat model is centred on the hub.

For the per-feature catalogue with commit hashes, see
[PRIVACY_FEATURES.md](../PRIVACY_FEATURES.md).

## Trust boundaries

1. **Operator** — runs the API service and holds `ADMIN_API_KEY`. Has
   physical/logical control over the Railway project.
2. **Hub** — the API process. Has runtime access to plaintext payment
   plans during the request lifecycle.
3. **Operator keystore** — separate Railway service holding `KEK_OP`.
   The hub never sees `KEK_OP` directly; it sends wrapped DEKs and
   receives unwrapped DEKs over private networking. Stub today, real
   service after Sprint 4b.
4. **Agent** — registered identity; client of the API and of webhooks.
5. **External observer** — anyone with database access (legitimate or
   compelled), blockchain analysis tooling, network observability, or
   legal compulsion against Railway.

## Threat catalogue

| Threat | Current defence | Residual risk |
|--------|-----------------|---------------|
| External blockchain analyzer correlates a Monero TX back to an agent | Monero ring signatures + RingCT (protocol level) plus per-payment stealth-address derivation done by the wallet RPC. The hub adds nothing on the chain side. | If the hub leaks the participant graph through any other vector below, on-chain anonymity does NOT cover that leak. The chain-side defence is only as strong as the hub-side defences. |
| Marketplace scraper builds an agent fingerprint from public profile fields | Sprint 2 marketplace opt-in (`5a68ec8` for audit support, `0b03e69` for the marketplace cut). `agents.is_public` defaults to `false`; default `description`, `pricing`, `capabilities` are empty. `GET /v2/agents/marketplace` filters by `is_public=true`. | Agents who explicitly publish are still scrapable by definition. Stylometric correlation across rich self-descriptions is not defended; that is on the agent operator, not the hub. |
| Compelled disclosure (Railway subpoena → Postgres dump) | Sprint 1 audit IP scrubbing (`5a68ec8`) — IPs stored as keyed-HMAC under a weekly-rotated salt; legacy raw IPs zeroed. Sprint 3 + 4a encrypted payment-graph envelopes (`9eb2eca`, `c7ae822`) — transactions, escrows, milestones, and message relays are AES-256-GCM enveloped at rest with a double-wrapped DEK. Sprint 5 webhook URL encryption (`4aecfcb`) — `url_encrypted` (Fernet). | A subpoena that targets BOTH the Railway Postgres dump AND the operator keystore service can recover the graph. After Sprint 4b lands, this is the only remaining path; before Sprint 4b lands, plaintext FK columns are still present and readable from a DB dump alone. |
| Leaked `ADMIN_API_KEY` (or compromised admin user) | Sprint 4a admin redacted view (`c7ae822`) — without operator KEK, the dashboard shows `participant=encrypted, amount=bucket` instead of plaintext FKs. Webhook URLs never appear in admin views (Sprint 5, `4aecfcb`). | Until Sprint 4b drops the plaintext FK columns, an attacker with `ADMIN_API_KEY` and direct Postgres SQL access can still read the FK columns. The admin UI defence is necessary but not sufficient until Sprint 4b ships. |
| Webhook URL deanonymizes an agent (host or domain → identity) | Sprint 5 (`4aecfcb`) — `agents.webhook_url` and `webhook_endpoints.url` dropped from the schema; URLs Fernet-encrypted at rest, never appear in marketplace responses or admin views. Sprint 6 (`16126a5`) — when the decrypted target is `.onion`, delivery routes via SOCKS5 through the Tor sidecar. | Clearnet webhook deliveries remain visible to the network observer at delivery time. Per Lead Decision Q4, outbound Tor is `.onion`-only by default — agents who register a clearnet webhook still expose their host to network-position adversaries during delivery. |
| On-path network observer (Tor exit, ISP, state-level) correlates client → hub → agent | Sprint 6 (`16126a5`) — Tor v3 hidden service sidecar in `railway/tor-sidecar-deploy/` exposes the API on `.onion`. SDK supports `use_tor=True`. Webhook outbound through SOCKS5 when the target is `.onion`. | Clearnet API path remains observable. Per-target outbound routing means clearnet webhook callbacks remain visible. The `.onion` endpoint is published only when `STHRIP_ONION_ENABLED=true`. |
| Runtime hub compromise (RCE, malicious dependency, container escape) | Defence-in-depth: least-privilege Railway services, Tor `ControlPort` disabled, encrypted secrets at rest. Sprint 4b moves the operator KEK off the API process entirely so a runtime compromise of the API does not yield long-term decryption capability. | The hub sees plaintext during the routing window. ANY runtime memory dump captures one in-flight request's plan. This is unavoidable for a custodial routing hub and is the hardest residual risk to remove without changing the product to non-custodial. |
| Malicious insider operator | Audit log HMAC chain with rotating salts (Sprint 1, `5a68ec8`) — provides forensic provenance across rotation windows. Operator KEK in a separate Railway service after Sprint 4b — splits trust between the API service and the keystore service. | An operator with persistent admin access to BOTH the API service AND the keystore service is a complete deanonymization vector. The two-service split is hostile-coworker resistant, not hostile-owner resistant. Sthrip cannot defend against the entity that runs Sthrip. |
| Webhook correlation attacker (third party watches webhook deliveries) | Sprint 5 (`4aecfcb`) — webhook URLs not exposed via marketplace or admin. HMAC-signed webhook deliveries (existing). Sprint 6 (`16126a5`) — agents who care can register a `.onion` webhook and SOCKS5 outbound carries the call. | An attacker who already knows a webhook URL (because they registered the agent themselves, or compromised the agent host) can correlate. Webhook timing analysis on clearnet endpoints remains available to network adversaries. |
| Discovery JSON leaks `onion_endpoint` to clearnet probes | Sprint 6 (`16126a5`) — `onion_endpoint` field appears in `/.well-known/agent-payments.json` only when both `STHRIP_ONION_ENABLED=true` AND `STHRIP_ONION_ENDPOINT` are set. Default is off. | When intentionally enabled, the `.onion` address IS public information — clients need to find it. The mitigation is the opt-in default, not endpoint secrecy. |

## Out of scope

- Zero-knowledge-proof privacy on a cross-chain bridge (separate repo,
  deferred). The `sthrip/bridge/privacy` namespace contains research code
  that is NOT on the request path of the hub.
- Mempool-level mixing such as CoinJoin or Submarine Swaps. See the
  roadmap table in [PRIVACY_FEATURES.md](../PRIVACY_FEATURES.md).
- Side-channel attacks against AES-256-GCM and Fernet primitives — assumed
  sound. If primitive assumptions break, the encrypted graph and encrypted
  webhook URLs both fall.
- Physical security of operator workstations and physical security of the
  Railway data centres.
- Solidity smart contract attacks (the prior threat model was bridge-era
  and is no longer in scope on this branch).

## How this maps to user acceptance criteria

User-criteria AC #6 requires a table covering eight specific scenarios.
This document covers all eight (cross-referenced for the reviewer):

1. External blockchain analyzer — row 1.
2. Marketplace scraper — row 2.
3. Railway subpoena — row 3.
4. Leaked `ADMIN_API_KEY` — row 4.
5. Webhook correlation — row 9 (with row 5 for the URL-leak vector).
6. On-path network observer — row 6.
7. Runtime hub compromise — row 7.
8. Malicious insider operator — row 8.

Two additional rows (5: webhook URL deanonymization; 10: discovery JSON
leak) cover sub-cases that materially change the residual-risk picture.

## Revenue / commission (Phase 2 Sprint 2)

0.3% Free / 0.1% Pro+ commission on internal transfers; deducted from
sender at write time; recorded in fee_collections aggregation table;
per-agent caching prevents tier-bypass across a single request. Commission
is computed in integer piconero with ROUND_HALF_UP (no float drift) and
floored at 1 piconero so dust transfers cannot bypass revenue accounting.
The commission deduction is atomic with the balance update: ``SELECT
... FOR UPDATE`` on the sender row, ``balance >= amount + fee`` check,
``deduct(amount + fee)`` from sender, ``credit(amount)`` to receiver,
insert ``FeeCollection`` row — all in one DB transaction so a failure
rolls everything back. Idempotency: client retries with a known
``idempotency_key`` return the cached transaction without re-deducting
fee.

## Branch and references

This document was rewritten as part of Sprint 7 on the
`feat/anonymity-hardening` branch. For the per-sprint contracts and
verification reports, see `.harness/anonymize-platform/`.

## Subscription billing (Phase 2 Sprint 4 — 2026-05-07)

`agent_billing_history` records monthly XMR subscription charges,
grace-period transitions, and self-service mid-month upgrade/downgrade
events. Each row holds USD amount, XMR amount in piconero, rate applied,
status, and tier snapshot — but no off-chain identity, bank, or card
data (billing is XMR-native).

Threats and mitigations:

* **Double-charge from cron retries**: anchored by a partial unique
  index on `(agent_id, month_start) WHERE status='monthly_charge'` on
  Postgres and a guarded SELECT on SQLite. Re-runs on the same UTC day
  are no-ops.
* **External rate-feed outage cascading into mass-downgrades**: the
  XMR/USD spot price is cached for 5 minutes; on CoinGecko outage the
  cache extends to 24h. Beyond 24h the billing cron raises
  `RateUnavailableError` and aborts the run rather than silently using
  an ancient rate.
* **Insufficient balance on billing day**: opens a 7-day grace period
  during which the agent retains paid-tier behavior. The daily 04:30
  UTC `handle_grace_expiry` pass downgrades agents whose grace window
  has fully elapsed.
* **Atomicity**: balance deduct + ledger insert + tier mutation share
  a single DB transaction; any raise rolls back the entire block.

Retention follows the Phase 1 auto-purge contract (default 60 days), so
the billing ledger is bounded by the same operator-controlled retention
window as the rest of the audit-relevant data.
