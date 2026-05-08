# Sprint 7 Generator Report — Remote Attestation + SDK verify_tee + Cutover (FINAL)

> Phase 3 Sprint 7 — last sprint of the harness. Closes Phase 2 + Phase 3.

## Files

### TEE-side
- **NEW** `gcp/payment_tee_deploy/attestation_service.py` (171 lines).
  Pure module: collects SEV-SNP quote (or stubs it when
  `python_sev_snp` is unavailable), signs canonical JSON with Ed25519.
  Mirrors `sthrip/services/canary_service.py` conventions exactly
  (sorted-keys / compact JSON / detached signature / dedicated key).
  Exposes `collect_attestation()` (called by FastAPI handler) and
  `verify_attestation_signature()` (used by tests + offline operator
  checks). Stub mode is tagged `mode="snp_stub"`; production must
  refuse to pin a stub.
- **MODIFIED** `gcp/payment_tee_deploy/payment_service.py`. The Sprint 5
  `/attestation` stub is replaced. Two routes now share a helper:
  `/attestation` (back-compat) and `/.well-known/attestation.json`
  (operator-discoverable). Returns 503 if `STHRIP_TEE_ATTESTATION_KEY`
  is unset — explicit failure, never silently signs a deceptive payload.

### SDK-side
- **MODIFIED** `sdk/sthrip/client.py`. New `verify_tee` (default False)
  and `expected_image_hash` constructor params. New
  `verify_tee_attestation(now=None)` method. 5-min cache, raises
  `TEEMismatchError` on hash/sig mismatch and
  `TEEAttestationStaleError` on fresh-fetch failure. Default
  `verify_tee=False` preserves byte-for-byte SDK back-compat — no
  existing test broke.
- **NEW** `sdk/sthrip/_tee_verify.py` (66 lines). Pure helpers
  (canonical-JSON + Ed25519 verify) extracted so `client.py` stays
  focused.
- **NEW** `sdk/sthrip/attestation_anchors.py` (47 lines). Empty
  `KNOWN_GOOD_HASHES` list — operator populates after first deploy.
  Per the contract anti-fantasy guard, no real production hashes
  shipped.
- **MODIFIED** `sdk/sthrip/exceptions.py`. Adds `TEEMismatchError`,
  `TEEAttestationStaleError` extending `StrhipError`.
- **MODIFIED** `sdk/sthrip/__init__.py`. Exports new exceptions; SDK
  version bump 0.5.0 → 0.6.0.

### Sprint 6 carry-over fix
- **MODIFIED** `sthrip/services/payment_tee_client.py`. New
  `TEEUserError` exception. When the TEE returns a 4xx with a non-dict
  JSON body, the client now raises `TEEUserError(status_code, detail)`
  instead of silently returning an untagged value. `json` imported as
  `json_lib` to avoid name collisions.
- **MODIFIED** `sthrip/services/payment_dispatch.py`. New `except
  TEEUserError` arm that surfaces the real status code as
  `HTTPException` and increments `tee_dispatch_total{outcome="user_error"}`.
  Crucially, NO fall-back — falling back would re-charge the user on
  the local path after the TEE already rejected them.

### Documentation
- **MODIFIED** `docs/THREAT_MODEL.md`. Added one row: "Hub runtime memory
  compromise — Railway host attacker reads in-flight plaintext (Phase 3)".
  Pre-Phase-3 row stays the same; the new row spells out post-Sprint-7
  benefits + residual risks (AMD primitive bugs, image-pinning bypass,
  Railway proxy still sees plaintext on the way to the TEE). Existing
  table structure preserved.
- **MODIFIED** `PRIVACY_FEATURES.md`. Appended Phase 2/Phase 3 section
  (~70 lines) at the end. Honest framing per the contract: "applies
  only when the operator has deployed the matching artefact AND set
  the corresponding feature flag". Earlier sections untouched.
- **NEW** `gcp/payment_tee_deploy/CUTOVER.md` (336 lines). 12+1 step
  operator runbook: pre-flight, mTLS gen, key gen, image build/push,
  VM provision, end-to-end verify, SDK pin, Railway env config,
  health-check soak, staging flip, prod flip, rollback. Each step has
  a `Verify` block. Rollback table + monitoring checklist + alert
  thresholds at the bottom.

### Tests
- **NEW** `tests/test_sdk_tee_attestation.py` (8 tests, 332 lines). All
  green.
- **MODIFIED** `tests/test_payment_service_self_contained.py`. The
  Sprint 5 `test_payment_service_attestation_stub` was asserting the
  old stub shape (`status="stub"`, `todo="Sprint 7 ..."`). Sprint 7
  superseded that endpoint, so the test now sets
  `STHRIP_TEE_ATTESTATION_KEY` and asserts the canonical Sprint 7
  payload (`quote_b64`, `image_hash_sha256`, `timestamp`,
  `signature_b64`, `mode`). The other 7 tests in that file are
  unchanged and still pass.

## Test results

```
$ pytest tests/test_sdk_tee_attestation.py -v --tb=short
tests/test_sdk_tee_attestation.py::test_attestation_endpoint_includes_required_fields PASSED
tests/test_sdk_tee_attestation.py::test_sdk_verify_tee_accepts_pinned_hash PASSED
tests/test_sdk_tee_attestation.py::test_sdk_verify_tee_rejects_mismatch_hash PASSED
tests/test_sdk_tee_attestation.py::test_sdk_verify_tee_rejects_invalid_signature PASSED
tests/test_sdk_tee_attestation.py::test_sdk_verify_tee_disabled_skips_check PASSED
tests/test_sdk_tee_attestation.py::test_sdk_attestation_cache_5min_ttl PASSED
tests/test_sdk_tee_attestation.py::test_sdk_attestation_stale_raises_on_fresh_fetch_failure PASSED
tests/test_sdk_tee_attestation.py::test_dispatcher_4xx_non_dict_body_does_not_fallback PASSED
8 passed in 0.28s
```

### Sprint 6 dispatch regression
12/12 still pass; the new `TEEUserError` arm is an additive code path.

```
$ pytest tests/test_payment_dispatch.py -v --tb=short
12 passed, 1 warning in 0.26s
```

### Full suite

| Metric | Sprint 6 baseline | Sprint 7 |
|--------|-------------------|----------|
| Passed | 2891 | **2899** (+8) |
| Failed | 24 (pre-existing) | 24 (same set) |
| Skipped | 21 | 21 |

```
24 failed, 2899 passed, 21 skipped, 3015 warnings in 117.18s
```

The 24 pre-existing failures are unchanged from Sprint 5/6 baseline
(idempotency-keys table missing in some integration fixtures, mcp_tools
auth tests requiring a live API key file, migration error-handling
tests with platform-specific behaviour). None of them touch Sprint 7
code paths and none flipped state.

ZERO regressions from Sprint 7 changes.

## Sprint 6 carry-over fix — verification

The contract carry-over reads:

> dispatcher 4xx routing relies on `body["_status_code"]`. If TEE
> returns a 4xx with non-dict body (e.g., plain string), the tag is
> skipped → misroutes as success.

Fix shipped in **two layers**:

1. **Client layer** (`payment_tee_client.py`): when `resp.status_code`
   is 4xx AND `parsed` is not a dict, raise
   `TEEUserError(status_code, detail)` instead of returning an
   untagged value.
2. **Dispatcher layer** (`payment_dispatch.py`): new `except
   TEEUserError` arm raises `HTTPException(exc.status_code,
   exc.detail)` — NO fall-back.

Verified by `test_dispatcher_4xx_non_dict_body_does_not_fallback`
(test #8 in the new file): TEE returns 422 with body
`"insufficient balance"` (plain string); dispatcher raises
`HTTPException(422)`, fall-back is NOT called. PASS.

## Deviations from contract

None. Two minor implementation choices worth flagging:

1. **Two attestation routes** (`/attestation` AND
   `/.well-known/attestation.json`) instead of one. The contract said
   `/.well-known/attestation.json`; I kept `/attestation` as a back-
   compat alias because it was already wired in Sprint 5 and removing
   it would have risked breaking any in-flight tooling. Both routes
   share the same handler, so the payload is bit-identical.
2. **`TEEUserError` as a separate exception** instead of inlining the
   non-dict 4xx detection in the dispatcher. The exception lives at
   the client boundary because the client is the only place that
   actually saw the raw status code; the dispatcher just translates.
   This keeps the dispatcher's branching shallow and matches the
   existing `TEEServerError` / `TEEUnreachableError` pattern.

Anti-fantasy guards verified:
- [x] Pubkey is NOT hardcoded — env-only via
      `STHRIP_TEE_ATTESTATION_PUBKEY`.
- [x] `KNOWN_GOOD_HASHES` ships empty (operator populates).
- [x] `/dev/sev-guest` is NOT touched in tests — `_try_collect_real_snp_quote`
      gracefully returns None when `python_sev_snp` is missing, and
      stub mode is engaged.
- [x] CUTOVER.md verifiable: every step has a `Verify` block with
      copy-pasteable check commands.

## Phase 2 close-out

Sprint 7 closes the entire `.harness/phase2-money-and-tee/` plan. Eight
sprint commits on `feat/revenue-and-tee` (Sprint 2 had an iter 2,
hence 8 commits for 7 sprints):

1. `feat/anonymity-hardening` — Phase 1 baseline (auto-purge + canary)
2. `b1d05a3` — commission Sprint 2
3. `dd29657` — subscription tier Sprint 3
4. `959377a` — XMR billing Sprint 4
5. `ed3821c` — TEE deploy Sprint 5
6. `6fee072` — TEE proxy Sprint 6
7. `<this commit>` — attestation + cutover Sprint 7

Operator action items remaining (per the runbook):
- Build + push the production TEE image, capture digest.
- Generate `STHRIP_TEE_ATTESTATION_KEY` and provision the VM.
- Append digest to `KNOWN_GOOD_HASHES`, ship SDK 0.6.0+.
- Soak Railway with flag OFF for 24-48h.
- Flip flag in staging, then prod.

Until the operator does the above, Phase 3 is "shipped pending operator
deploy" — `PRIVACY_FEATURES.md` says so honestly.
