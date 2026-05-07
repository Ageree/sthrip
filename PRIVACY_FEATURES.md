# Sthrip Privacy Features

> Honest catalog of what is shipped on the `feat/anonymity-hardening` branch
> versus what is planned. No marketing decoration of unshipped work.
>
> Last updated: 2026-05-06 (after Sprint 6 commit `16126a5`).

## How to read this file

Sthrip is a custodial Monero payment hub for AI agents. Monero's on-chain
anonymity is real, but a custodial hub is the dominant deanonymization vector
for everyone who uses it — if the hub keeps a plaintext payment graph, an
attacker who reaches the hub gets the graph regardless of how the chain
behaves. The "Shipped" list below is everything in the hub itself that
defends against that.

For the threat model and residual risks, see
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

## Shipped

### Sprint 1 — Audit log IP scrubbing + `request_body` allowlist

- **Commit**: `5a68ec8`
- `audit_log.ip_address` removed; replaced by `ip_hmac` (32-byte HMAC under
  a weekly-rotated keyed salt). Old salts are zeroed after
  `2 × IP_SALT_ROTATION_DAYS`. Rotation cadence configurable via
  `IP_SALT_ROTATION_DAYS` (default `7`, range `1..30`).
- `audit_log.request_body` is filtered through a per-action allowlist of
  field names — unknown actions default-deny.
- The HMAC integrity chain across audit rows remains valid; the migration
  aborts on any chain break instead of silently breaking forensics.

### Sprint 2 — Marketplace opt-in (`is_public`)

- **Commit**: `0b03e69`
- `agents.is_public` defaults to `false`. The `GET /v2/agents/marketplace`
  endpoint serves only `is_public=true` rows.
- Default `description`, `pricing`, and `capabilities` are empty so an
  agent that does nothing leaks no stylometry on registration.
- Existing rows hard-cut to `is_public=false` on migration. Operators who
  want to remain discoverable opt in via SDK `update_profile(is_public=True)`.

### Sprint 3 — Encrypted payment-graph envelopes (dual-write)

- **Commit**: `9eb2eca`
- `transactions`, `escrow_deals`, `escrow_milestones`, and `message_relays`
  gain an envelope column populated on write: AES-256-GCM ciphertext over a
  msgpack v1 payload, with a per-row DEK wrapped twice — once with the
  hub's KEK and once with the operator KEK delivered through the keystore
  service.
- Plaintext FK columns (`from_agent_id`, `to_agent_id`, `buyer_id`,
  `seller_id`, `amount`) are still written in this sprint — dual-write
  only — so reads remain compatible. The amount field also writes a
  log-scale `amount_bucket` so admin views can show coarse aggregates
  without unwrapping the operator KEK.
- The keystore service exists as a stub in this sprint (identity unwrap);
  the production deployment is part of Sprint 4b (see "In progress" below).

### Sprint 4a — Dual-read with feature flag + backfill

- **Commit**: `c7ae822`
- Read paths try the envelope first and fall back to plaintext FKs only if
  the envelope is `NULL` or fails to decrypt. Gated behind
  `STHRIP_READ_FROM_ENVELOPE` (default `false`).
- `scripts/backfill_payment_envelope.py` populates the envelope on rows
  written before Sprint 3. Rerun-safe — skips rows where envelope is
  already present.
- Admin dashboard (`api/admin_ui/views.py`) gains a redacted view: when
  the operator KEK is unavailable to the API process, the dashboard shows
  `participant=encrypted, amount=bucket` instead of the plaintext FKs.
- Net effect today: with the flag off in production, behaviour is
  unchanged. With the flag on, an admin holding only `ADMIN_API_KEY`
  cannot see the participant graph through the dashboard — the operator
  keystore service is required.

### Sprint 5 — Encrypted webhook URLs

- **Commit**: `4aecfcb`
- `agents.webhook_url` and `webhook_endpoints.url` columns dropped.
  Replaced by `url_encrypted` (Fernet at rest).
- Marketplace and admin views never expose webhook URLs.
- Migration is 7-phase and idempotent. The plaintext column is dropped
  only after the encrypted column is populated and verified.

### Sprint 6 — Tor `.onion` sidecar + SDK SOCKS5 + per-target webhook routing

- **Commit**: `16126a5`
- New Railway sidecar `railway/tor-sidecar-deploy/` runs a Tor v3 hidden
  service mapping `:80 → api:8000`. Persistent volume holds the onion
  private key; `ControlPort` disabled.
- `/.well-known/agent-payments.json` publishes `onion_endpoint` ONLY when
  both `STHRIP_ONION_ENABLED=true` AND `STHRIP_ONION_ENDPOINT` are set.
  The default is off.
- SDK supports `Sthrip(use_tor=True)` for inbound calls via the SOCKS5
  proxy bundled with the sidecar.
- Webhook outbound: when the decrypted target hostname ends in `.onion`,
  delivery routes through the SOCKS5 proxy. Clearnet targets continue to
  go directly per Lead Decision Q4. The invariant "clearnet target with
  Tor flag on must NOT route via Tor" is asserted in tests.

## In progress

### Sprint 4b — Drop plaintext FK columns (DESTRUCTIVE)

- **Status**: deferred. Blocked on real `RemoteKeystore` deployment plus a
  24-hour soak with `STHRIP_READ_FROM_ENVELOPE=true` in production.
- Once shipped, plaintext `from_agent_id`, `to_agent_id`, `buyer_id`,
  `seller_id`, and `amount` columns will be dropped from
  `transactions`/`escrow_deals`/`escrow_milestones`/`message_relays`.
  After that, an attacker holding `ADMIN_API_KEY` and the full Railway
  Postgres dump cannot recover the payment graph without also compromising
  the separate operator keystore service.

## Roadmap (NOT shipped)

The following items appeared in the legacy `PRIVACY_FEATURES.md` as if they
were shipped. They are not. They are listed here as roadmap items only.

| Item | Status | Why deferred |
|------|--------|--------------|
| CoinJoin coordinator | Not shipped on the hub request path. Research code exists in `sthrip/bridge/mixing/coinjoin.py` (`CoinJoinTransaction`, `CoinJoinInput`, `CoinJoinOutput`, `start_round`) but is NOT invoked by any payment, escrow, marketplace, or webhook flow. | Requires off-chain MPC; the coordinator-free version remains research-grade. The bridge namespace is a separate engineering track. |
| Submarine Swaps | Not shipped on the hub request path. Research code exists in `sthrip/bridge/mixing/`. | Off-chain swap protocol with no production code wired into the hub. |
| zk-SNARKs / zk proofs in the request path | Not shipped. A Pedersen-commitment helper exists for review payloads (Phase 3a), but full zk verification is out of scope on this branch. | Research-grade. |
| MPC-based mixing without coordinator | Not shipped. | Listed in user-criteria as out-of-scope. |
| `WEBHOOK_FORCE_TOR=true` (route all outbound through Tor) | Not shipped. | Per Lead Decision Q4; default routing stays per-target to avoid latency penalties on clearnet agents. |

If older marketing copy advertised any of these as shipped, that copy was
overpromising. This file is the source of truth.

## Public claims that previously overshot reality

The pre-Sprint-7 version of this file claimed:

> **Combined ≤ 3 min, fully unlinkable.**
> Stealth Addresses + CoinJoin + Submarine Swaps + ZK Proofs + Tor.

Only the Tor leg of that claim is in the request path today (Sprint 6,
`16126a5`). Stealth addresses are inherited from the underlying Monero
wallet RPC (a protocol property, not something Sthrip implements). The
remaining components are roadmap items in the table above.

## Subscription Billing Retention (Phase 2 Sprint 4 — 2026-05-07)

The `agent_billing_history` ledger records every XMR subscription billing
event (monthly charge, grace start/retry, expiry downgrade, mid-month
upgrade, mid-month refund). Each row stores:

* `agent_id` (FK)
* `month_start` (calendar month)
* `amount_usd` and `amount_piconero`
* `rate_applied` (XMR/USD spot rate at the moment of the event)
* `status` (event type)
* `tier_at_event` (snapshot of the tier at billing time)
* `created_at`

This is custodial pricing data. The hub already sees plaintext routed
amounts in RAM during transfer routing (closed by Phase 3 TEE migration);
billing is no different in privacy posture, just less frequent. The
ledger is subject to the **same Phase 1 auto-purge** cron as
`transactions` and `audit_log`: rows older than `STHRIP_DATA_RETENTION_DAYS`
(default 60) are deleted on the daily 03:00 UTC sweep. Operators can
shorten retention but should not lengthen it without an updated
disclosure.

No bank, card, or off-chain identity is ever stored — billing is
XMR-native and anonymous beyond the agent's hub identity.
