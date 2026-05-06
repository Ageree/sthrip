# Lead Decisions on Open Questions

Even autonomy needs a deciding party. Lead resolves Planner's open questions so Generator/Evaluator have a fixed contract surface.

## Q1: Operator KEK custody

**Decision:** Option (a) — Railway service variable on a separate privileged service the API never reaches.

Concretely: a new Railway service `sthrip-op-keystore` (no public ingress, no DATABASE_URL) holds `KEK_OP` in env. It exposes a tiny HTTP API on private network (`sthrip-op-keystore.railway.internal`) with a single endpoint `POST /unwrap` that accepts wrapped DEKs and returns plaintext DEKs to caller. Auth via mTLS or shared secret distinct from `ADMIN_API_KEY`.

Rationale: realistic for single-operator startup. Achieves the property "ADMIN_API_KEY alone cannot decrypt the graph" because admin views must call the keystore service over network, and that service has independent ACLs.

For Sprint 3 dual-write phase, the keystore can be a no-op stub returning the DEK as-is (still encrypted but identity unwrap) — so Sprint 3 lands without infra dependency, and Sprint 4 cutover blocks until real `sthrip-op-keystore` deploys. Generator must stub-then-real.

HSM upgrade documented as future hardening in `docs/THREAT_MODEL.md` (Sprint 7).

## Q2: Salt rotation cadence

**Decision:** Weekly, configurable via `IP_SALT_ROTATION_DAYS` env var (default 7, accepts 1..30).

Rotation cron in existing scheduler infra, retires salts older than `2 * IP_SALT_ROTATION_DAYS` (so verifier still has a brief window for cross-rotation forensics tooling, but the destroy threshold is firm).

## Q3: Marketplace migration to `is_public=false`

**Decision:** **Hard cut.** All existing rows get `is_public=false` on migration. No grace period.

Rationale: the entire point of this hardening is no leaks by default. A grace period contradicts the threat model. Operators get notified via PRIVACY_FEATURES.md changelog and a release note in `MIGRATION_NOTES.md`. SDK 0.5.0 release announcement points them at `update_profile(is_public=True)`.

Generator will surface the SDK migration steps clearly.

## Q4: Tor sidecar scope

**Decision:** Outbound Tor **only when target hostname is `.onion`**. Inbound serves both clearnet and onion.

Rationale: forcing all hub→agent traffic through Tor doubles average latency for clearnet agents and adds operational fragility (Tor circuit failures = webhook retry storms). Per-target routing is the conservative ship-able default.

Future work in roadmap: optional config flag `WEBHOOK_FORCE_TOR=true` that routes all outbound through Tor for operators who accept the latency hit. Not in this sprint.

## Q5: MessageRelay envelope inclusion

**Decision:** Include `message_relays.from_agent_id` and `to_agent_id` in the same envelope migration as transactions/escrow.

Rationale: same migration window, same key schedule, same threat model. Splitting would just create a second migration with identical structure.

The `ciphertext_encrypted` field already protects message content; this closes the metadata-graph leak (who messaged whom).

## Workflow Decisions

- **Branch:** `feat/anonymity-hardening`. All sprint commits land here. No push to `origin/main` until full suite green AND Lead user approval.
- **Local test gate:** every sprint contract requires `pytest tests/ -x` (fail-fast) plus `pytest --cov=sthrip --cov-report=term --cov-fail-under=80` on changed modules before Generator declares ready.
- **Railway deploys:** sprints 1–5 require successful `pytest` only. Sprint 6 (Tor sidecar) is the first real Railway deploy in this branch, behind a `STHRIP_ONION_ENABLED=false` flag.
- **GitNexus reindex:** after each sprint commit, Generator runs `npx gitnexus analyze --embeddings` so the next sprint's `gitnexus_impact` calls are fresh.
- **Subagent context isolation:** Generator and Evaluator are spawned via independent `Agent({subagent_type:...})` calls with no shared message history. Lead passes only file paths, not Generator's reasoning, to Evaluator.
- **/loop checkpoint:** every 30 min the loop re-fires; Lead resumes by reading `.harness/anonymize-platform/state.json` (a tiny file Lead writes between sprints with `current_sprint`, `iteration`, `last_status`).
