# Sthrip Security Audit — 2026-04-27

**Scope:** `sthrip/` Python core (FastAPI + escrow + Monero hub + integrations).
**Out-of-scope (flagged for separate review):** Solidity contracts (`contracts/*.sol` — needs Slither/Mythril), TSS service (`tss-service/`), TS SDK (`stealthpay-ts/` — zero runtime deps, low risk).
**Tooling used:** `bandit` 1.8.6, `pip-audit`, manual review with gitnexus, three parallel sub-agents, grep-based pattern hunting.
**Methodology:** State-machine integrity + balance arithmetic > authz boundaries > crypto primitives > deps + infra. Findings ranked by exploitability × impact, not OWASP order.

---

## Executive Summary

Sthrip's security architecture is **fundamentally sound**: API keys hashed with HMAC-SHA256, webhook delivery uses DNS resolve + IP pinning + SNI preservation (anti-rebinding), CSRF tokens on admin UI, constant-time admin compare, deny-by-default CORS, settings validators reject default secrets in non-dev, all mutation routers behind auth, and balance mutations use `FOR UPDATE` row-level locks with status-guarded writes.

The audit found **3 CRITICAL** (all in background-cron + recurring-payment paths), **5 HIGH**, **8 MEDIUM**, and several LOW/informational issues. No CRITICAL issues exist in synchronous request paths or auth — the elevated risk lives in the asynchronous payment-scheduling layer (subscriptions/streams/recurring) which lacks distributed leases and post-cancellation re-validation.

**Production blockers (must fix before further mainnet exposure):** F-1, F-2, F-3, F-7 (dep CVEs).

---

## CRITICAL

### F-1. Concurrent recurring-payment double-charge (no distributed lease)
- **File:** `sthrip/services/recurring_service.py` — `_recurring_payment_loop()` (background cron)
- **Vector:** Any deployment with ≥2 API replicas (Railway autoscale, blue/green deploy, manual scale-out). Both instances poll the recurring-payments table at the cron interval; both pick up the same `subscription_id` due whose `next_charge_at <= now()` and execute deduction in parallel.
- **Why it works:** SELECT loads candidate rows without `FOR UPDATE SKIP LOCKED`. There is no Redis/DB lease keying the loop instance. The only thing protecting balance is the `FOR UPDATE` *inside* `BalanceRepository.deduct`, which serialises the two charges but does not de-duplicate them: both succeed, balance is debited twice for the same period.
- **Impact:** Direct theft from any user when more than one replica runs (mainnet payments). Severity is independent of escrow integrity controls because it bypasses them — recurring charges don't go through escrow status guards.
- **Fix:** Wrap the loop body in a Redis SETNX lease (pattern already used by `deposit_monitor.py:170` `_UNLOCK_SCRIPT`). Additionally use `SELECT ... FOR UPDATE SKIP LOCKED` when fetching due payments and update `next_charge_at` *inside* the same transaction.
- **PoC sketch:**
  ```bash
  # Scale to 2 replicas, time the cron to overlap, observe duplicate Transactions
  railway scale --replicas 2
  watch -n5 "psql $DB -c \"SELECT subscription_id, COUNT(*) FROM transactions WHERE created_at > now()-interval '5 min' GROUP BY 1 HAVING COUNT(*) > 1\""
  ```

### F-2. Ghost charges after subscription cancellation (TOCTOU on `is_active`)
- **File:** `sthrip/services/recurring_service.py` — charge-execution path
- **Vector:** Cron fetches the due subscription row at T0, the user calls `DELETE /v2/subscriptions/{id}` at T0+ε which sets `is_active=False`, the cron continues without re-reading and deducts the balance.
- **Why it works:** `_recurring_payment_loop` reads `is_active` once when selecting candidates, then commits the deduction without re-checking inside the locked transaction.
- **Impact:** Cancelled subscription continues charging at least once (worst case once per replica per cycle).
- **Fix:** Re-validate `is_active` inside the same transaction as the deduction, with `FOR UPDATE` on the subscription row before computing the charge.

### F-3. No lease on background loops (escrow auto-resolution + recurring + SLA)
- **Files:**
  - `api/main_v2.py` — `_escrow_auto_resolution_loop()` (5-min cycle)
  - `sthrip/services/recurring_service.py` — `_recurring_payment_loop()`
  - `sthrip/services/sla_service.py` — SLA enforcement cron (30 s cycle per memory)
- **Vector:** Multiple replicas execute the same cron simultaneously. For escrow auto-resolution the status-guarded UPDATE prevents double-state-transition (mitigated), but the cron still does N times the work, costing DB locks and starving the request path during peak. For SLA enforcement the failure mode depends on the service implementation; treat as exploitable until each loop is verified to be idempotent under concurrency.
- **Fix:** Single Redis SETNX lease per loop name with `EXPIRE` slightly longer than the cycle interval; release on shutdown via the existing `_UNLOCK_SCRIPT` pattern.
- **Note:** Same root cause as F-1; a single `with_lease("loop-name", ttl)` decorator applied to every cron closes all three.

---

## HIGH

### F-4. Idempotency-key replay window after Redis TTL expiry
- **File:** `sthrip/services/idempotency.py` (24 h Redis TTL on idempotency keys)
- **Vector:** A client retains a long-lived idempotency key, replays the same `POST /v2/payments` request 24 h+ later. Redis no longer remembers the key; Sthrip processes the request as new and charges again.
- **Impact:** Double-charge on retry beyond the TTL, especially with naive client SDKs that retry indefinitely with a stable key.
- **Fix:** For mutation endpoints, persist idempotency keys to PostgreSQL (`idempotency_keys` table with `agent_id, key, response_hash, created_at`) with no expiry, and only use Redis as a hot-path cache. Or scope the Redis TTL to the documented retry window (e.g. 1 h) and reject keys older than that with 409.

### F-5. Channel-signing exception handler swallows non-signature errors
- **File:** `sthrip/services/channel_signing.py:82-90` — `verify_channel_state()`
- **Code:**
  ```python
  except (BadSignatureError, Exception):  # noqa: BLE001
      return False
  ```
- **Vector:** A bug elsewhere in the verification path (e.g. `ValueError` from malformed base64, or a regression in `_build_message`) returns `False` *for the same reason a forged signature returns `False`*. The caller cannot distinguish "rejected forgery" from "bug ate the input." Future refactor risk: if any malformed key/signature crashes deep inside libsodium with a non-`BadSignatureError`, a future reorder of catch clauses could cause silent acceptance.
- **Impact:** Defence-in-depth weakened; alarms hidden; future refactor risk. Not directly exploitable today because the function returns `False` (deny-by-default).
- **Fix:** Catch `BadSignatureError` only; let `(ValueError, TypeError)` log and return `False`; let unexpected exceptions propagate (they'll surface in Sentry instead of silently rejecting valid signatures).

### F-6. Race between cancellation and in-flight charge confirmation
- **File:** `sthrip/services/recurring_service.py`
- **Vector:** Same window as F-2 but for the confirmation path: cancel arrives between `deduct_balance` and `commit`; the deduction commits because no row-level lock on the subscription is held, and the cancellation cannot roll it back.
- **Fix:** Lock the subscription row (`FOR UPDATE`) inside the charge transaction so cancellation must wait for the charge to commit (or vice versa).

### F-7. Vulnerable dependencies (3 CVEs)
- **`python-multipart 0.0.20`** — fix `0.0.26`
  - GHSA-wp53-j4wj-2cfg: path traversal when `UPLOAD_DIR` non-default (low blast radius — Sthrip uses default in-memory parsing, but upgrade for hygiene)
  - GHSA-mj87-hwqh-73pj: DoS via crafted `multipart/form-data`. **This is exploitable now**: any unauthenticated POST endpoint with multipart parsing can be force-fed pathological input.
- **`requests 2.32.5`** — fix `2.33.0`
  - GHSA-gc5v-m9x4-r6x2: predictable temp filename in `extract_zipped_paths`. Sthrip uses `requests` for outbound HTTP only; risk is moderate.
- **`python-dotenv 1.2.1`** — fix `1.2.2`
  - GHSA-mf9w-mj56-hr94: symlink follow on `set_key`. Sthrip only reads `.env` at startup — exploitable only if an attacker has filesystem write access (already game over). LOW in practice.
- **Fix:** `pip install --upgrade python-multipart>=0.0.26 requests>=2.33.0 python-dotenv>=1.2.2`, regenerate `requirements.lock`.

### F-8. Webhook URL SSRF guard — TOCTOU at registration vs. delivery
- **File:** `sthrip/services/url_validator.py` + `webhook_service.py`
- **Status:** **Largely mitigated.** The `_send_webhook` path re-resolves DNS and pins to the resolved IP on every send (`webhook_service.py:106-135`), with SNI preserved via `server_hostname=original_hostname`. This closes the rebinding gap. Confirm `block_on_dns_failure=True` is used in the sender path; at registration time it defaults to `False`.
- **Recommendation:** Reclassify to MEDIUM if confirmed.

---

## MEDIUM

### F-9. Admin API session tokens not bound to IP/User-Agent
- **File:** `api/deps.py:185-210` (`get_admin_session`) + `api/session_store.py`
- **Vector:** A leaked admin bearer token (XSS on a third-party page the admin visited, log file capture, accidental paste) is usable from anywhere for the full 8 h TTL.
- **Fix:** Store `client_ip` + a UA fingerprint hash with the session and reject if either changes.

### F-10. Webhook receiver-side signature check is the receiver's responsibility (no SDK enforcement)
- **File:** `sthrip/services/webhook_service.py:88-94`
- **Status:** Sthrip signs outgoing webhooks correctly (HMAC-SHA256 + timestamp). Receivers can choose not to verify, opening them up to forged events. This is a documentation/SDK gap, not a Sthrip bug.
- **Fix:** Ship a `WebhookVerifier` helper in the SDK and document mandatory verification.

### F-11. Audit log lacks tamper-evident chaining
- **File:** `sthrip/services/audit_logger.py`
- **Vector:** An attacker with DB write access (compromised credential, insider threat) can rewrite `audit_log` rows — modify timestamps, change `agent_id`, or delete rows entirely.
- **Fix:** Append-only chain with HMAC over `(prev_hmac, action, agent_id, ip, timestamp, sanitized_details)`. Verify chain on startup and via a periodic job. Use a separate `AUDIT_HMAC_KEY`.

### F-12. TSS deterministic-nonce with shared seed across signing sessions
- **File:** `sthrip/bridge/tss/signer.py:149-166`
- **Vector:** Nonce derivation is `SHA256(self.nonce_seed || message_hash || private_share)`. If the same signer instance signs the same message twice (retry on transport error without rotating the seed), the nonce repeats. ECDSA-style schemes leak the private share on nonce reuse.
- **Fix:** Add a monotonic per-signer counter to the nonce input, or regenerate `self.nonce_seed = secrets.token_bytes(32)` per signing call.
- **Note:** Out-of-scope for the synchronous API path. Flag for the TSS service review.

### F-13. `random` (Mersenne Twister) used in client-side privacy decisions
- **File:** `sthrip/privacy.py:64, 78, 92, 109, 127` (imported by `sthrip/client.py`)
- **Vector:** Decisions like ring-size choice, decoy timing, profile selection use `random.choice` / `random.random`. Mersenne Twister is deterministic given enough samples and is **not** suitable for adversarial-privacy choices — an observer can recover the PRNG state from ~624 outputs and predict subsequent privacy decisions.
- **Impact:** Anti-fingerprinting weaker than intended for clients that ship this module to end users. **Not exploitable against the hub** (hub doesn't run this module).
- **Fix:** Replace with `secrets.choice` / `secrets.randbelow` throughout `sthrip/privacy.py`.

### F-14. `sthrip/network.py` and `sthrip/antifingerprint.py` — dead code with weak primitives
- **Files:** `sthrip/network.py:208` (MD5), `sthrip/antifingerprint.py` (`random.choice` × 10)
- **Status:** Verified via grep — neither module is imported by `sthrip/services/`, `api/`, or any active production path. **Dead/dormant code.**
- **Fix:** Delete the modules. If retained, replace MD5 with SHA-256 (or annotate `usedforsecurity=False`) and `random` with `secrets`.

### F-15. CORS auto-extends to localhost in dev mode regardless of config
- **File:** `api/middleware.py:146-156`
- **Vector:** In `dev` mode, the CORS list always extends to `localhost:3000/8000`. If a production hotfix accidentally sets `ENVIRONMENT=dev` (e.g. on a staging-disguised box), `allow_origins` includes localhost.
- **Fix:** Auto-extend only on explicit opt-in (`CORS_DEV_AUTOEXTEND=1`).

### F-16. `text(...).format()` with hardcoded enum values
- **File:** `api/main_v2.py:257` — `sa_text("ALTER TYPE {} ADD VALUE IF NOT EXISTS '{}'".format(enum_name, val))`
- **Status:** Inputs are hardcoded ENUM_MAP, no user-derived values. Functionally safe today.
- **Risk:** Pattern is fragile. A future contributor adds `ENUM_MAP[user_provided_field] = …` and now there's an SQL-injection vector in startup code.
- **Fix:** Whitelist enum names; or extract to a utility that asserts identifier-safe strings.

---

## LOW / Informational

### F-17. Bandit: 5 × bind-to-0.0.0.0 (B104)
- `api/main_v2.py:724`, `examples/data_selling_agent.py:226`, `sthrip/bridge/p2p/node.py:91`, `sthrip/bridge/relayers/mpc_node_v2.py:75`. Acceptable inside containers if Railway/network-policy firewalls inbound traffic. **Document the assumption** in `DEPLOYMENT.md`.

### F-18. Bandit: `urlopen` permissive scheme (B310)
- `sthrip/services/rate_service.py:141` — verify URL source is internal/trusted. If user-influenced, route through `validate_url_target`.

### F-19. `requests` calls without timeout in test scripts
- `scripts/test_hub_routing_e2e.py` — 11 instances. Test-only. Add `timeout=10` for hygiene.

### F-20. pyCryptodome keccak fallback in `api/schemas.py:438`
- **Status:** Correct. `pycryptodome` (maintained) is preferred, `pysha3` is fallback. Bandit B413 fires on the import name `Crypto` regardless of which library implements it. **Suppress** with `# nosec B413 - pycryptodome, not pycrypto`.

### F-21. `sql_echo` setter inferred from env
- `sthrip/config.py:156-159` — model_validator correctly raises `SystemExit` if `SQL_ECHO=true` in production. ✓ Verified.

---

## Verified safe (no finding)

| Area | Why it's safe |
|---|---|
| API key storage | HMAC-SHA256 hashed in `Agent.api_key_hash`; plaintext never persisted; lookups by hash equality on indexed column. Generation via `secrets.token_hex(32)`. |
| Admin key compare | `hmac.compare_digest` (constant-time). Length & default-value validators reject weak keys at startup in non-dev. |
| Webhook delivery SSRF | DNS resolve → IP-pinned URL → SNI preservation via `server_hostname`; blocks private/loopback/reserved/link-local/multicast/unspecified; rejects creds in URL. |
| Webhook HMAC | SHA-256 + timestamp header. |
| CSRF on admin UI | Single-use tokens via `_session_store.create_csrf_token()` + `verify_csrf_token`; embedded in every form. |
| CORS | Deny-by-default; `allow_credentials=False`; no reflective origin. |
| Body size DoS | 1 MB hard cap, chunked-aware (rejects mid-stream). |
| Security headers | CSP (strict for app, relaxed only at `/docs`), HSTS, X-Frame DENY, Referrer-Policy, Permissions-Policy. |
| Balance arithmetic | `Decimal` types throughout; `FOR UPDATE` on balance rows; status-guarded UPDATEs; no float money. |
| Escrow state machine (synchronous path) | Conditional UPDATE `WHERE status=expected`; row-level lock; trust-score side effects in same tx. |
| Authorization between agents | All 26 mutation routers `Depends(get_current_agent)`; ownership checks present in escrow/payment endpoints (verified by sub-agent). |
| Rate limiting | Per-IP failed-auth + per-agent tier; Redis Lua atomic. |
| HTLC preimage entropy | 32 bytes from `secrets.token_bytes()`; SHA-256 hashlock; 144-block timeout. |
| Channel nonce replay | Strictly increasing nonce required (`channel_service.py:160`). |
| TLS for P2P | TLS 1.3 only, AEAD ciphers only, MD5/DSS rejected. |
| `.env` handling | `.env*` in `.gitignore` (verified); only `.env.example` files committed; pydantic-settings validates required values at startup. |
| ZK reputation primitives | Pedersen commitments over RFC 3526 Group 14, second generator from NUMS hash, `secrets.randbelow` for scalars, SHA-256 Fiat-Shamir. |

---

## Out-of-scope items (recommend separate review)

1. **Solidity contracts** (`contracts/StealthPayBridge.sol`, `InsuranceFund.sol`, `PriceOracle.sol`)
   - First-pass observation: uses OpenZeppelin AccessControl + ReentrancyGuard + Pausable, Chainlink oracle, `nonReentrant` on lock/claim/refund/MPC paths, MIN/MAX amount, address(0) checks, claim de-duplication, `MAX_DEVIATION` price guard.
   - **Action:** Run Slither + Mythril; manual reentrancy + oracle-manipulation review by a Solidity auditor before any value flows through the bridge.
2. **TSS service** (`tss-service/`) — separate review needed; nonce-counter fix from F-12 is a starting point.
3. **TS SDK** (`stealthpay-ts/`) — `@ageree/sthrip` 0.5.0, zero runtime deps. Low supply-chain risk.
4. **Infrastructure** — assumed correctly firewalled per `Infrastructure` notes. Run an external Nmap to confirm only the API port is exposed publicly.

---

## Remediation plan (priority order)

| Order | Action | Effort | Blocker for production? |
|---|---|---|---|
| 1 | Distributed lease decorator on all background loops (fixes F-1, F-3) | M | **Yes** |
| 2 | Re-validate `is_active` + lock subscription row in charge path (F-2) | S | **Yes** |
| 3 | Bump `python-multipart`, `requests`, `python-dotenv` (F-7) | XS | **Yes** |
| 4 | DB-backed idempotency for payments (F-4) | M | **Yes** |
| 5 | Tighten exception in `verify_channel_state` (F-5) | XS | No |
| 6 | Bind admin session to IP/UA hash (F-9) | S | No |
| 7 | Audit-log HMAC chain (F-11) | M | No |
| 8 | TSS nonce counter (F-12) — when TSS service is in scope | S | Yes (for TSS) |
| 9 | Replace `random` → `secrets` in `sthrip/privacy.py` (F-13) | XS | No (client-side) |
| 10 | Delete `sthrip/network.py` + `antifingerprint.py` if unused (F-14) | XS | No |
| 11 | Lock CORS dev-extend behind explicit env (F-15) | XS | No |
| 12 | Whitelist enum identifiers in startup migration (F-16) | XS | No |

---

**Auditor:** Claude Opus 4.7 (1M context) + Haiku 4.5 sub-agents (escrow/payments, auth/admin/webhook, crypto)
**Date:** 2026-04-27
**Confidence:** HIGH on synchronous request path (auth, webhook, middleware, CSRF, balance arithmetic). MEDIUM on background-cron concurrency claims (sub-agent #1 found patterns; concurrency tests under load would confirm). LOW on Solidity contracts and TSS service (out of scope).

**Supplementary detail in:**
- `/tmp/audit_escrow_payments.md` — escrow + payments deep dive (510 lines)
- `/tmp/audit_auth_admin_webhook.md` — auth/admin/webhook full report
- `/tmp/audit_crypto.md` — cryptography review
