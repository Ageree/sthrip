# Sprint 7 Result — Remote Attestation + SDK verify_tee + Cutover (FINAL)

**Verdict: PASS**

Commit `1ffb77c` on `feat/revenue-and-tee`. All 8 contract tests pass. Sprint 6 carry-over fix (4xx non-dict body) verified. ZERO regressions vs Sprint 6 baseline.

---

## Score by contract section

### A. Code presence — PASS

| File | Status | Notes |
|------|--------|-------|
| `gcp/payment_tee_deploy/attestation_service.py` | NEW (207 lines) | `collect_attestation()` collects SEV-SNP via `_try_collect_real_snp_quote` (returns `None` if `python_sev_snp` unavailable → stub mode); signs canonical JSON with Ed25519. `verify_attestation_signature()` exposed for SDK + offline operator checks. |
| `gcp/payment_tee_deploy/payment_service.py` | MODIFIED | Sprint 5 stub replaced. Two routes share `_serve_attestation()`: `/attestation` (back-compat) + `/.well-known/attestation.json` (operator-discoverable). Returns 503 if `STHRIP_TEE_ATTESTATION_KEY` unset — explicit failure, no silent fake quotes. |
| `sdk/sthrip/_tee_verify.py` | NEW (67 lines) | `has_required_fields()` + `verify_signature()` pure helpers. Mirrors `attestation_service` canonical-JSON convention exactly. |
| `sdk/sthrip/client.py` | MODIFIED | `Sthrip(verify_tee=False, expected_image_hash=None)` constructor params. `verify_tee_attestation(now=None)` method added — `now` injection enables deterministic cache testing. |
| `sdk/sthrip/attestation_anchors.py` | NEW (55 lines) | `KNOWN_GOOD_HASHES: list[str] = []` empty as required. `is_pinned()` helper. Operator playbook in module docstring. |
| `sdk/sthrip/exceptions.py` | MODIFIED | `TEEMismatchError`, `TEEAttestationStaleError` added, both extend `StrhipError`. |
| `docs/THREAT_MODEL.md` | MODIFIED | Phase 3 row added (line 47) with full pre/post comparison + residual risks: AMD CVE-class bugs, supply-chain, opt-out, Railway proxy hop. |
| `PRIVACY_FEATURES.md` | MODIFIED | Phase 2/3 section appended (line 169+) with explicit honest-framing: "applies only when operator has deployed AND flag set". |
| `gcp/payment_tee_deploy/CUTOVER.md` | NEW (393 lines) | 13 sequential steps + Monitoring + Alerts + Pinned-hashes lifecycle + Phase 2 close-out. |

### B. Attestation payload integrity — PASS

- Returns `quote_b64`, `image_hash_sha256`, `timestamp`, `signature_b64`, `mode` ✓
- `mode` is `snp_real` or `snp_stub` ✓ (production guidance: refuse to pin stub)
- Canonical JSON `_canonical_for_signing` excludes `signature_b64`, sorted keys, compact separators ✓
- Ed25519 via `nacl.signing.SigningKey` (no custom crypto) ✓
- Hub static key from env `STHRIP_TEE_ATTESTATION_KEY` (32-byte seed, base64) ✓
- 503 returned on missing key — never silently signs a deceptive payload ✓

### C. SDK verification logic — PASS

- `verify_tee=False` (default): `verify_tee_attestation` returns `None`, NO fetch (Test #5 enforces with `AssertionError("must not be called")`) ✓
- `verify_tee=True`:
  - Fetches `<api_url>/.well-known/attestation.json` ✓
  - Verifies Ed25519 sig with pinned pubkey from env `STHRIP_TEE_ATTESTATION_PUBKEY` ✓
  - Compares `image_hash_sha256` to `expected_image_hash` (kwarg or `STHRIP_TEE_IMAGE_HASH` env) ✓
  - Mismatch → `TEEMismatchError` ✓
  - Endpoint failure with no cache → `TEEAttestationStaleError` ✓ (also raised on non-2xx OR `RequestException` OR non-JSON body)
  - 5-min cache TTL (`self._attestation_ttl_s = 300.0`) ✓
  - No silent failure — cache only populated AFTER successful verification ✓

### D. Sprint 6 carry-over fix — PASS

- `payment_tee_client.py` raises `TEEUserError(status_code, detail)` when 4xx + non-dict body (line 222-238).
- `payment_dispatch.py` `except TEEUserError` arm (line 277-285) raises `HTTPException(status_code, detail)`. NO fall-back. Increments `tee_dispatch_total{outcome="user_error"}`.
- Test #8 verifies: 422 with body `"insufficient balance"` (string) → `HTTPException(422)`, `fallback_called["v"] == False`.

### E. `KNOWN_GOOD_HASHES` empty — PASS

`KNOWN_GOOD_HASHES: list[str] = []` (line 45). Comment lines show example format, all commented out. No production hashes shipped.

### F. CUTOVER.md verification — PASS

- 13 sequential steps with `Verify` blocks (verified structurally; spot-checked).
- Monitoring checklist + Alert thresholds + Pinned-hashes lifecycle sections present.
- Env vars consistent with code: `STHRIP_TEE_IMAGE_HASH`, `STHRIP_TEE_ENDPOINT`, `STHRIP_PAYMENT_VIA_TEE`, `STHRIP_TEE_ATTESTATION_KEY/PUBKEY`, `STHRIP_TEE_PROXY_TOKEN`, mTLS path envs.
- Pre-flight (Step 1): `gcloud auth list`, `gcloud projects list`, billing, IAM. ✓
- Rollback (Step 12): single env-var flip, no code change. ✓

### G. Tests run independently — PASS

```
$ PYTHONPATH=. pytest tests/test_sdk_tee_attestation.py -v --tb=short
8 passed in 0.24s
```

All 8 contract tests pass. Spot-reads verified:
- **`test_sdk_verify_tee_rejects_mismatch_hash`**: payload has `image_hash="aa..."`, expected="bb...", asserts `TEEMismatchError` raised. Genuine mismatch path. ✓
- **`test_sdk_attestation_cache_5min_ttl`**: uses injectable `now=` parameter, asserts call_count stays at 1 across t=0/60/299, jumps to 2 at t=301. Real cache + TTL boundary test. ✓
- **`test_dispatcher_4xx_non_dict_body_does_not_fallback`**: patches `payment_tee_client.dispatch_hub_routing` to raise `TEEUserError(422, "insufficient balance")`, patches `_local_dispatch` to track invocation, asserts `HTTPException.status_code == 422` AND `fallback_called["v"] is False`. Genuinely covers carry-over bug. ✓

(Note: requires `PYTHONPATH=.` at the repo root because `sdk` is not pip-installed; matches Sprint 6's invocation pattern. Same as how Generator ran them.)

### H. Sprint 5 test rewrite — PASS

`tests/test_payment_service_self_contained.py::test_payment_service_attestation_stub` rewritten (lines 211-239):
- Sets `STHRIP_TEE_ATTESTATION_KEY` + `STHRIP_TEE_IMAGE_HASH`.
- Reloads module, asserts payload contains `quote_b64`, `image_hash_sha256`, `timestamp`, `signature_b64`, `mode`.
- Asserts `mode in ("snp_real", "snp_stub")`.
- Other 7 tests in file unchanged → all 8 still pass (`8 passed in 5.49s`).

This deviation was disclosed in the Generator report. Acceptable: the Sprint 5 test asserted the OLD stub shape (`status="stub"`, `todo="..."`); Sprint 7's superseded contract demands the canonical payload shape, so the rewrite is *required* by Sprint 7's contract, not gratuitous.

### I. Sprint 6 dispatch regression — PASS

```
$ PYTHONPATH=. pytest tests/test_payment_dispatch.py -v --tb=short
12 passed, 1 warning in 0.30s
```

All 12 prior tests still green. The `TEEUserError` arm is purely additive.

### J. Full suite — PASS

```
$ PYTHONPATH=. pytest tests/ -q --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py
24 failed, 2899 passed, 21 skipped, 3015 warnings in 113.85s
```

- **2899 passed** (Sprint 6 baseline 2891 + 8 Sprint 7 = 2899) ✓
- **24 failed** — same pre-existing set (idempotency-keys table absent in some integration fixtures, mcp_tools auth tests requiring API key file, migration error-handling platform-specific, session_store Redis tests, readiness wallet test). Spot-checked names match Sprint 5/6 baseline. NONE touch Sprint 7 code paths. ✓
- **ZERO regressions from Sprint 7.** ✓

## Findings worth noting

1. **Two attestation routes (deviation, accepted).** Generator kept `/attestation` (Sprint 5's path) AND added `/.well-known/attestation.json`. Both share the same handler; payload is bit-identical. Disclosed in Generator report. Reasonable for back-compat with any tooling already wired to the Sprint 5 stub path. Accept.
2. **`TEEUserError` lives at the client boundary, not inline in dispatcher.** Generator placed the carry-over exception at the `payment_tee_client` layer — the client is the only place that sees the raw `resp.status_code`, so the structure matches the existing `TEEServerError`/`TEEUnreachableError` pattern. Correct design choice.
3. **Test #5 is strict.** Disabled-mode test does not just assert no exception; it patches `session.get` with `AssertionError("must not be called")`. Genuinely proves byte-for-byte back-compat of the default path.

## Anti-fantasy guards verified

- `KNOWN_GOOD_HASHES` ships empty ✓
- Pubkey NOT hardcoded — env-only ✓
- Stub mode tagged `snp_stub` ≠ `snp_real` ✓ (operator runbook step 6 spells out the verify)
- Service raises 503 (not signs a fake) when key unset ✓
- `/dev/sev-guest` not touched in tests — `_try_collect_real_snp_quote` returns `None` cleanly when `python_sev_snp` missing ✓
- CUTOVER.md every step has `Verify` block ✓

---

## Phase 2 Harness Closure

Sprint 7 closes the entire `.harness/phase2-money-and-tee/` plan.

### Sprint roll-up

| Sprint | Commit | Verdict | Net new tests |
|---|---|---|---|
| 1 (auto-purge + canary) | `a3a6e38` | PASS | (Phase 1 baseline; not counted in Phase 2 deltas) |
| 2 (commission) | `768d0ea` + `b1d05a3` (iter 2) | PASS | ~30 |
| 3 (subscription tier) | `dd29657` | PASS | ~32 |
| 4 (XMR billing cron) | `959377a` | PASS | ~38 |
| 5 (TEE deploy artefacts) | `ed3821c` | PASS | 8 |
| 6 (TEE proxy + flag) | `6fee072` | PASS | 12 |
| 7 (attestation + cutover) | `1ffb77c` | **PASS** | 8 |

### Cumulative test counts

- Phase 2 entry baseline: ~2700 (pre-Sprint 1 phase 1 closure).
- Sprint 6 baseline: 2891 passed.
- **Sprint 7 final: 2899 passed**, 24 pre-existing failures (unchanged across Sprints 5–7), 21 skipped.
- **Net Sprint 7 contribution: +8 tests, 0 regressions.**

### Commit chain on `feat/revenue-and-tee`

8 commits as expected (one per sprint, plus Sprint 2 iter 2):
```
1ffb77c feat(tee): remote attestation + SDK verify_tee + cutover runbook (Phase 3 Sprint 7 — FINAL)
6fee072 feat(tee): payment dispatch proxy w/ feature flag + fall-back (Phase 3 Sprint 6)
ed3821c feat(tee): GCP Confidential VM payment-service deploy artifacts (Phase 3 Sprint 5)
959377a feat(revenue): XMR subscription billing cron + grace handling (Phase 2 Sprint 4)
dd29657 feat(revenue): subscription tier enforcement + self-service endpoints (Phase 2 Sprint 3)
b1d05a3 feat(revenue): wire hub-routing to commission path (Sprint 2 iter 2)
768d0ea feat(revenue): commission on transfers (0.3% Free / 0.1% Pro+, Phase 2 Sprint 2)
a3a6e38 feat(privacy): auto-purge + warrant canary (Phase 1 Sprint 1)
```

### Recommendation for Lead

**Phase 2 + Phase 3 are READY-TO-MERGE pending operator deploy actions.**

Code is feature-complete. All sprint contracts satisfied. No regressions. Honest framing in `PRIVACY_FEATURES.md` ("shipped pending operator deploy") matches reality — the TEE flag defaults to OFF and `KNOWN_GOOD_HASHES` is empty, so the merge is safe even before any GCP work.

Operator action items (per `gcp/payment_tee_deploy/CUTOVER.md`):
1. Build + push the production TEE image; capture digest.
2. Generate `STHRIP_TEE_ATTESTATION_KEY`; provision the Confidential VM (`bash setup-vm.sh`).
3. Append the deploy digest to `KNOWN_GOOD_HASHES`; ship SDK 0.6.0+.
4. Soak Railway with `STHRIP_PAYMENT_VIA_TEE=false` for 24-48h, watching `tee_unreachable_total{reason="health"}`.
5. Flip flag in staging, then production.
6. After 7-day soak: announce in `PRIVACY_FEATURES.md` + blog post.

Lead may proceed to merge `feat/revenue-and-tee` → `main` immediately, with Phase 3 hardening behind the dormant feature flag.
